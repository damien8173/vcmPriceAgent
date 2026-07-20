"""Minimal SurrealDB HTTP client -- no SDK dependency, just requests.

Talks to the /sql and /health REST endpoints that every SurrealDB
instance exposes, regardless of how it was started (Docker or the
native `surreal` binary).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any

import requests

from monitor.config import HKT, get_config


class SurrealDBError(RuntimeError):
    pass


def _auth() -> tuple[str, str]:
    cfg = get_config()
    return (cfg.surreal_username, cfg.surreal_password)


def _headers() -> dict[str, str]:
    cfg = get_config()
    return {
        "Accept": "application/json",
        "Content-Type": "text/plain",
        "Surreal-NS": cfg.surreal_namespace,
        "Surreal-DB": cfg.surreal_database,
    }


def health(timeout: float = 5.0) -> bool:
    try:
        resp = requests.get(f"{get_config().surreal_endpoint}/health", timeout=timeout)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def query(sql: str, timeout: float = 30.0) -> list[Any]:
    """Execute a raw SurrealQL query via POST /sql, returning the list of result rows.

    Raises SurrealDBError on transport failure or a query-level error in the response.
    """
    try:
        resp = requests.post(
            f"{get_config().surreal_endpoint}/sql",
            data=sql.encode("utf-8"),
            headers=_headers(),
            auth=_auth(),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SurrealDBError(f"SurrealDB request failed: {exc}") from exc

    if resp.status_code != 200:
        raise SurrealDBError(f"SurrealDB returned HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise SurrealDBError(f"SurrealDB returned non-JSON response: {resp.text[:500]}") from exc

    results: list[Any] = []
    for statement in payload:
        if statement.get("status") != "OK":
            raise SurrealDBError(f"SurrealDB query error: {statement.get('result')}")
        result = statement.get("result")
        if isinstance(result, list):
            results.extend(result)
        elif result is not None:
            results.append(result)
    return results


def _escape_sql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


_FILING_ID_RE = re.compile(r"^[a-f0-9]{16}$")


def _validate_filing_id(filing_id: str) -> str:
    """filingId is always a 16-char lowercase hex md5 prefix (see the upstream
    scraper's own id generation). Enforced here because callers embed this
    value directly into a SurrealQL record-ID position (`table:{id}`, not a
    quoted string literal) -- and it can originate from LLM tool-call
    arguments, which should never be trusted into that position unescaped."""
    if not _FILING_ID_RE.match(filing_id):
        raise ValueError(f"Invalid filingId: {filing_id!r}")
    return filing_id


def _hkt_date_to_utc_iso(d: date) -> str:
    return datetime.combine(d, datetime.min.time(), tzinfo=HKT).astimezone(timezone.utc).isoformat()


def fetch_matching_filings(ticker_since: dict[str, date]) -> list[dict[str, Any]]:
    """Fetch filings for the given stock codes, each filed on/after its own
    `since` (HKT date) in `ticker_since` (ticker -> earliest date worth
    fetching for that ticker).

    Bounding each ticker by its own date -- rather than one global `since`
    for every ticker -- matters once the watchlist has more than one active
    target: a single old/forgotten target would otherwise widen the date
    range queried for every *other* ticker too, since a single shared cutoff
    has no way to represent "this ticker only cares about last week, that
    one cares about last year".

    Deliberately does NOT filter on documentStatus -- the scheduled poll
    only runs a fast metadata-only scrape (see scraper_runner.run_scrape's
    metadata_only flag), so most candidate filings will have documentText
    empty and documentStatus unset at this point. Callers are expected to
    run targeted extraction (monitor.document_extractor) on demand for
    whichever specific filings actually match the watchlist, rather than
    relying on the full slow scrape to have already populated every filing.
    """
    if not ticker_since:
        return []

    # filingDate is a real datetime in SurrealDB; it must be compared against
    # a d'' datetime literal. Comparing against a plain string doesn't error --
    # it silently applies cross-type ordering and returns wrong results.
    clauses = [
        f"(stockCode = '{_escape_sql_string(ticker)}' AND filingDate >= d'{_hkt_date_to_utc_iso(since)}')"
        for ticker, since in ticker_since.items()
    ]
    sql = (
        "SELECT filingId, stockCode, stockName, title, filingDate, "
        "documentUrl, documentText, documentStatus "
        "FROM exchange_filing "
        f"WHERE {' OR '.join(clauses)} "
        "ORDER BY filingDate ASC;"
    )
    return query(sql)


# Keep the SurrealDB /sql request body safely under its ~1 MiB limit even
# after escaping and wrapping in the UPDATE statement.
MAX_STORED_DOCUMENT_CHARS = 500_000


def update_filing_document(
    filing_id: str,
    document_text: str,
    document_type: str = "",
    status: str = "processed",
    status_reason: str = "",
) -> None:
    """Write extracted document text back onto an existing exchange_filing record.

    Used by monitor.document_extractor for targeted, single-filing text
    extraction -- the fast-path alternative to waiting for the upstream
    scraper's full (slow, sequential) Phase 2 document backfill.
    """
    filing_id = _validate_filing_id(filing_id)
    text = document_text[:MAX_STORED_DOCUMENT_CHARS]
    was_truncated = len(document_text) > MAX_STORED_DOCUMENT_CHARS
    reason = status_reason or (f"truncated_from_{len(document_text)}" if was_truncated else "")

    sql = (
        f"UPDATE exchange_filing:{filing_id} SET "
        f"documentText = '{_escape_sql_string(text)}', "
        f"documentTextLen = {len(text)}, "
        f"documentType = '{_escape_sql_string(document_type)}', "
        f"documentStatus = '{_escape_sql_string(status)}', "
        f"documentStatusReason = '{_escape_sql_string(reason)}', "
        "updatedAt = time::now() "
        "RETURN NONE;"
    )
    query(sql)


def filing_hkt_date(filing: dict[str, Any]) -> date | None:
    """Convert a filing's filingDate (ISO datetime string, usually UTC) to an HKT calendar date."""
    raw = filing.get("filingDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(HKT).date()
