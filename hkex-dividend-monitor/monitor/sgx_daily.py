"""SGX daily settlement-price archive (Derivatives Daily Data), fetched
live from SGX's own per-business-day archive -- not this app's database.

Unlike `monitor.settlement`'s SGX Final Settlement Price workbook (a
current snapshot only -- see that module's docstring) or
`monitor.settlement_history`'s SurrealDB archive (only as deep as this app
has been running), SGX itself hosts a complete history of daily
settlement marks -- SETTLE, OHLC, volume, open interest, one row per
contract month. SGX's archive itself reaches back to 2013-04-05 (key 1),
but this module only parses the MODERN column layout, which live key
mapping confirms starts at 2018-01-19 (see EARLIEST_SUPPORTED_TRADE_DATE);
older files use different, undocumented layouts across at least three eras
and are deliberately unsupported (see SGXDailyFormatUnsupported). So a
past-date question doesn't depend on this app having archived anything:
within the supported range, it's fetched from SGX on demand, the same
request whether the date is yesterday or 2019.

Reverse-engineered from SGX's own "Derivatives Daily Data" download page
(`sgx.com/research-education/derivatives`), the same way the FSP workbook
chain in `monitor.settlement` was:

  - a JSON list feed (SGX's `V1_DERIVATIVES_DAILY_LIST_URL`, from the same
    appconfig.json `_fetch_sgx_appconfig` already reads) gives the newest
    ~60 business days as {numeric key -> trade date} pairs;
  - each business day's data lives at a fixed URL keyed by that same
    numeric key: `links.sgx.com/1.0.0/derivatives-daily/{key}/FUTURE.zip`
    (options are also available at OPTION.zip but out of scope here).

The key sequence isn't perfectly one-per-weekday -- SGX has occasionally
published on a weekend, shifting later keys by 1-2 -- so a date outside
the list feed's ~60-day window is resolved by ESTIMATING a key via plain
weekday arithmetic from the nearest known (date, key) anchor, then
VERIFYING against the file's own DATE column and correcting if wrong (see
resolve_daily_key). This is the one thing that would have broken a naive
port of prior art for this exact problem (two public scripts doing the
same date->key math, both admittedly fragile to the same weekend-shift
issue) -- never trusting the arithmetic alone is the fix. Confirmed dates
are persisted to data/sgx_daily_keys.json (grows monotonically, never
invalidated -- a business day's key never changes once assigned), so
repeated or nearby lookups get cheaper over time.

Important complementary-not-overlapping relationship with the FSP
workbook: on a contract's own expiry day, its row in this feed has
SETTLE=0 (verified live: SGX Nikkei July-2026 futures on 2026-07-10, its
last trading day) -- the true final settlement is only ever published in
the FSP workbook / archived by monitor.settlement_history. This module is
for the ongoing daily marks a contract carries while still live, the SGX
equivalent of Eurex's D. Settle in monitor.settlement.fetch_eurex_settlement.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests

from monitor import settlement, settlement_history
from monitor.config import HKT, SGX_DAILY_KEYS_FILE
from monitor.jsonutil import atomic_write_json, load_json

SGX_DAILY_LIST_URL = (
    "https://api3.sgx.com/infofeed/Apps?A=COW_Infopubdtstat_Content&B=DailyDataDownload&S_T=1&C_T=60"
)
SGX_DAILY_BASE_URL = "https://links.sgx.com/1.0.0/derivatives-daily"

# SGX's archive itself starts at key 1 = 2013-04-05, but that isn't the
# floor this module can actually serve: live key-mapping (binary search
# during implementation) found the MODERN comma-delimited format this
# module parses only begins at key 5331 = 2018-01-19 -- keys before that
# are older, unsupported layouts (see SGXDailyFormatUnsupported), and keys
# 1-~3000 don't even run forward in time (SGX backfilled 2001-2013 data
# into low keys after the fact). A request predating this floor is
# rejected immediately rather than burning network round-trips discovering
# the same thing the slow way, and the message says so honestly instead of
# implying the whole 2013+ archive is available here.
EARLIEST_SUPPORTED_TRADE_DATE = date(2018, 1, 19)

# Modern files start with this exact header; older files use a handful of
# different, undocumented column layouts across at least three eras and
# are deliberately unsupported (see module docstring) -- sniffed here
# rather than assumed.
_MODERN_HEADER_PREFIX = "DATE,COM,"

_LIST_FEED_TTL_SECONDS = 3600.0
# History is immutable once published (unlike the FSP workbook, which
# updates through the trading day) -- a long TTL just avoids re-hitting
# SGX for the same key repeatedly within one process's lifetime.
_KEY_FETCH_TTL_SECONDS = 6 * 3600.0

# Bounded so a bad estimate can never loop indefinitely; SGX's observed
# weekend-shift drift is 1-2 keys, so this is generous headroom.
_MAX_KEY_VERIFY_ATTEMPTS = 6

# Only worth consulting the list feed (an extra network call) for a date
# that might actually be IN its ~60-business-day window, or when there's
# no persisted anchor yet at all (a fresh install's first-ever lookup).
_LIST_FEED_RELEVANT_WINDOW_DAYS = 120


class SGXDailyNotAvailable(settlement.SettlementError):
    """`trade_date` has no SGX derivatives-daily file: predates the
    supported range, falls on a weekend/holiday, or (for a very recent
    date) hasn't been published yet (~07:20 SGT the next morning). Callers
    can catch this distinctly from a genuine fetch failure to render a
    softer "no trading that day" response instead of an error."""


class SGXDailyFormatUnsupported(settlement.SettlementError):
    """The resolved key has a real file for a real date, but it's in an
    older column layout this app doesn't parse (see module docstring) --
    distinct from SGXDailyNotAvailable: the date isn't missing, just not
    supported yet. resolve_daily_key's verify loop re-raises this
    immediately rather than retrying nearby keys, since format changes are
    chronological -- every date near this one is in the same unsupported
    era, so no amount of retrying finds a parseable neighbor."""


class SGXDailyNoFileAtKey(settlement.SettlementError):
    """The estimated key has no real file at all -- confirmed live: keys
    past the newest published one serve a 200-status HTML error page
    (there is no 404 to detect), which fails zip parsing. This is the
    *expected* shape of "this key doesn't correspond to a trading day" and
    feeds resolve_daily_key's bracket-and-conclude-NotAvailable logic --
    deliberately distinct from a genuine network/download failure
    (timeout, DNS, 5xx), which must never be reported as the same
    "weekend/holiday" calendar claim. Internal to this module's key
    resolution; never expected to escape resolve_daily_key uncaught."""


# ---- persisted key<->date map ----


def _load_key_map() -> dict[str, int]:
    return load_json(SGX_DAILY_KEYS_FILE, {})


def _merge_into_key_map(dates_to_keys: dict[date, int]) -> None:
    data = _load_key_map()
    changed = False
    for d, key in dates_to_keys.items():
        iso = d.isoformat()
        if data.get(iso) != key:
            data[iso] = key
            changed = True
    if changed:
        atomic_write_json(SGX_DAILY_KEYS_FILE, data)


# ---- list feed (recent ~60 business days, authoritative -- no guessing) ----


def _parse_list_feed_items(payload: dict[str, Any]) -> dict[date, int]:
    result: dict[date, int] = {}
    for item in payload.get("items") or []:
        raw_key = item.get("key")
        raw_date = item.get("Trade Date")
        if not raw_key or not raw_date:
            continue
        try:
            key = int(raw_key)
            d = datetime.strptime(raw_date, "%d %b %Y").date()
        except (TypeError, ValueError):
            continue
        result[d] = key
    return result


def _fetch_list_feed_impl() -> dict[date, int]:
    try:
        resp = requests.get(SGX_DAILY_LIST_URL, headers=settlement._HEADERS, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise settlement.SettlementError(f"SGX daily-data list feed fetch failed: {exc}") from exc
    items = payload.get("items") or []
    parsed = _parse_list_feed_items(payload)
    if items and not parsed:
        # Every item present failed to parse -- the feed's own field
        # names (key / "Trade Date") most likely changed shape, not that
        # ~60 consecutive business days all happened to be malformed at
        # once. Surface as a fetch/parse problem rather than silently
        # caching an empty result that reads as "SGX has no recent files."
        raise settlement.SettlementError(
            "SGX daily-data list feed returned items but none were parseable -- its shape may have changed"
        )
    if parsed:
        _merge_into_key_map(parsed)
    return parsed


def _fetch_list_feed(force: bool = False) -> dict[date, int]:
    return settlement._cached_fetch("sgx_daily_list_feed", force, _LIST_FEED_TTL_SECONDS, _fetch_list_feed_impl)


# ---- key resolution (estimate from a known anchor, then verify) ----


def _weekdays_between(start: date, end: date) -> int:
    """Signed count of weekdays (Mon-Fri) in the half-open interval
    [start, end) when end >= start (negative of [end, start) otherwise) --
    matches numpy.busday_count's convention, which both pieces of prior
    art for this exact SGX key arithmetic rely on: the day AT `start`
    counts, the day AT `end` does not, so "the very next business day"
    is a diff of exactly 1. Used only to ESTIMATE an unresolved key from a
    known (date, key) anchor -- resolve_daily_key always verifies the
    result against the file's own DATE column rather than trusting this
    arithmetic alone, since SGX's key sequence isn't strictly one-per-
    weekday (occasional weekend publications shift it).
    """
    if end == start:
        return 0
    sign = 1 if end > start else -1
    lo, hi = (start, end) if end > start else (end, start)
    count = 0
    d = lo
    while d < hi:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return sign * count


def _nearest_anchor(
    trade_date: date, key_map: dict[str, int], recent: dict[date, int]
) -> tuple[Optional[date], Optional[int]]:
    candidates: dict[date, int] = dict(recent)
    for iso, key in key_map.items():
        try:
            candidates[date.fromisoformat(iso)] = key
        except ValueError:
            continue
    if not candidates:
        return None, None
    nearest = min(candidates, key=lambda d: abs((d - trade_date).days))
    return nearest, candidates[nearest]


def resolve_daily_key(trade_date: date) -> int:
    """Resolve `trade_date` to its numeric key on SGX's derivatives-daily
    archive. Checks the persisted map first; on a miss, tries the live
    list feed (covers the newest ~60 business days); failing that,
    estimates from the nearest known anchor via weekday arithmetic and
    VERIFIES against the actual file's own DATE column, correcting and
    retrying (bounded) until it lands on the requested date -- see
    _weekdays_between's docstring for why the estimate alone isn't trusted.
    Every date confirmed along the way is persisted, so nearby lookups get
    cheaper over time.

    Raises SGXDailyNotAvailable if `trade_date` predates the supported
    range, or if resolution brackets it with no file of its own (a
    weekend/holiday, or -- for a very recent date -- not published yet).
    Raises a plain SettlementError (never SGXDailyNotAvailable) if key
    verification fails for a genuine network/fetch reason rather than a
    confirmed absence of a file -- a transient outage must never be
    reported as if it were a calendar fact about SGX's trading days.
    """
    if trade_date < EARLIEST_SUPPORTED_TRADE_DATE:
        raise SGXDailyNotAvailable(
            f"SGX's daily archive holds older files too (back to 2013-04-05 and beyond), but "
            "this app only parses the modern column format, which is supported from "
            f"{EARLIEST_SUPPORTED_TRADE_DATE.isoformat()}; {trade_date.isoformat()} predates that."
        )

    iso = trade_date.isoformat()
    key_map = _load_key_map()
    if iso in key_map:
        return key_map[iso]

    recent: dict[date, int] = {}
    if not key_map or (datetime.now(HKT).date() - trade_date).days <= _LIST_FEED_RELEVANT_WINDOW_DAYS:
        try:
            recent = _fetch_list_feed()
        except settlement.SettlementError:
            recent = {}
        if trade_date in recent:
            _merge_into_key_map({trade_date: recent[trade_date]})
            return recent[trade_date]

    anchor_date, anchor_key = _nearest_anchor(trade_date, key_map, recent)
    if anchor_date is None or anchor_key is None:
        raise settlement.SettlementError(
            "No known SGX daily-archive date to estimate from yet -- the list feed must be "
            "reachable at least once before resolving a date outside its recent window."
        )

    key = anchor_key + _weekdays_between(anchor_date, trade_date)
    tried: set[int] = set()
    for _ in range(_MAX_KEY_VERIFY_ATTEMPTS):
        if key in tried or key < 1:
            break
        tried.add(key)
        try:
            actual_date = _peek_key_date(key)
        except SGXDailyFormatUnsupported as exc:
            # This key has real data for a real (if unconfirmed) date --
            # just not one we parse. Retrying a neighboring key can't fix
            # that (format changes are chronological), so surface this
            # distinctly instead of reporting the date as if it didn't exist.
            # Re-raised with the originally REQUESTED date prefixed -- the
            # exception's own text only names the (possibly different)
            # probed key's recovered date.
            raise SGXDailyFormatUnsupported(f"{trade_date.isoformat()}: {exc}") from exc
        except SGXDailyNoFileAtKey:
            # Confirmed live: this is the expected shape of "this estimated
            # key isn't a real trading day" (SGX serves a 200-status HTML
            # error page past the newest published key, and presumably for
            # any other genuinely absent key) -- feeds the bracket-and-
            # conclude-NotAvailable logic below, same as before.
            break
        except settlement.SettlementError as exc:
            # A genuine fetch/network problem (timeout, DNS, 5xx) -- NOT
            # the same thing as "no file at this key". Reporting this as
            # "likely a weekend/holiday" would misrepresent an outage as a
            # calendar fact (confirmed live: this exact conflation was the
            # audit's finding). Surface it distinctly instead.
            raise settlement.SettlementError(
                f"could not verify the SGX archive key for {trade_date.isoformat()}: {exc}"
            ) from exc
        _merge_into_key_map({actual_date: key})
        if actual_date == trade_date:
            return key
        key += _weekdays_between(actual_date, trade_date)

    raise SGXDailyNotAvailable(
        f"{trade_date.isoformat()} does not have an SGX derivatives-daily file -- likely a "
        "weekend/holiday, or (for a very recent date) not published yet (~07:20 SGT the next morning)."
    )


def _peek_key_date(key: int) -> date:
    return _fetch_by_key(key)["tradeDate"]


# ---- fetch + parse one key's FUTURE.zip ----


def _download_key_file(url: str) -> bytes:
    """Like settlement._download, but preserves the distinction between "no
    real file here" and "the request itself failed" instead of collapsing
    both into one SettlementError -- resolve_daily_key's verify loop needs
    that distinction (see SGXDailyNoFileAtKey)."""
    try:
        resp = requests.get(url, headers=settlement._HEADERS, timeout=60.0)
        if resp.status_code == 404:
            raise SGXDailyNoFileAtKey(f"No SGX derivatives-daily file at {url} (404).")
        resp.raise_for_status()
        return resp.content
    except SGXDailyNoFileAtKey:
        raise
    except requests.RequestException as exc:
        raise settlement.SettlementError(f"Failed to download {url}: {exc}") from exc


def _fetch_key_impl(key: int) -> dict[str, Any]:
    url = f"{SGX_DAILY_BASE_URL}/{key}/FUTURE.zip"
    raw_bytes = _download_key_file(url)

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
        inner_name = zf.namelist()[0]
        text = zf.read(inner_name).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, IndexError) as exc:
        # Confirmed live: SGX serves a 200-status HTML error page (not a
        # 404) for keys past the newest published one, so this is where
        # "no real file at this key" actually shows up in practice.
        raise SGXDailyNoFileAtKey(f"No real SGX derivatives-daily file at key {key}: {exc}") from exc

    lines = text.splitlines()
    if not lines or not lines[0].startswith(_MODERN_HEADER_PREFIX):
        # Best-effort: DATE is always the first column regardless of
        # delimiter (comma in the modern format, tab in older ones), so
        # even here we can usually name which date this actually was.
        probed_date = None
        if len(lines) > 1:
            first_field = re.split(r"[,\t]", lines[1], maxsplit=1)[0].strip()
            try:
                probed_date = datetime.strptime(first_field, "%Y%m%d").date()
            except ValueError:
                pass
        detail = f" (trade date {probed_date.isoformat()})" if probed_date else ""
        raise SGXDailyFormatUnsupported(
            f"SGX daily archive file at key {key}{detail} uses an older, unsupported column "
            "format (this app only parses the modern DATE,COM,... layout)."
        )

    rows: list[dict[str, Any]] = []
    trade_date: Optional[date] = None
    for raw_row in csv.reader(lines[1:]):
        if len(raw_row) < 11:
            continue
        d, com, mm, yy, o, h, lo, c, settle, vol, oi = raw_row[:11]
        series = raw_row[11].strip() if len(raw_row) > 11 else ""
        try:
            row_date = datetime.strptime(d.strip(), "%Y%m%d").date()
        except ValueError:
            continue
        if trade_date is None:
            trade_date = row_date

        def _num(raw: str) -> Any:
            raw = raw.strip()
            return settlement._coerce_numeric(raw) if raw else None

        rows.append(
            {
                "ticker": com.strip().upper(),
                "contractMonth": f"{yy.strip()}-{mm.strip()}",
                "open": _num(o),
                "high": _num(h),
                "low": _num(lo),
                "close": _num(c),
                "settle": _num(settle),
                "volume": _num(vol),
                "openInterest": _num(oi),
                "series": series,
            }
        )

    if trade_date is None:
        raise settlement.SettlementError(f"SGX daily archive file at key {key} contained no data rows")

    return {"key": key, "tradeDate": trade_date, "sourceFileUrl": url, "rows": rows}


def _fetch_by_key(key: int, force: bool = False) -> dict[str, Any]:
    return settlement._cached_fetch(f"sgx_daily:{key}", force, _KEY_FETCH_TTL_SECONDS, lambda: _fetch_key_impl(key))


# ---- public API ----


def fetch_sgx_daily(trade_date: date, force: bool = False) -> dict[str, Any]:
    """Daily settlement marks (settle, OHLC, volume, open interest) for
    every SGX futures contract month on `trade_date`, straight from SGX's
    own per-business-day archive -- data since EARLIEST_SUPPORTED_TRADE_DATE
    (2018-01-19; see that constant's comment for why, not SGX's older
    2013-04-05 archive floor), a date's file published ~07:20 SGT the next
    morning.

    These are ongoing DAILY marks, not final settlement prices at expiry:
    an expiring contract's `settle` is 0 on its own last trading day (the
    true final only ever appears in fetch_sgx_fsp's workbook / the
    monitor.settlement_history archive). Raises SGXDailyNotAvailable if
    `trade_date` isn't a trading day (or, for a very recent date, its file
    hasn't been published yet) -- see resolve_daily_key.
    """
    key = resolve_daily_key(trade_date)
    try:
        result = _fetch_by_key(key, force=force)
    except SGXDailyFormatUnsupported as exc:
        # Rare shortcut-path edge case: resolve_daily_key can return a key
        # straight from the persisted map or list feed without ever
        # calling _peek_key_date itself, so the format is only confirmed
        # here. Re-raise with the requested date, same as the verify loop.
        raise SGXDailyFormatUnsupported(f"{trade_date.isoformat()}: {exc}") from exc
    return {
        "tradeDate": result["tradeDate"].isoformat(),
        "sourceFileUrl": result["sourceFileUrl"],
        "rows": result["rows"],
    }


def filter_daily_rows(
    rows: list[dict[str, Any]], ticker: Optional[str] = None, contract_month: Optional[str] = None
) -> list[dict[str, Any]]:
    """Narrow a fetch_sgx_daily() row list. `ticker` matches via
    settlement_history's compound-ticker splitting (so "NK/NKO" typed
    verbatim, or a bare "NK", both work); `contract_month` accepts
    "YYYY-M" as well as "YYYY-MM" (zero-padded via settlement's own
    normalizer, same convention as the HKEX/SGX current-snapshot filters)."""
    out = rows
    if ticker:
        needles = set(settlement_history._ticker_components(str(ticker))) or {str(ticker).strip().upper()}
        out = [r for r in out if (r.get("ticker") or "").upper() in needles]
    if contract_month:
        wanted = settlement._normalize_year_month(str(contract_month)).strip()
        out = [r for r in out if (r.get("contractMonth") or "") == wanted]
    return out
