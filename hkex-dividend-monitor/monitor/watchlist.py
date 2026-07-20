"""Orchestration for Today's HKEX Dividend Watchlist.

Ties together candidate discovery (monitor.hkex_search, per-ticker only --
see module docstring note below), structured extraction
(monitor.document_extractor + monitor.announcement_extractor), history
persistence (monitor.history), and deterministic scoring
(monitor.features + monitor.scoring) into one ranked, explainable list per
day. See the module docstrings of those pieces for the "why" of each step;
this module is the "when/in what order".

Scoped to the user's own tickers -- monitor.registry.WatchlistTickers (a
dedicated list managed on the Dividend Watchlist tab) unioned with
whatever's on the alert watchlist (monitor.registry.TargetRegistry) --
rather than a market-wide scan. HKEX's per-ticker search
(monitor.hkex_search.search_filings_by_ticker) is one request per ticker
regardless of how far back it looks, so this keeps generation time and LLM
cost proportional to how many companies the user actually cares about
instead of the whole exchange.

Generation is triggered lazily (once on app startup, non-blocking; on
demand via the dashboard's Refresh button) -- never by a recurring
scheduler. A module-level lock serializes generation so the startup thread
and a manual Refresh can never run concurrently and double-write.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Any, Optional

from monitor import history
from monitor.activity import log_event
from monitor.announcement_extractor import AnnouncementExtraction, classify_title, explain, extract_announcement
from monitor.config import get_config
from monitor.daemon import HKT
from monitor.diagnostics import log_error
from monitor.document_extractor import DocumentExtractionError, extract_and_save_filing
from monitor.extractor import ExtractionError
from monitor.features import build_features
from monitor.hkex_search import HKEXSearchError, search_filings_by_ticker, upsert_filing_metadata
from monitor.registry import TargetRegistry, WatchlistTickers
from monitor.scoring import ScoreResult, score_candidate

# Keywords searched per ticker, and which lookback window each uses.
_NOTICE_KEYWORDS = ("board meeting", "results")
_HISTORY_KEYWORD = "dividend"

# Cap on *new* filings ingested per ticker per run (across notice + history
# searches combined) -- bounds LLM cost/runtime per ticker regardless of how
# far back watchlist_history_lookback_days reaches. Filings already in
# company_event from a prior run don't count against this (see
# known_filing_ids skip-list), so this only ever taxes genuinely new filings.
_MAX_NEW_FILINGS_PER_TICKER = 12

_generation_lock = threading.Lock()


def _today() -> date:
    return datetime.now(HKT).date()


def is_generating() -> bool:
    return _generation_lock.locked()


def has_tracked_tickers() -> bool:
    """Whether there's anything to rank at all. An empty ranking still
    saves zero dividend_watchlist rows for today (nothing to write a row
    for), which is otherwise indistinguishable from "generation hasn't run
    yet" -- callers (the GET endpoint, trigger_background_generate) use
    this to report/skip immediately instead of showing "generating"
    forever when the user simply hasn't tracked any tickers yet.
    """
    if WatchlistTickers().tickers():
        return True
    try:
        return bool(TargetRegistry().active_targets())
    except Exception:  # noqa: BLE001 - a bad registry read means "assume nothing tracked"
        return False


def _parse_hkex_date(value: Optional[str]) -> Optional[date]:
    """rec['date'] from monitor.hkex_search is 'DD/MM/YYYY'."""
    if not value:
        return None
    try:
        dd, mm, yyyy = value.split("/")
        return date(int(yyyy), int(mm), int(dd))
    except (ValueError, AttributeError):
        return None


def _parse_iso_or_none(value: Optional[str]) -> Optional[date]:
    """A date string the LLM extracted, expected 'YYYY-MM-DD' -- tolerant
    of a malformed value (dropped, never raised) since this is untrusted
    model output, not a value we control the format of."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _ingest_filing_as_event(rec: dict[str, Any], ticker: str) -> None:
    """Classify + structurally extract one HKEX filing and persist it as a
    company_event row. Title-keyword pre-classification (classify_title)
    filters out obviously irrelevant filings before spending an LLM call;
    any failure (download, extraction, LLM) is logged and the filing is
    skipped -- one bad filing must never abort watchlist generation.
    """
    filing_id = rec["filingId"]
    title = rec.get("title", "")
    coarse_kind = classify_title(title)
    if coarse_kind == "other":
        return

    try:
        upsert_filing_metadata([rec])
    except Exception as exc:  # noqa: BLE001 - metadata upsert failure shouldn't block extraction
        log_error("watchlist.ingest", f"Failed to upsert filing metadata {filing_id}: {exc}")

    log_event("watchlist.ingest", "doc.extract", f"Downloading + extracting document for filing {filing_id}", ticker=ticker)
    try:
        text = extract_and_save_filing(filing_id, rec.get("link", ""))
    except DocumentExtractionError as exc:
        log_error("watchlist.ingest", f"Extraction failed for {filing_id}: {exc}")
        return

    extraction_status = "ok"
    log_event("watchlist.ingest", "llm.classify", f"Sending filing {filing_id} to LLM for classification", ticker=ticker)
    try:
        extraction = extract_announcement(filing_id, title, text)
    except ExtractionError as exc:
        log_error("watchlist.ingest", f"LLM classification failed for {filing_id}: {exc}")
        extraction = AnnouncementExtraction(event_kind=coarse_kind)
        extraction_status = "failed"

    raw_code = str(ticker).lstrip("0") or "0"
    event = {
        "filingId": filing_id,
        "stockCode": ticker,
        "companyTicker": f"{raw_code.zfill(4)}.HK",
        "stockName": rec.get("stockName"),
        "title": title,
        "documentUrl": rec.get("link"),
        "announcementDate": _parse_hkex_date(rec.get("date")),
        "source": "HKEx",
        "eventKind": extraction.event_kind,
        "boardMeetingDate": _parse_iso_or_none(extraction.board_meeting_date),
        "boardMeetingPurposeApprovesResults": extraction.board_meeting_purpose_approves_results,
        "boardMeetingPurposeConsidersDividend": extraction.board_meeting_purpose_considers_dividend,
        "boardMeetingPurposeRaw": extraction.board_meeting_purpose_raw,
        "resultsPeriod": extraction.results_period,
        "dividendType": extraction.dividend_type,
        "dividendAmount": extraction.dividend_amount,
        "exDate": _parse_iso_or_none(extraction.ex_date),
        "recordDate": _parse_iso_or_none(extraction.record_date),
        "paymentDate": _parse_iso_or_none(extraction.payment_date),
        "declaredDate": _parse_iso_or_none(extraction.declared_date),
        "extractionStatus": extraction_status,
    }
    try:
        history.upsert_event(event)
    except Exception as exc:  # noqa: BLE001 - a store write must never break generation
        log_error("watchlist.ingest", f"Failed to persist event {filing_id}: {exc}")


