"""HKEXnews' "Announcements of Board Meetings" report -- a market-wide,
forward-looking calendar of upcoming board meetings, many of which will
consider a dividend/distribution (HKEX Listing Rule 13.43 requires a
meeting to give >=7 clear business days' notice when it will consider
one). Distinct from monitor.watchlist's company_event pipeline: that one
is per-TRACKED-ticker, LLM-extracted from each ticker's own "board
meeting" filing; this is one deterministic HTTP fetch covering EVERY
HKEX-listed company market-wide, with a machine-parseable Purpose column,
no LLM involved.

Live-verified (2026-07-17): this page has no JSON API, no date/ticker
filter parameters, and no pagination -- one fixed URL, server-rendered
HTML, ~60 rows spanning roughly the next 6-7 weeks. HKEX regenerates it
roughly once a day (its own "Date :" line and the HTTP Last-Modified
header both lag the fetch date by about a day), so a build cached across
an entire long-running process would go stale -- refreshed at web app
startup (see monitor.web's lifespan hook) on top of a several-hour
in-process cache, so repeated chat questions/tab loads within the same
session don't re-hit HKEX for no reason, but a fresh app start (or a
dashboard Refresh click) always sees HKEX's latest filed notices.

Row shape, once parsed: {"bmDate": "2026-07-17", "stockName":
"CHINA PPT INV", "stockCode": "00736", "purpose": "FIN RES", "period":
"Y.E.31/03/26", "likelyDividend": False}. `purpose` is HKEX's own raw
abbreviation (no published fixed enum -- observed values include
"FIN RES", "INT RES/DIV", "SPECIAL DIVIDEND", "RESULTS/INT DIV",
"2ND QUARTER RES") and is passed through verbatim, never paraphrased;
`likelyDividend` is a conservative, purely mechanical flag (does the raw
Purpose text contain "DIV") -- NOT a claim that a dividend will
definitely be declared, just that the notice itself says the meeting will
consider one.
"""
from __future__ import annotations

import html
import re
import threading
from datetime import datetime
from typing import Any, Optional

import requests

from monitor import settlement
from monitor.diagnostics import log_error
from monitor.registry import normalize_ticker

BOARD_MEETINGS_URL = "https://www3.hkexnews.hk/reports/bmn/ebmn.htm"

# HKEX regenerates this report roughly once a day (verified live: the
# page's own "Date :" line and its HTTP Last-Modified header both lag the
# fetch date by about a day) -- long enough that this cache barely ever
# serves genuinely stale data even at the full TTL, short enough that a
# long-running process still picks up a same-day correction. App startup
# forces a fresh fetch on top of this regardless (see monitor.web).
_BOARD_MEETINGS_TTL_SECONDS = 4 * 3600.0


class BoardMeetingsError(settlement.SettlementError):
    """Distinct name for clearer log/error messages, but deliberately a
    SettlementError subclass -- callers that already catch that broadly
    (chat tool dispatch, the web endpoint's error mapping) handle this
    correctly with no special-casing needed."""


# The page's own "as of" line, e.g. "Date : 16/07/2026" -- distinct from
# this app's own fetch time (asOf below), same distinction as HKEX FSP's
# dataGeneratedAt vs asOf.
_GENERATED_DATE_RE = re.compile(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})")

# Each data row is six <td><font ...>CONTENT</font></td> cells in a row:
# BM Date, an always-blank spacer, Stock Short Name, Code, Purpose,
# Period -- verified against the page's real HTML (view-source), not
# guessed. The header and separator ("----------") rows share this exact
# six-cell shape too; they're excluded by validating the first cell looks
# like a DD/MM/YYYY date, not by trying to match the regex more narrowly
# (the header row's HTML is malformed -- one of its <font> tags is never
# closed -- so it simply fails to match at all, which is what we want).
_ROW_RE = re.compile(
    r"<tr>\s*"
    r"<td[^>]*><font[^>]*>([^<]*)</font></td>\s*"  # BM Date
    r"<td[^>]*><font[^>]*>([^<]*)</font></td>\s*"  # spacer (unused)
    r"<td[^>]*><font[^>]*>([^<]*)</font></td>\s*"  # Stock Short Name
    r"<td[^>]*><font[^>]*>([^<]*)</font></td>\s*"  # Code
    r"<td[^>]*><font[^>]*>([^<]*)</font></td>\s*"  # Purpose
    r"<td[^>]*><font[^>]*>([^<]*)</font></td>\s*"  # Period
    r"</tr>",
    re.IGNORECASE,
)
_BM_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _ddmmyyyy_to_iso(value: str) -> Optional[str]:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _clean_cell(raw: str) -> str:
    """Decode HTML entities (the Code column is &nbsp;-padded for
    fixed-width display) and trim -- _strip_tags elsewhere in this app
    doesn't decode entities, which this page specifically needs."""
    return html.unescape(raw).strip()


