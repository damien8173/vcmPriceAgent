import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Query
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from monitor.hkex import fetch_filings
from monitor.extractor import extract_dividend

load_dotenv()

INTERNAL_SECRET = os.getenv("INTERNAL_SECRET", "")
scheduler = AsyncIOScheduler()


def require_secret(x_internal_secret: str = Header(default="")):
    if INTERNAL_SECRET and x_internal_secret != INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Poll HKEX every 15 minutes while the service is running
    scheduler.add_job(poll_all_tickers, "interval", minutes=15, id="hkex_poll")
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="HKEX Monitor Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tickers")
async def get_tickers(x_internal_secret: str = Header(default="")):
    require_secret(x_internal_secret)
    from supabase import create_client
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    result = sb.table("tickers").select("symbol").eq("active", True).execute()
    return {"tickers": [r["symbol"] for r in result.data]}


@app.get("/filings")
async def get_filings(
    ticker: str = Query(...),
    limit: int = Query(10),
    x_internal_secret: str = Header(default=""),
):
    require_secret(x_internal_secret)
    filings = await fetch_filings(ticker, limit=limit)
    return {"filings": filings}


@app.get("/dividends/upcoming")
async def get_upcoming_dividends(
    days: int = Query(30),
    x_internal_secret: str = Header(default=""),
):
    require_secret(x_internal_secret)
    from supabase import create_client
    from datetime import date, timedelta
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    cutoff = (date.today() + timedelta(days=days)).isoformat()
    result = (
        sb.table("dividends")
        .select("*")
        .gte("ex_date", date.today().isoformat())
        .lte("ex_date", cutoff)
        .order("ex_date")
        .execute()
    )
    return {"dividends": result.data}


@app.post("/poll")
async def manual_poll(x_internal_secret: str = Header(default="")):
    require_secret(x_internal_secret)
    await poll_all_tickers()
    return {"status": "poll triggered"}


async def poll_all_tickers():
    """Core polling loop — fetch new filings and extract dividends."""
    from supabase import create_client
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    tickers_result = sb.table("tickers").select("symbol").eq("active", True).execute()

    for row in tickers_result.data:
        symbol = row["symbol"]
        filings = await fetch_filings(symbol, limit=5)
        for filing in filings:
            # Skip already-processed filings
            existing = sb.table("filings").select("id").eq("hkex_id", filing["id"]).execute()
            if existing.data:
                continue

            # Persist filing
            filing_row = sb.table("filings").insert({
                "hkex_id":        filing["id"],
                "ticker_symbol":  symbol,
                "title":          filing.get("title"),
                "filing_url":     filing.get("url"),
                "published_at":   filing.get("published_at"),
            }).execute().data[0]

            # Extract dividend data via AI
            dividend = await extract_dividend(filing)
            if dividend:
                sb.table("dividends").insert({
                    "filing_id":      filing_row["id"],
                    "ticker_symbol":  symbol,
                    **dividend,
                }).execute()
                # TODO: send notification via Slack/Discord/Telegram