def _discover_universe(cfg) -> tuple[list[str], dict[str, Optional[str]]]:
    """The tickers to rank: the user's dedicated Dividend Watchlist ticker
    list (monitor.registry.WatchlistTickers) unioned with whatever's
    currently on the alert watchlist (monitor.registry.TargetRegistry) --
    a ticker being watched for an exact-date alert is worth ranking here
    too. Deduped, capped at watchlist_max_candidates as a safety ceiling
    (the realistic size is however many tickers the user actually added).
    Returns (tickers, ticker -> best-known display name).
    """
    tickers: list[str] = []
    seen: set[str] = set()
    names: dict[str, Optional[str]] = {}

    for entry in WatchlistTickers().load():
        t = entry.get("ticker")
        if not t or t in seen:
            continue
        seen.add(t)
        tickers.append(t)
        names[t] = entry.get("name")

    try:
        for target in TargetRegistry().active_targets():
            t = target["ticker"]
            if t not in seen:
                seen.add(t)
                tickers.append(t)
                names.setdefault(t, None)
    except Exception as exc:  # noqa: BLE001 - a bad registry read must not block generation
        log_error("watchlist.discover", f"Failed to read active targets: {exc}")

    return tickers[: cfg.watchlist_max_candidates], names


def _process_ticker(ticker: str, today: date, cfg, known_ids: set[str]) -> list[dict[str, Any]]:
    """Search HKEX directly for this one ticker's board-meeting/results
    notices (notice lookback) and dividend history (history lookback),
    ingest any new filings (bounded to _MAX_NEW_FILINGS_PER_TICKER per
    run), and return its full company_event history for feature building.
    One ticker's search failure is logged and skipped, not fatal to the
    rest of generation.
    """
    notice_from = today - timedelta(days=cfg.watchlist_notice_lookback_days)
    history_from = today - timedelta(days=cfg.watchlist_history_lookback_days)

    candidate_records: dict[str, dict[str, Any]] = {}
    for keyword, from_date in (
        (_NOTICE_KEYWORDS[0], notice_from),
        (_NOTICE_KEYWORDS[1], notice_from),
        (_HISTORY_KEYWORD, history_from),
    ):
        try:
            records = search_filings_by_ticker(ticker, from_date, today, title_keyword=keyword)
        except HKEXSearchError as exc:
            log_error("watchlist.discover", f"Search for {ticker} ({keyword!r}) failed: {exc}")
            continue
        log_event(
            "watchlist.discover", "hkex.search", f"HKEX search {ticker} {keyword!r}: {len(records)} record(s)",
            level="debug", ticker=ticker,
        )
        for rec in records:
            candidate_records.setdefault(rec["filingId"], rec)

    new_records = [r for r in candidate_records.values() if r["filingId"] not in known_ids]
    for rec in new_records[:_MAX_NEW_FILINGS_PER_TICKER]:
        _ingest_filing_as_event(rec, ticker)
        known_ids.add(rec["filingId"])

    return history.events_for_ticker(ticker)


