import httpx
from typing import Any

HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"

# Map common headline category codes to dividend-relevant ones
DIVIDEND_HEADLINE_CATS = ["DNS", "DNI", "DNF"]  # dividend notice, interim, final


async def fetch_filings(ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    Fetch recent dividend-related filings for a ticker from HKEX news search.
    Returns a normalised list of filing dicts.
    """
    # Strip the .HK suffix for HKEX queries if present
    stock_code = ticker.replace(".HK", "").lstrip("0") or "0"

    params = {
        "lang":        "EN",
        "category":    "0",
        "market":      "SEHK",
        "searchType":  "1",
        "t1code":      "40000",   # corporate actions
        "t2Gcode":     "-2",
        "t2code":      "DNS",     # dividend / distribution notice
        "stockId":     stock_code,
        "from":        "",
        "to":          "",
        "MB-Annquery": "hkexnews",
        "rowRange":    str(limit),
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(HKEX_SEARCH_URL, params=params)
        resp.raise_for_status()

    # HKEX returns HTML — parse with BeautifulSoup
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for row in soup.select("table.table tr")[1:limit + 1]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link_tag = cells[2].find("a")
        results.append({
            "id":           cells[0].get_text(strip=True),
            "published_at": cells[1].get_text(strip=True),
            "title":        cells[2].get_text(strip=True),
            "url":          f"https://www1.hkexnews.hk{link_tag['href']}" if link_tag else None,
            "ticker":       ticker,
        })

    return results
