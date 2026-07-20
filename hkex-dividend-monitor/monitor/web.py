"""FastAPI web dashboard + JSON API for the HKEX dividend monitor.

Serves a single static HTML page (monitor/static/index.html -- vanilla
JS, no build step) and a small JSON API that wraps the exact same
registry/notifier/db/chat modules the CLI and daemon use, so there is
only one source of truth for every operation.

Binds to 127.0.0.1 only (see docker-compose.yml's port mapping) -- this
is a single-user local tool, so there is no authentication layer.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from datetime import date as _date
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from monitor import board_meetings, chat_feedback, history, settlement, settlement_history, sgx_daily, watchlist
from monitor._ssl_bootstrap import trust_store_status
from monitor.activity import log_event, read_recent
from monitor.chat import ChatError, run_chat_turn
from monitor.config import HEARTBEAT_FILE, get_config, masked_settings, save_settings
from monitor.daemon import HKT, racing_targets, target_match_status
from monitor.db import SurrealDBError, health as db_health
from monitor.diagnostics import log_error
from monitor.extractor import ExtractionError, test_deepseek_connection
from monitor.hkex_search import (
    HKEXSearchError,
    fetch_latest_filings,
    lookup_stock_id,
    upsert_filing_metadata,
)
from monitor.notifier import AlertPayload, configured_channels, dispatch_alert
from monitor.registry import (
    AlertHistory,
    ChannelHealth,
    DividendStore,
    NotifiedCache,
    TargetRegistry,
    WatchlistTickers,
    normalize_ticker,
)
from monitor.settlement import SettlementError

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Row cap for the Dashboard's latest-market-filings overview (see
# /api/latest-filings), regardless of what ?limit= the client asks for.
LATEST_FILINGS_MAX_LIMIT = 100

registry = TargetRegistry()
notified_cache = NotifiedCache()
alert_history = AlertHistory()
dividend_store = DividendStore()
channel_health = ChannelHealth()
watchlist_tickers = WatchlistTickers()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    get_config().ensure_data_dir()
    dividend_store.ensure_seeded()
    # Define company_event/dividend_watchlist before anything queries them --
    # without this, the very first GET /api/watchlist on a fresh SurrealDB
    # (before any generation has ever run) fails with "table does not
    # exist" instead of the intended empty/"generating" response.
    try:
        history.ensure_schema()
    except Exception as exc:  # noqa: BLE001 - a DB hiccup at startup must not prevent the app from serving
        log_error("web.startup", f"Failed to ensure watchlist schema: {exc}")
    try:
        settlement_history.ensure_schema()
    except Exception as exc:  # noqa: BLE001 - a DB hiccup at startup must not prevent the app from serving
        log_error("web.startup", f"Failed to ensure SGX settlement history schema: {exc}")
    # Best-effort, non-blocking: reuse today's Dividend Watchlist if it was
    # already generated, otherwise kick off generation in a background
    # thread so startup never waits on live HKEX search + LLM calls. A
    # one-shot kickoff, not a recurring scheduler -- see
    # monitor.watchlist.trigger_background_generate.
    watchlist.trigger_background_generate()
    # HKEXnews regenerates the board meetings report roughly daily -- a
    # long-running process could otherwise keep serving whatever this
    # module's in-process cache last held from a previous run. Force a
    # fresh fetch in the background on every (re)start rather than waiting
    # for the first request to pay that latency.
    board_meetings.trigger_background_refresh()
    yield


app = FastAPI(title="HKEX Dividend Monitor", lifespan=_lifespan)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---- Status ----


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    cfg = get_config()
    db_ok = db_health()

    heartbeat_iso: Optional[str] = None
    heartbeat_healthy = False
    if HEARTBEAT_FILE.exists():
        try:
            raw = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
            hb_time = datetime.fromisoformat(raw)
            age = (datetime.now(hb_time.tzinfo) - hb_time).total_seconds()
            heartbeat_healthy = age < (2 * cfg.poll_interval_seconds)
            heartbeat_iso = raw
        except (ValueError, OSError):
            pass

    targets = registry.load()
    nc = notified_cache.load()
    active_targets = [t for t in targets if t["status"] == "active"]

    return {
        "database_healthy": db_ok,
        "daemon_heartbeat": heartbeat_iso,
        "daemon_healthy": heartbeat_healthy,
        "notification_channels": configured_channels(),
        "llm_key_configured": bool(cfg.deepseek_api_key),
        "targets_total": len(targets),
        "targets_active": len(active_targets),
        "alerts_sent": len(nc["notified"]),
        "processed_no_alert": len(nc["processed"]),
        "pending_retries": len(nc["failed"]),
        "racing_tickers": sorted({t["ticker"] for t in racing_targets(active_targets)}),
        "channel_health": channel_health.load(),
        # "injected" = outbound HTTPS trusts the OS cert store (works behind a
        # TLS-inspecting corporate proxy); anything else = plain certifi. See
        # monitor._ssl_bootstrap.
        "tls_trust_store": trust_store_status(),
    }


# ---- Targets ----


class AddTargetRequest(BaseModel):
    ticker: str
    target_date: str


@app.get("/api/targets")
def api_list_targets() -> list[dict[str, Any]]:
    """Each active target is annotated with a computed `match_status`
    (upcoming/racing/today/seen/pending) so the Watchlist tab can show
    whether a past-date target actually matched anything, instead of that
    being silently indistinguishable from "still fine". See
    monitor.daemon.target_match_status."""
    targets = registry.load()
    today = datetime.now(HKT).date()
    active = [t for t in targets if t.get("status") == "active"]
    racing = {t["ticker"] for t in racing_targets(active)}
    dividend_records = dividend_store.load()

    enriched = []
    for t in targets:
        entry = dict(t)
        if t.get("status") == "active":
            entry["match_status"] = target_match_status(
                t["ticker"], date.fromisoformat(t["target_date"]), today, racing, dividend_records
            )
        else:
            entry["match_status"] = "inactive"
        enriched.append(entry)
    return enriched


@app.post("/api/targets")
def api_add_target(body: AddTargetRequest) -> dict[str, Any]:
    try:
        result = registry.add_target(body.ticker, body.target_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event("web.targets", "target.change", f"Watch target added: {body.ticker} for {body.target_date}", ticker=body.ticker)
    return result


@app.delete("/api/targets/{ticker}")
def api_remove_target(ticker: str) -> dict[str, Any]:
    try:
        removed = registry.remove_target(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"No target found for ticker {ticker}")
    log_event("web.targets", "target.change", f"Watch target removed: {ticker}", ticker=ticker)
    return {"removed": removed}


@app.get("/api/resolve-ticker/{ticker}")
def api_resolve_ticker(ticker: str) -> dict[str, Any]:
    """Resolve an HKEX stock code to its company name, so the Watchlist
    tab's add-target form can show "-> Tencent Holdings Limited" before the
    user submits -- a typo'd or wrong ticker previously had no feedback
    loop at all; it would just silently watch nothing meaningful."""
    try:
        code = normalize_ticker(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        info = lookup_stock_id(code)
    except HKEXSearchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"code": code, "name": info.get("name")}


# ---- Alerts ----


@app.get("/api/alerts")
def api_alerts(limit: int = 50) -> list[dict[str, Any]]:
    return alert_history.recent(limit=limit)


# ---- Activity log ----


@app.get("/api/activity")
def api_activity(limit: int = 200, level: Optional[str] = None) -> dict[str, Any]:
    """Live feed of monitor operations (see monitor.activity) for the
    Dashboard's Activity Log panel -- HKEX refreshes, parsing, LLM
    classification, notification decisions, retries. An invalid `level`
    is treated as "no filter" rather than a 400, since this is a polling
    endpoint the dashboard hits every few seconds."""
    limit = max(1, min(limit, 500))
    return {"events": read_recent(limit=limit, min_level=level)}


# ---- Dividends ----


@app.get("/api/dividends")
def api_dividends(limit: int = 100) -> list[dict[str, Any]]:
    return dividend_store.recent(limit=limit)


@app.get("/api/latest-filings")
def api_latest_filings(limit: int = 20, days: int = 1) -> dict[str, Any]:
    """The newest filings across ALL HKEX-listed companies -- every filing
    regardless of title, straight from the JSON feed behind HKEXnews'
    "Latest Listed Company Information" front page, for the Dividends tab's
    overview table.

    Replaces the old dividend-title-keyword overview (/api/market-dividends):
    that endpoint's marketwide title search proved to silently miss recent
    filings (observed live returning 34 records for a day the front page
    showed 373, including 0 of its dividend filings), and a title filter
    can't see a dividend declared inside e.g. an interim-results
    announcement anyway. See monitor.hkex_search.fetch_latest_filings.
    """
    limit = max(1, min(limit, LATEST_FILINGS_MAX_LIMIT))
    try:
        records = fetch_latest_filings(limit=limit, days=days)
    except HKEXSearchError as exc:
        # Upstream HKEX outage/format change -- surface as a bad-gateway rather
        # than a 500, so the UI can say "couldn't reach HKEX" specifically.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Best-effort ingest so anything shown here is immediately queryable by
    # the chat assistant -- but a DB hiccup must not blank a list HKEX
    # itself served fine.
    try:
        upsert_filing_metadata(records)
    except Exception as exc:  # noqa: BLE001
        log_error("web.latest_filings", f"Failed to ingest latest filings: {exc}")

    return {
        "days": 7 if days == 7 else 1,
        "showing": len(records),
        "filings": [
            {
                "filingId": r["filingId"],
                "date": r["date"],
                "dateTime": r["dateTime"],
                "stockCode": r["stockCode"],
                "stockName": r["stockName"],
                "title": r["title"],
                "category": r.get("category"),
                "documentUrl": r["link"],
            }
            for r in records
        ],
    }


# ---- Dividend Watchlist ----


@app.get("/api/watchlist")
def api_watchlist() -> dict[str, Any]:
    """Today's HKEX Dividend Watchlist ranking (see monitor.watchlist),
    scoped to the user's own tickers -- monitor.registry.WatchlistTickers
    plus whatever's on the alert Watchlist.

    Reuses the persisted ranking if today's has already been generated;
    otherwise kicks off best-effort background generation and reports
    status="generating" so the dashboard can poll instead of blocking this
    request on live HKEX search + LLM calls.
    """
    today = datetime.now(HKT).date()
    try:
        cached = history.load_watchlist(today)
    except SurrealDBError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if cached is not None:
        return {"status": "ready", "generatedAt": cached["generatedAt"], "rows": cached["rows"]}

    if not watchlist.has_tracked_tickers():
        # Nothing tracked yet -- an empty ranking never persists a row for
        # today (there's nothing to write one for), so without this the
        # dashboard would show "generating" forever instead of "add a
        # ticker". Report ready-and-empty immediately; no need to spin up
        # a background thread that would just persist zero rows.
        return {"status": "ready", "generatedAt": None, "rows": []}

    watchlist.trigger_background_generate()
    return {"status": "generating", "generatedAt": None, "rows": []}


@app.post("/api/watchlist/refresh")
def api_watchlist_refresh() -> dict[str, Any]:
    """Force-regenerate today's watchlist synchronously. Safe to press
    repeatedly: monitor.history.save_watchlist does a deterministic
    DELETE-then-insert keyed by (date, ticker), so this never creates
    duplicate rows."""
    try:
        return watchlist.get_or_generate_today(force=True)
    except (SurrealDBError, HKEXSearchError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class AddWatchlistTickerRequest(BaseModel):
    ticker: str


@app.get("/api/watchlist/tickers")
def api_list_watchlist_tickers() -> list[dict[str, Any]]:
    return watchlist_tickers.load()


@app.post("/api/watchlist/tickers")
def api_add_watchlist_ticker(body: AddWatchlistTickerRequest) -> dict[str, Any]:
    """Add a ticker to the Dividend Watchlist's ranked universe. Resolves
    a display name best-effort (like /api/resolve-ticker) so the dashboard
    can show a company name immediately rather than waiting for the next
    generation to discover it from a filing -- an unresolvable ticker is
    still added (name left null), since a temporary HKEX lookup hiccup
    shouldn't block the user from tracking a ticker they typed correctly.
    """
    try:
        code = normalize_ticker(body.ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    name = None
    try:
        name = lookup_stock_id(code).get("name")
    except HKEXSearchError:
        pass
    return watchlist_tickers.add(code, name)


@app.delete("/api/watchlist/tickers/{ticker}")
def api_remove_watchlist_ticker(ticker: str) -> dict[str, Any]:
    try:
        removed = watchlist_tickers.remove(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"No watchlist ticker found for {ticker}")
    return {"removed": removed}


@app.get("/api/board-meetings")
def api_board_meetings(
    ticker: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    dividend_only: bool = False,
    refresh: bool = False,
) -> dict[str, Any]:
    """HKEXnews' own consolidated, market-wide list of upcoming board
    meeting notices (see monitor.board_meetings) -- forward-looking only,
    roughly the next 6-7 weeks of currently-filed notices, not scoped to
    any tracked ticker."""
    try:
        data = board_meetings.fetch_board_meetings(force=refresh)
    except board_meetings.BoardMeetingsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    rows = board_meetings.filter_board_meeting_rows(
        data["rows"], ticker=ticker, date_from=date_from, date_to=date_to, dividend_only=dividend_only
    )
    return {
        "asOf": data["asOf"],
        "generatedDate": data["generatedDate"],
        "sourceUrl": data["sourceUrl"],
        "rows": rows,
    }


# ---- Settings ----


class SettingsUpdate(BaseModel):
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_model: Optional[str] = None
    deepseek_reasoning_model: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    poll_interval_seconds: Optional[int] = None
    scrape_lookback_days: Optional[int] = None
    max_extraction_retries: Optional[int] = None
    race_poll_interval_seconds: Optional[int] = None
    race_start_hour: Optional[int] = None
    race_end_hour: Optional[int] = None
    race_alert_failure_threshold: Optional[int] = None
    race_alert_cooldown_seconds: Optional[int] = None
    bloomberg_enabled: Optional[int] = None
    bloomberg_bridge_url: Optional[str] = None
    bloomberg_token: Optional[str] = None
    chat_daily_message_limit: Optional[int] = None


@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    return masked_settings()


@app.post("/api/settings")
def api_save_settings(body: SettingsUpdate) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    save_settings(updates)
    if updates:
        # Key names only, never values -- several of these are secrets
        # (API keys, webhook URLs).
        log_event("web.settings", "settings.save", f"Settings saved: {', '.join(sorted(updates))}")
    return masked_settings()


# ---- Test alert ----


@app.post("/api/test-alert")
def api_test_alert() -> dict[str, Any]:
    channels = configured_channels()
    if not channels:
        log_event("web.test", "notify.test", "Test alert requested but no notification channels are configured", level="warn")
        raise HTTPException(status_code=400, detail="No notification channels configured yet.")

    payload = AlertPayload(
        ticker="00700",
        company_name="Example Holdings Limited (TEST)",
        payout_amount="HKD 0.45 per share",
        ex_dividend_date="2026-08-01",
        payment_date="2026-08-15",
        source_url="https://example.com/test-filing.pdf",
    )
    results = dispatch_alert(payload)
    succeeded = [ch for ch, ok in results.items() if ok]
    log_event(
        "web.test", "notify.test",
        f"Test alert sent via {', '.join(succeeded)}" if succeeded else "Test alert failed on all channels",
        level="info" if succeeded else "warn",
    )
    return {"results": results, "any_succeeded": any(results.values())}


@app.post("/api/test-deepseek")
def api_test_deepseek() -> dict[str, Any]:
    """Actually exercise the configured DeepSeek key/base URL/model with a
    minimal real API call, rather than just checking a key string is
    present (see monitor.extractor.test_deepseek_connection)."""
    try:
        test_deepseek_connection()
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


# ---- Chat ----


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] = []


@app.post("/api/chat")
def api_chat(body: ChatRequest) -> dict[str, Any]:
    try:
        return run_chat_turn(body.history, body.message)
    except ChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ChatFeedbackRequest(BaseModel):
    message: str
    reply: str
    note: Optional[str] = None
    tool_activity: list[dict[str, Any]] = []
    prior_transcript: list[dict[str, Any]] = []


@app.post("/api/chat/feedback")
def api_chat_feedback(body: ChatFeedbackRequest) -> dict[str, Any]:
    """Record one thumbs-downed chat turn (see monitor.chat_feedback).
    Returns the updated stats so the UI can show the new count without a
    second request."""
    if not body.message.strip() or not body.reply.strip():
        raise HTTPException(status_code=400, detail="message and reply are both required")
    try:
        chat_feedback.record_feedback(
            body.message,
            body.reply,
            note=body.note,
            tool_activity=body.tool_activity,
            prior_transcript=body.prior_transcript,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write the feedback log: {exc}") from exc
    log_event("web.chat", "chat.feedback", "Assistant reply flagged for review")
    return {"recorded": True, **chat_feedback.feedback_stats()}


@app.get("/api/chat/feedback")
def api_chat_feedback_stats() -> dict[str, Any]:
    return chat_feedback.feedback_stats()


@app.get("/api/chat/feedback/download")
def api_chat_feedback_download() -> FileResponse:
    if not chat_feedback.feedback_stats()["count"]:
        raise HTTPException(status_code=404, detail="No flagged chat replies recorded yet")
    return FileResponse(
        chat_feedback.CHAT_FEEDBACK_FILE,
        media_type="application/x-ndjson",
        filename="chat_feedback.log",
    )


@app.delete("/api/chat/feedback")
def api_chat_feedback_clear() -> dict[str, Any]:
    removed = chat_feedback.clear_feedback()
    if removed:
        log_event("web.chat", "chat.feedback", f"Cleared {removed} flagged chat repl{'y' if removed == 1 else 'ies'}")
    return {"removed": removed}


# ---- Settlement Prices (HKEX / SGX / Eurex) ----
#
# All three fetch/parse live pages or files from the respective exchange --
# deterministic (regex/openpyxl/JSON navigation), no LLM involved anywhere
# in this section. See monitor.settlement's module docstring for how each
# source was reverse-engineered.


@app.get("/api/settlement/hkex")
def api_settlement_hkex(refresh: bool = False) -> dict[str, Any]:
    """HKEX Final Settlement Prices -- roughly a year of history across
    every listed futures/options contract in one call."""
    try:
        return settlement.fetch_hkex_fsp(force=refresh)
    except SettlementError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/settlement/sgx")
def api_settlement_sgx(refresh: bool = False) -> dict[str, Any]:
    """Today's SGX-DC Final Settlement Price workbook (Financials +
    Commodities Contracts sheets) plus the latest FlexC file."""
    try:
        main = settlement.fetch_sgx_fsp(force=refresh)
        flexc = settlement.fetch_sgx_flexc(force=refresh)
    except SettlementError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"asOf": main["asOf"], "sourceFileUrl": main["sourceFileUrl"], "rows": main["rows"], "flexc": flexc}


@app.get("/api/settlement/sgx/daily")
def api_settlement_sgx_daily(
    date: str = Query(...),
    search: Optional[str] = None,
    contract_month: Optional[str] = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Daily settlement marks (like Eurex's D. Settle) for every SGX
    futures contract month on one trading day, straight from SGX's own
    public per-business-day archive -- any date since 2018-01-19 (the
    modern column format's start; SGX's own archive holds older files too,
    but this app doesn't parse their layout -- see monitor.sgx_daily),
    independent of this app's own SurrealDB archive (see
    monitor.settlement_history for that narrower, final-settlement-only,
    app-uptime-bounded alternative). A date that isn't a trading day (or
    isn't published yet) is reported as a normal 200 with an empty row
    list and an explanatory `note`, not an error -- a holiday is expected
    input, not a failure. A date within the supported range but before the
    modern format started (2013-2018) gets the same soft-200 treatment,
    since that's also an app limitation rather than an upstream error.
    """
    # `date` (the query param, a str) shadows the `date` class imported at
    # module level for the rest of this function's body -- use the `_date`
    # alias (imported solely for this) instead of the shadowed name.
    try:
        trade_date = _date.fromisoformat(date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date {date!r}, expected YYYY-MM-DD") from exc

    try:
        data = sgx_daily.fetch_sgx_daily(trade_date, force=refresh)
    except sgx_daily.SGXDailyNotAvailable as exc:
        return {"tradeDate": trade_date.isoformat(), "sourceFileUrl": None, "rows": [], "note": str(exc)}
    except sgx_daily.SGXDailyFormatUnsupported as exc:
        return {"tradeDate": trade_date.isoformat(), "sourceFileUrl": None, "rows": [], "note": str(exc)}
    except SettlementError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    rows = sgx_daily.filter_daily_rows(data["rows"], ticker=search, contract_month=contract_month)
    return {"tradeDate": data["tradeDate"], "sourceFileUrl": data["sourceFileUrl"], "rows": rows}


@app.get("/api/settlement/eurex/products")
def api_settlement_eurex_products() -> dict[str, Any]:
    """The Eurex product catalog for the dashboard's product picker, each
    entry annotated with whether it already has a resolved internal
    product id (seeded, or previously resolved via the resolve endpoint
    below) -- codes without one need a one-time page-URL resolve first."""
    try:
        products = settlement.fetch_eurex_products()
    except SettlementError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    for p in products:
        p["resolved"] = settlement.resolve_eurex_product_id(p["code"]) is not None
    return {"products": products}


@app.get("/api/settlement/eurex")
def api_settlement_eurex(product: str, busdate: Optional[str] = None, refresh: bool = False) -> dict[str, Any]:
    """Daily settlement prices for one Eurex product code (e.g. "FDAX"),
    as of `busdate` (Eurex's own YYYYMMDD format; defaults to its latest
    business date). 404s with a hint toward the resolve endpoint if the
    code's internal product id -- required by Eurex's own API, and not
    the same as the public product code -- hasn't been resolved yet."""
    code = product.strip().upper()
    product_id = settlement.resolve_eurex_product_id(code)
    if product_id is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"'{code}' hasn't been resolved to a Eurex product id yet. "
                "Paste that product's Eurex page URL via POST /api/settlement/eurex/resolve."
            ),
        )
    try:
        return settlement.fetch_eurex_settlement(product_id, busdate=busdate, force=refresh)
    except SettlementError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class EurexResolveRequest(BaseModel):
    code: str
    page_url: str


@app.post("/api/settlement/eurex/resolve")
def api_settlement_eurex_resolve(body: EurexResolveRequest) -> dict[str, Any]:
    """Resolve and persist a Eurex product code's internal numeric product
    id from its public product page URL -- the one-time step needed for
    any code not already covered by monitor.settlement's seed map."""
    try:
        product_id = settlement.resolve_eurex_product_id_from_url(body.code, body.page_url)
    except SettlementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        "web.settlement", "settlement.eurex_resolve",
        f"Resolved Eurex product {body.code} -> internal id {product_id}",
    )
    return {"code": body.code.strip().upper(), "productId": product_id}


@app.get("/api/settlement/eurex/msci")
def api_settlement_eurex_msci(refresh: bool = False) -> dict[str, Any]:
    """Eurex MSCI Futures final settlement prices, one row per index with
    a settlementPricesByExpiry breakdown, plus the latest expiry column
    that actually has values (a sensible default for the dashboard)."""
    try:
        data = settlement.fetch_eurex_msci_fsp(force=refresh)
    except SettlementError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    data["defaultExpiry"] = settlement.latest_populated_msci_expiry(data["rows"], data["expiries"])
    return data