def _build_evidence(events: list[dict[str, Any]], features: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {e.get("filingId"): e for e in events if e.get("filingId")}
    picks = []
    for fid in features.get("evidence_filing_ids", []):
        e = by_id.get(fid)
        if not e:
            continue
        picks.append(
            {
                "filingId": fid,
                "title": e.get("title"),
                "documentUrl": e.get("documentUrl"),
                "announcementDate": e.get("announcementDate"),
            }
        )
    return picks


def _build_row(
    ticker: str,
    known_name: Optional[str],
    events: list[dict[str, Any]],
    features: dict[str, Any],
    result: ScoreResult,
) -> dict[str, Any]:
    stock_name = known_name or next(
        (e.get("stockName") for e in reversed(events) if e.get("stockName")), None
    )
    reasons = [r.to_dict() for r in result.reasons]
    row: dict[str, Any] = {
        "stockCode": ticker,
        "stockName": stock_name,
        "score": result.score,
        "band": result.band,
        "reasons": reasons,
        "boardMeetingDate": features.get("board_meeting_date"),
        "resultsDate": features.get("results_date"),
        "historicalWindow": {
            "avgDeclarationMonth": features.get("avg_declaration_month"),
            "avgDeclarationIntervalDays": features.get("avg_declaration_interval_days"),
            "consistencyScore": features.get("historical_consistency_score"),
            "numObservations": features.get("num_observations"),
            "inHistoricalWindow": features.get("in_historical_declaration_window"),
        },
        "latestDividend": {
            "type": features.get("last_dividend_type"),
            "amount": features.get("last_dividend_amount"),
            "declaredDate": features.get("last_declaration_date"),
        },
        "evidence": _build_evidence(events, features),
    }
    # Best-effort readable summary -- explanation only, never used to
    # adjust score/rank; a None here just means the dashboard falls back to
    # listing `reasons` directly.
    row["explanation"] = explain(stock_name or ticker, ticker, reasons)
    return row


def generate_watchlist(today: date) -> tuple[list[dict[str, Any]], str]:
    """Run the full discover -> extract -> score -> rank pipeline, scoped
    to the user's own tickers, and persist the result as today's
    dividend_watchlist rows. Returns (ranked_rows, generated_at_iso).
    Callers needing "reuse if it already exists" semantics should use
    get_or_generate_today instead -- this function always does the work.
    """
    cfg = get_config()
    history.ensure_schema()
    known_ids = history.known_filing_ids()

    tickers, known_names = _discover_universe(cfg)
    log_event("watchlist.generate", "watchlist.progress", f"Watchlist generation started for {len(tickers)} ticker(s)")

    scored: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            events = _process_ticker(ticker, today, cfg, known_ids)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not abort the whole run
            log_error("watchlist.generate", f"Processing failed for {ticker}: {exc}", exc)
            events = history.events_for_ticker(ticker)

        features = build_features(today, events, horizon_days=cfg.watchlist_horizon_days)
        result = score_candidate(features, horizon_days=cfg.watchlist_horizon_days)
        if result.score <= 0:
            continue  # no positive signal at all -- not worth surfacing today

        scored.append(_build_row(ticker, known_names.get(ticker), events, features, result))

    scored.sort(key=lambda r: r["score"], reverse=True)
    for i, row in enumerate(scored, start=1):
        row["rank"] = i

    generated_at = datetime.now(HKT).isoformat()
    history.save_watchlist(today, generated_at, scored)
    log_event("watchlist.generate", "watchlist.progress", f"Watchlist generated: {len(scored)} companies scored")
    return scored, generated_at


def get_or_generate_today(force: bool = False) -> dict[str, Any]:
    """Return today's watchlist, generating it first if it doesn't exist
    yet (or unconditionally if force=True). Safe to call concurrently --
    generation is serialized by _generation_lock, and a caller that loses
    the race to acquire it simply reuses the winner's freshly-saved result
    instead of generating a second time.
    """
    today = _today()
    if not force:
        cached = history.load_watchlist(today)
        if cached is not None:
            return {"status": "ready", "generatedAt": cached["generatedAt"], "rows": cached["rows"]}

    with _generation_lock:
        if not force:
            cached = history.load_watchlist(today)
            if cached is not None:
                return {"status": "ready", "generatedAt": cached["generatedAt"], "rows": cached["rows"]}
        rows, generated_at = generate_watchlist(today)
        return {"status": "ready", "generatedAt": generated_at, "rows": rows}


def trigger_background_generate() -> None:
    """Kick off best-effort watchlist generation in a background thread if
    nothing is generating already and today's watchlist doesn't exist yet.
    Used by web.py's startup hook and by GET /api/watchlist when no cached
    watchlist is found -- never blocks the caller, and is NOT a recurring
    scheduler: this fires at most once per process per "missing today's
    watchlist" situation.
    """
    if _generation_lock.locked():
        return
    if not has_tracked_tickers():
        return  # nothing to rank -- don't spin up a thread just to persist zero rows
    today = _today()
    try:
        if history.watchlist_exists(today):
            return
    except Exception as exc:  # noqa: BLE001 - a DB hiccup here just means we try to generate anyway
        log_error("watchlist.background", f"Failed to check existing watchlist: {exc}")

    def _run() -> None:
        try:
            get_or_generate_today(force=False)
        except Exception as exc:  # noqa: BLE001 - background thread must never crash the process
            log_error("watchlist.background", f"Background generation failed: {exc}", exc)

    threading.Thread(target=_run, name="watchlist-generate", daemon=True).start()
