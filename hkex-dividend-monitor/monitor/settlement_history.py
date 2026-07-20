"""Daily SGX settlement-price archive in SurrealDB.

Unlike HKEX (serves ~1 year of history natively) and Eurex (its statistics
API keeps a rolling window of currently-live contract months), SGX's daily
FSP workbook is a CURRENT snapshot only, not a history -- each ticker's row
reflects whatever its most recent published settlement is (usually its
current front-month contract), and that date varies per ticker: verified
live, a single fetch's ~230 rows span fspDates from days to several weeks
apart, not uniformly "today". Once SGX republishes the file, a ticker's
earlier row is simply gone -- there's no way to ask SGX's own site for a
specific PAST date's figure. monitor.settlement's fetchers are purely
on-demand with only a short in-process cache, so nothing persists this
data. This module archives it so real history accumulates day over day,
independent of whether anyone opens the app that day.

One table, `sgx_settlement_history`, covers both files SGX publishes daily
(the main Financials/Commodities workbook and the FlexC flexible-FX file),
distinguished by a `source` field -- they're structurally the same shape
(ticker/contract/fsp/date), so two tables would just be duplicated schema.

Follows monitor.history's established pattern exactly: SCHEMALESS (this
repo owns no migration tool), string-interpolated SurrealQL, UPSERT keyed
by a deterministic id so re-archiving the same day's data (daemon tick,
dashboard Refresh, a chat question) is a no-op rather than a duplicate.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Optional

from monitor import settlement
from monitor.db import _escape_sql_string, query
from monitor.history import _sql_literal

_MAIN_SOURCE = "main"
_FLEXC_SOURCE = "flexc"


def ensure_schema() -> None:
    """Idempotent -- safe to call on every daemon loop and web app startup,
    mirroring monitor.history.ensure_schema."""
    query("DEFINE TABLE IF NOT EXISTS sgx_settlement_history SCHEMALESS;")
    _migrate_slash_dates()


def _migrate_slash_dates() -> None:
    """One-time cleanup for rows archived before FlexC's MM/DD/YYYY dates
    were normalized to ISO at the parse boundary (see
    monitor.settlement._normalize_flexc_date) -- a pre-existing archived
    row can still carry the old slash-formatted fspDate verbatim, which
    breaks history_for_date's exact-match lookup outright and skews
    history_for_ticker's ORDER BY fspDate string sort across year/month
    boundaries. Idempotent and self-contained: both callers already wrap
    ensure_schema() broadly (a DB hiccup at startup must never block the
    app), so a failure here just means the same rows get retried on the
    next startup rather than corrupting anything -- no explicit try/except
    needed inside this function itself. Once every row is ISO, the SELECT
    below finds nothing and every subsequent call is a single cheap no-op.
    """
    rows = query("SELECT * FROM sgx_settlement_history WHERE fspDate CONTAINS '/';")
    if not rows:
        return

    statements: list[str] = []
    for row in rows:
        old_fsp_date = row.get("fspDate")
        ticker = row.get("ticker")
        source = row.get("source")
        if not old_fsp_date or not ticker or not source:
            continue
        new_fsp_date = settlement._normalize_flexc_date(old_fsp_date)
        if new_fsp_date == old_fsp_date:
            continue  # contained '/' but didn't parse as MM/DD/YYYY -- leave it alone
        old_id = _row_id(old_fsp_date, ticker, source)
        new_id = _row_id(new_fsp_date, ticker, source)
        # Carry every field over as-is except the corrected date -- this
        # row was already fully shaped by _archive_rows at write time, so
        # there's nothing else here that needs re-deriving.
        fields = {k: v for k, v in row.items() if k not in ("id", "archivedAt")}
        fields["fspDate"] = new_fsp_date
        set_clause = ", ".join(f"{k} = {_sql_literal(v)}" for k, v in fields.items())
        statements.append(
            f"UPSERT sgx_settlement_history:{new_id} SET {set_clause}, archivedAt = time::now() RETURN NONE;"
        )
        statements.append(f"DELETE sgx_settlement_history:{old_id};")

    if statements:
        query("\n".join(statements))


def _row_id(fsp_date: str, ticker: str, source: str) -> str:
    """Deterministic id from (fspDate, ticker, source) -- NOT "today", since
    the row's own fspDate is the settlement date it actually belongs to.
    This is what makes archiving idempotent: fetching/archiving the same
    day's data any number of times just re-UPSERTs the same row."""
    return hashlib.md5(f"{fsp_date}|{ticker}|{source}".encode()).hexdigest()[:16]


