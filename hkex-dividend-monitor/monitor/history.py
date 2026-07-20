"""Structured announcement history + generated watchlist persistence for
Today's HKEX Dividend Watchlist.

Two new SurrealDB tables, both SCHEMALESS -- this repo owns neither via a
migration tool (see monitor/db.py's module docstring re: exchange_filing
being owned by the external hkex-filing-scraper package), so keeping these
schemaless avoids inventing a migration system for two tables:

  company_event       -- one row per board-meeting/results/dividend notice
                         this app has LLM-extracted structured fields from
                         (see monitor.announcement_extractor). Builds up
                         over time as monitor.watchlist processes filings
                         for the user's chosen tickers.
  dividend_watchlist  -- one row per (date, company) in a generated
                         ranking (see monitor.watchlist). Store of record
                         so the ranking survives restarts and is queryable
                         by the chat assistant, not just a JSON cache file.

Follows monitor.db / monitor.hkex_search's existing convention of building
SurrealQL by string interpolation (no SDK) with escaped string literals.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Optional

from monitor.db import _escape_sql_string, _validate_filing_id, query


def ensure_schema() -> None:
    """Idempotent -- safe to call on every watchlist generation (and on
    every web app startup). `IF NOT EXISTS` matters here, not just as a
    style choice: a bare `DEFINE TABLE` errors with "table already exists"
    on SurrealDB if the table was already defined by an earlier call, which
    would otherwise make every call after the very first one fail."""
    query(
        "DEFINE TABLE IF NOT EXISTS company_event SCHEMALESS;\n"
        "DEFINE TABLE IF NOT EXISTS dividend_watchlist SCHEMALESS;"
    )


def _sql_literal(value: Any) -> str:
    """Render a Python value as a SurrealQL literal for direct interpolation
    into a SET clause. Extends the escaped-string-literal approach
    monitor.db/monitor.hkex_search already use to nested lists/dicts (needed
    for dividend_watchlist's `reasons` scoring breakdown) -- SurrealQL's
    object/array literal syntax is close enough to JSON that this stays
    simple, but None must render as SurrealQL's NONE, not JSON's null, and
    dates must render as SurrealQL's d'...' datetime literal.
    """
    if value is None:
        return "NONE"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, date):
        return f"d'{value.isoformat()}'"
    if isinstance(value, str):
        return f"'{_escape_sql_string(value)}'"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_sql_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        fields = ", ".join(f"'{_escape_sql_string(str(k))}': {_sql_literal(v)}" for k, v in value.items())
        return "{" + fields + "}"
    raise TypeError(f"Unsupported SurrealQL literal type: {type(value)!r}")


# ---- company_event ----


def upsert_event(record: dict[str, Any]) -> None:
    """UPSERT one row into company_event, keyed by filingId (the same
    16-hex id exchange_filing uses) so re-processing a filing -- e.g. a
    same-day watchlist refresh re-scanning the same notice -- overwrites
    rather than duplicates it.
    """
    filing_id = _validate_filing_id(record["filingId"])
    fields = {k: v for k, v in record.items() if k != "filingId"}
    set_clause = ", ".join(f"{k} = {_sql_literal(v)}" for k, v in fields.items())
    sql = (
        f"UPSERT company_event:{filing_id} SET "
        f"filingId = '{filing_id}', {set_clause}, "
        "updatedAt = time::now() "
        "RETURN NONE;"
    )
    query(sql)


def events_for_ticker(stock_code: str) -> list[dict[str, Any]]:
    """All extracted announcement-history rows for one ticker, oldest
    first -- the raw material monitor.features builds signals from."""
    sql = (
        "SELECT * FROM company_event "
        f"WHERE stockCode = '{_escape_sql_string(stock_code)}' "
        "ORDER BY announcementDate ASC;"
    )
    return query(sql)


def known_filing_ids() -> set[str]:
    """Every filingId already recorded in company_event -- lets
    monitor.watchlist skip re-downloading/re-extracting/re-classifying a
    filing it has already processed on an earlier run, so a same-day
    Refresh (or tomorrow's generation re-discovering an old notice still
    inside the lookback window) doesn't repeat LLM calls for it."""
    rows = query("SELECT filingId FROM company_event;")
    return {r["filingId"] for r in rows if r.get("filingId")}


# ---- dividend_watchlist ----


def _watchlist_row_id(watchlist_date: date, stock_code: str) -> str:
    return hashlib.md5(f"{watchlist_date.isoformat()}{stock_code}".encode()).hexdigest()[:16]


def save_watchlist(watchlist_date: date, generated_at: str, rows: list[dict[str, Any]]) -> None:
    """Replace today's dividend_watchlist rows with a freshly generated
    ranking. DELETE-then-insert (rather than UPSERT-only) so a refresh that
    drops a company that no longer qualifies doesn't leave a stale row
    behind; deterministic per-row ids (date+ticker) mean even a
    partially-applied statement batch is safe to re-run without duplicates.
    """
    statements = [f"DELETE dividend_watchlist WHERE watchlistDate = d'{watchlist_date.isoformat()}';"]
    for row in rows:
        stock_code = row["stockCode"]
        row_id = _watchlist_row_id(watchlist_date, stock_code)
        fields = dict(row)
        fields["watchlistDate"] = watchlist_date
        fields["generatedAt"] = generated_at
        set_clause = ", ".join(f"{k} = {_sql_literal(v)}" for k, v in fields.items())
        statements.append(f"UPSERT dividend_watchlist:{row_id} SET {set_clause} RETURN NONE;")
    query("\n".join(statements))


def load_watchlist(watchlist_date: date) -> Optional[dict[str, Any]]:
    """Today's persisted ranking, rank-ascending, or None if nothing has
    been generated for that date yet."""
    sql = (
        "SELECT * FROM dividend_watchlist "
        f"WHERE watchlistDate = d'{watchlist_date.isoformat()}' "
        "ORDER BY rank ASC;"
    )
    rows = query(sql)
    if not rows:
        return None
    return {"generatedAt": rows[0].get("generatedAt"), "rows": rows}


def watchlist_exists(watchlist_date: date) -> bool:
    sql = (
        "SELECT stockCode FROM dividend_watchlist "
        f"WHERE watchlistDate = d'{watchlist_date.isoformat()}' LIMIT 1;"
    )
    return bool(query(sql))


def latest_watchlist_date() -> Optional[date]:
    """The most recent date a watchlist was generated for, or None if
    dividend_watchlist is empty -- lets callers fall back to showing recent
    (stale) data if today's generation hasn't run yet."""
    sql = "SELECT watchlistDate FROM dividend_watchlist ORDER BY watchlistDate DESC LIMIT 1;"
    rows = query(sql)
    if not rows:
        return None
    raw = rows[0].get("watchlistDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None