def parse_board_meetings_html(text: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Returns (rows, generatedDateIso). Skips the header/separator rows
    (same six-cell shape as data rows, but their first cell isn't a real
    date) rather than assuming a fixed number of non-data rows up front,
    so this stays correct even if HKEX ever adds/removes a boilerplate
    row above the data."""
    m = _GENERATED_DATE_RE.search(text)
    generated_date = _ddmmyyyy_to_iso(m.group(1)) if m else None

    rows: list[dict[str, Any]] = []
    for match in _ROW_RE.finditer(text):
        bm_date_raw, _spacer, name_raw, code_raw, purpose_raw, period_raw = (
            _clean_cell(g) for g in match.groups()
        )
        if not _BM_DATE_RE.match(bm_date_raw):
            continue  # header/separator row, or something unparseable -- not real data
        bm_date = _ddmmyyyy_to_iso(bm_date_raw)
        if bm_date is None:
            continue
        try:
            stock_code = normalize_ticker(code_raw)
        except ValueError:
            continue  # no digits at all -- not a real row
        purpose = purpose_raw.upper()
        rows.append(
            {
                "bmDate": bm_date,
                "stockName": name_raw,
                "stockCode": stock_code,
                "purpose": purpose,
                "period": period_raw or None,
                "likelyDividend": "DIV" in purpose,
            }
        )
    return rows, generated_date


def _fetch_board_meetings_impl() -> dict[str, Any]:
    try:
        resp = requests.get(BOARD_MEETINGS_URL, headers=settlement._HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise BoardMeetingsError(f"HKEXnews board meetings page fetch failed: {exc}") from exc

    rows, generated_date = parse_board_meetings_html(resp.text)
    if not rows:
        # A page that fetched fine but yielded zero rows almost certainly
        # means the HTML structure changed (this parser is regex-matched
        # against today's exact markup, not a general HTML table parser)
        # -- surface as a fetch/parse problem, not "no board meetings are
        # scheduled anywhere on HKEX for the next ~7 weeks", which would
        # never genuinely happen.
        raise BoardMeetingsError(
            "HKEXnews board meetings page returned no parseable rows -- its layout may have changed"
        )
    return {
        "asOf": datetime.now(settlement.HKT).isoformat(),
        "generatedDate": generated_date,
        "sourceUrl": BOARD_MEETINGS_URL,
        "rows": rows,
    }


def fetch_board_meetings(force: bool = False) -> dict[str, Any]:
    """Every upcoming HKEX board meeting currently on file (market-wide,
    not scoped to any tracked ticker) -- see module docstring for shape,
    freshness, and how this differs from the watchlist's own board-meeting
    signal. Cached a few hours; pass force=True to bypass (dashboard
    Refresh button, and this app's own startup refresh)."""
    return settlement._cached_fetch(
        "board_meetings", force, _BOARD_MEETINGS_TTL_SECONDS, _fetch_board_meetings_impl
    )


def filter_board_meeting_rows(
    rows: list[dict[str, Any]],
    ticker: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    dividend_only: bool = False,
) -> list[dict[str, Any]]:
    """Narrow a fetch_board_meetings() row list. `date_from`/`date_to` are
    inclusive ISO "YYYY-MM-DD" bounds; either may be given alone."""
    out = rows
    if ticker:
        try:
            wanted = normalize_ticker(str(ticker))
        except ValueError:
            return []
        out = [r for r in out if r.get("stockCode") == wanted]
    if date_from:
        out = [r for r in out if (r.get("bmDate") or "") >= date_from]
    if date_to:
        out = [r for r in out if (r.get("bmDate") or "") <= date_to]
    if dividend_only:
        out = [r for r in out if r.get("likelyDividend")]
    return out


def trigger_background_refresh() -> None:
    """Force a fresh fetch in a background thread, never blocking the
    caller -- used by web.py's startup hook so a long-running process
    always picks up HKEX's latest notices the moment it (re)starts,
    rather than potentially serving whatever this module's own in-process
    cache last held from a previous run's final minutes. Mirrors
    monitor.watchlist.trigger_background_generate's pattern: fire-and-
    forget, broad exception guard so a fetch failure here can never crash
    the process it's trying to warm up."""

    def _run() -> None:
        try:
            fetch_board_meetings(force=True)
        except Exception as exc:  # noqa: BLE001 - background thread must never crash the process
            log_error("board_meetings.background", f"Startup refresh failed: {exc}", exc)

    threading.Thread(target=_run, name="board-meetings-refresh", daemon=True).start()