def _ticker_components(ticker: str) -> list[str]:
    """SGX combines related tickers into one compound field like "NK/NKO"
    (same pattern as HKEX's compound HKATS codes -- see
    monitor.settlement._hkats_components) -- split so an exact-code lookup
    on just "NK" still finds a row archived under "NK/NKO"."""
    return [c.strip().upper() for c in ticker.split("/") if c.strip()]


def _archive_rows(rows: list[dict[str, Any]], source: str, extra_fields: tuple[str, ...]) -> list[str]:
    statements = []
    for row in rows:
        fsp_date = row.get("fspDate")
        ticker = row.get("ticker")
        if not fsp_date or not ticker:
            continue  # nothing stable to key an archive row on
        row_id = _row_id(fsp_date, ticker, source)
        fields: dict[str, Any] = {
            "source": source,
            "ticker": ticker,
            "tickerComponents": _ticker_components(ticker),
            "fspDate": fsp_date,
            "fsp": row.get("fsp"),
        }
        for field in extra_fields:
            fields[field] = row.get(field)
        set_clause = ", ".join(f"{k} = {_sql_literal(v)}" for k, v in fields.items())
        statements.append(f"UPSERT sgx_settlement_history:{row_id} SET {set_clause}, archivedAt = time::now() RETURN NONE;")
    return statements


def archive_sgx_snapshot(main_rows: list[dict[str, Any]], flexc_rows: list[dict[str, Any]]) -> int:
    """Archive fetch_sgx_fsp()'s and fetch_sgx_flexc()'s row lists into
    sgx_settlement_history. Returns how many rows were archived (rows
    missing fspDate/ticker are skipped, not errored -- there's nothing
    stable to key them on). A single batched multi-statement query, like
    monitor.history.save_watchlist."""
    statements = _archive_rows(main_rows, _MAIN_SOURCE, ("sheet", "productType", "contract", "contractMonth"))
    statements += _archive_rows(flexc_rows, _FLEXC_SOURCE, ())
    if not statements:
        return 0
    query("\n".join(statements))
    return len(statements)


def history_for_ticker(ticker: str, source: Optional[str] = None, limit: int = 90) -> list[dict[str, Any]]:
    """Archived rows for one ticker, newest fspDate first. Matches against
    tickerComponents (see _ticker_components) so "NK" finds a row archived
    under a compound ticker like "NK/NKO" -- and, symmetrically, so typing
    the compound form itself ("NK/NKO") also finds it: the input is split
    into the same components and OR'd, since a caller (a chat model reusing
    exactly what a prior tool result showed it) may reasonably pass either
    form."""
    components = _ticker_components(ticker) or [ticker.strip().upper()]
    code_match = " OR ".join(
        f"tickerComponents CONTAINS '{_escape_sql_string(c)}'" for c in components
    )
    clauses = [f"({code_match})"]
    if source:
        clauses.append(f"source = '{_escape_sql_string(source)}'")
    sql = (
        "SELECT * FROM sgx_settlement_history "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY fspDate DESC LIMIT {int(limit)};"
    )
    return query(sql)


def history_for_date(fsp_date: date, source: Optional[str] = None) -> list[dict[str, Any]]:
    """Every row archived for one date."""
    clauses = [f"fspDate = '{fsp_date.isoformat()}'"]
    if source:
        clauses.append(f"source = '{_escape_sql_string(source)}'")
    sql = f"SELECT * FROM sgx_settlement_history WHERE {' AND '.join(clauses)};"
    return query(sql)


def _archive_range_impl() -> Optional[tuple[str, str]]:
    rows = query(
        "SELECT fspDate FROM sgx_settlement_history ORDER BY fspDate ASC LIMIT 1; "
        "SELECT fspDate FROM sgx_settlement_history ORDER BY fspDate DESC LIMIT 1;"
    )
    if len(rows) < 2:
        return None
    earliest = rows[0].get("fspDate")
    latest = rows[1].get("fspDate")
    if not earliest or not latest:
        return None
    return earliest, latest


def archive_range(force: bool = False) -> Optional[tuple[str, str]]:
    """(earliest, latest) fspDate this app has archived, or None if the
    archive is empty. Cached briefly -- lets a caller (the chat tool) turn
    an empty history_for_ticker/history_for_date result into "the archive
    covers X..Y" / "the archive is empty" instead of a bare zero rows,
    which a model can't distinguish from "this date/ticker was simply
    never archived". Propagates SurrealDBError on a genuine DB failure
    rather than swallowing it -- a caller must not present an outage as
    if it were a real statement about archive coverage."""
    return settlement._cached_fetch("sgx_archive_range", force, 600.0, _archive_range_impl)
