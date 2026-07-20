import os
import json
import httpx
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.getenv("LLM_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

EXTRACTION_PROMPT = """Extract dividend details from the following HKEX filing text.
Return ONLY valid JSON with these keys (use null if not found):
{
  "dividend_type": "interim|final|special",
  "amount": <number>,
  "currency": "HKD",
  "ex_date": "YYYY-MM-DD",
  "record_date": "YYYY-MM-DD",
  "payment_date": "YYYY-MM-DD",
  "raw_text": "<brief summary>"
}"""


async def fetch_filing_text(url: str) -> str:
    """Download and extract text from a filing URL (HTML or PDF)."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

    if "pdf" in content_type:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages[:5])
    else:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        return soup.get_text(separator="\n", strip=True)[:8000]


async def extract_dividend(filing: dict) -> dict | None:
    """Use LLM to pull structured dividend data from a filing."""
    url = filing.get("url")
    if not url:
        return None

    try:
        text = await fetch_filing_text(url)
    except Exception:
        return None

    response = await client.chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user",   "content": text},
        ],
    )

    try:
        return json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, AttributeError):
        return None
