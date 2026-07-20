"""Background scheduler: polls SurrealDB for new matching filings every
POLL_INTERVAL_SECONDS, strictly during HKEX disclosure hours
(Mon-Fri, 06:00-23:00 HKT), and drives the extract -> notify pipeline.

Design goal: this process must never die from a single bad filing, a
flaky webhook, or a transient DB/LLM error. Every step is wrapped so a
failure is logged to diagnostics.log and the cycle continues.

Race mode: for arbitrage use, a target whose target_date is *today* gets
tight per-ticker polling (racing_targets/run_race_tick below) instead of
waiting for the next full-market scrape cycle -- see the module docstring
in monitor/hkex_search.py for why per-ticker search is fast enough for this.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Optional

from monitor.activity import log_event
from monitor.bloomberg import bloomberg_configured, fetch_dividend_data
from monitor.config import HEARTBEAT_FILE, HKT, get_config
from monitor.db import SurrealDBError, fetch_matching_filings, filing_hkt_date, health
from monitor.diagnostics import log_error
from monitor.document_extractor import DocumentExtractionError, extract_and_save_filing
from monitor.extractor import ExtractionError, extract_dividend_info
from monitor.hkex_search import HKEXSearchError, search_filings_by_ticker, upsert_filing_metadata
from monitor.jsonutil import to_iso_date_str
from monitor.notifier import (
    AlertPayload,
    FilingPing,
    RaceOutageAlert,
    any_succeeded,
    configured_channels,
    dispatch_alert,
    dispatch_text,
)
from monitor.registry import AlertHistory, DividendStore, NotifiedCache, TargetRegistry
from monitor.scraper_runner import ScraperError, compute_scrape_window, run_scrape
from monitor.settlement import SettlementError, fetch_sgx_flexc, fetch_sgx_fsp
from monitor.settlement_history import archive_sgx_snapshot
from monitor.settlement_history import ensure_schema as ensure_settlement_history_schema

# Race backoff on consecutive HKEX search failures for a given ticker.
RACE_MAX_BACKOFF_SECONDS = 300

# Bloomberg enrichment inside _classify_and_alert runs synchronously and, in
# race mode, sits inside run_race_tick's per-ticker loop -- a slow/hanging
# bridge must not be allowed to delay other tickers' time-critical instant
# pings within the same tick, so this is deliberately much shorter than
# fetch_dividend_data's own default timeout.
BLOOMBERG_ALERT_TIMEOUT_SECONDS = 5.0

registry = TargetRegistry()
notified_cache = NotifiedCache()
alert_history = AlertHistory()
dividend_store = DividendStore()


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(datetime.now(HKT).isoformat(), encoding="utf-8")
    except OSError as exc:
        log_error("daemon.heartbeat", f"Failed to write heartbeat file: {exc}")


def within_disclosure_hours(now: datetime | None = None) -> bool:
    """Monday-Friday, 06:00-23:00 HKT (HKEX disclosure window)."""
    now = now or datetime.now(HKT)
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    return 6 <= now.hour < 23


def run_scraper_step(active_targets: list[dict]) -> None:
    """Fast metadata-only scan: ticker/date/title/documentUrl for every
    filing in the window, no PDF text extraction (that would mean waiting
    on the upstream scraper's slow, sequential Phase 2 for filings nobody
    is even watching). run_match_and_notify_step extracts text only for
    the specific filings that actually match the watchlist."""
    target_dates = [date.fromisoformat(t["target_date"]) for t in active_targets]
    window = compute_scrape_window(target_dates, get_config().scrape_lookback_days)
    if window is None:
        return
    from_date, to_date = window
    log_event("daemon.scraper", "scrape.window", f"Market metadata scan {from_date} → {to_date}")
    try:
        run_scrape(from_date, to_date, metadata_only=True)
    except ScraperError as exc:
        log_error("daemon.scraper", str(exc))


def run_sgx_archive_step() -> None:
    """Archive today's SGX settlement-price files (main + FlexC) into
    SurrealDB -- see monitor.settlement_history's module docstring for why:
    unlike HKEX/Eurex, SGX's own site only ever shows a same-day snapshot,
    so this is the only place that history survives.

    Independent of the dividend-watch pipeline entirely -- called
    unconditionally from main()'s loop, not from run_cycle() (which
    early-returns when there are no active watchlist targets, unrelated to
    SGX's own publishing schedule). fetch_sgx_fsp/fetch_sgx_flexc's own
    10-minute cache means this doesn't hammer SGX every tick, and
    archive_sgx_snapshot's UPSERT-by-fspDate is idempotent, so no extra
    "already archived today" throttle is needed. A settlement-site or DB
    hiccup here must never touch the dividend-alert pipeline.
    """
    try:
        main = fetch_sgx_fsp()
        flexc = fetch_sgx_flexc()
    except SettlementError as exc:
        log_error("daemon.sgx_archive", f"SGX fetch failed: {exc}")
        return
    try:
        archived = archive_sgx_snapshot(main["rows"], flexc["rows"])
    except SurrealDBError as exc:
        log_error("daemon.sgx_archive", f"Failed to archive SGX settlement snapshot: {exc}")
        return
    log_event("daemon.sgx_archive", "sgx.archived", f"Archived {archived} SGX settlement row(s)", level="debug")


def _send_filing_ping(filing_id: str, ping: FilingPing) -> bool:
    """Dispatch an instant 'filing detected' notification; on success, mark
    it pinged (dedup, shared with the classify-and-alert tail below) and
    record it in alert_history.json (kind='ping') so it's reviewable later
    in the dashboard with its file link -- not just a one-shot chat message.
    Shared by both the normal cycle and race mode's stage 1."""
    results = dispatch_text(ping.render())
    if not any_succeeded(results):
        log_event(
            "daemon.notify", "notify.ping", f"Ping failed on all channels for {ping.ticker}; will retry",
            level="warn", ticker=ping.ticker,
        )
        return False
    notified_cache.mark_pinged(filing_id)
    alert_history.append(
        {
            "timestamp": datetime.now(HKT).isoformat(),
            "kind": "ping",
            "ticker": ping.ticker,
            "company_name": ping.stock_name,
            "title": ping.title,
            "source_url": ping.document_url,
            "channels": [ch for ch, ok in results.items() if ok],
        }
    )
    log_event(
        "daemon.notify", "notify.ping",
        f"Instant ping sent for {ping.ticker} via {', '.join(ch for ch, ok in results.items() if ok)}",
        ticker=ping.ticker,
    )
    return True


def _classify_and_alert(
    filing_id: str,
    ticker: str,
    document_text: str,
    source_url: Optional[str],
    stock_name: Optional[str],
    retry_cap: int,
    source_tag: str,
    filing_date: Optional[str] = None,
    title: Optional[str] = None,
) -> None:
    """Shared LLM-classify -> alert-or-file-away tail, used by both the
    normal match/notify cycle and race mode's stage 2 (after the instant
    FilingPing has already gone out)."""
    log_event(source_tag, "llm.classify", f"Sending filing {filing_id} to LLM for dividend classification", ticker=ticker)
    try:
        extraction = extract_dividend_info(filing_id, document_text)
    except ExtractionError as exc:
        log_error(source_tag, str(exc))
        notified_cache.record_failure(filing_id, retry_cap)
        return
    log_event(
        source_tag, "llm.classify",
        f"LLM verdict for {filing_id}: {'dividend' if extraction.is_dividend_announcement else 'not a dividend'}",
        ticker=ticker,
        meta={
            "is_dividend": extraction.is_dividend_announcement,
            "payout_amount": extraction.payout_amount,
            "ex_dividend_date": extraction.ex_dividend_date,
        },
    )

    # Record every classified filing for the watchlist's target date -- not
    # just confirmed dividends -- so the Dividends tab shows everything a
    # watched stock released that day. Independent of whether notification
    # dispatch below succeeds -- a webhook outage must not erase a filing
    # the LLM has already read.
    try:
        dividend_store.mark_dividend(
            {
                "filingId": filing_id,
                "ticker": ticker,
                "stockName": extraction.company_name or stock_name,
                "title": title,
                "isDividend": extraction.is_dividend_announcement,
                "payoutAmount": extraction.payout_amount,
                "exDividendDate": extraction.ex_dividend_date,
                "paymentDate": extraction.payment_date,
                "filingDate": filing_date,
                "documentUrl": source_url,
                "detectedAt": datetime.now(HKT).isoformat(),
            }
        )
    except Exception as exc:  # noqa: BLE001 - a store write must never break the pipeline
        log_error(source_tag, f"Failed to record filing {filing_id}: {exc}", exc)

    if not extraction.is_dividend_announcement:
        notified_cache.mark_processed_no_alert(filing_id)
        log_event(source_tag, "filter.not_dividend", f"Filing {filing_id} is not a dividend announcement — no alert sent", ticker=ticker)
        return

    bloomberg_fields = None
    if bloomberg_configured():
        try:
            bloomberg_results = fetch_dividend_data([ticker], timeout=BLOOMBERG_ALERT_TIMEOUT_SECONDS)
            if bloomberg_results:
                bloomberg_fields = bloomberg_results[0].get("fields") or None
        except Exception as exc:  # noqa: BLE001 - a Bloomberg outage must never break an alert
            log_error(source_tag, f"Bloomberg lookup failed for {ticker}: {exc}")

    payload = AlertPayload(
        ticker=ticker,
        company_name=extraction.company_name or stock_name,
        payout_amount=extraction.payout_amount,
        ex_dividend_date=extraction.ex_dividend_date,
        payment_date=extraction.payment_date,
        source_url=source_url,
        bloomberg_fields=bloomberg_fields,
    )
    results = dispatch_alert(payload)
    if any_succeeded(results):
        notified_cache.mark_notified(filing_id)
        alert_history.append(
            {
                "timestamp": datetime.now(HKT).isoformat(),
                "ticker": payload.ticker,
                "company_name": payload.company_name,
                "payout_amount": payload.payout_amount,
                "ex_dividend_date": payload.ex_dividend_date,
                "payment_date": payload.payment_date,
                "source_url": payload.source_url,
                "channels": [ch for ch, ok in results.items() if ok],
            }
        )
        log_event(
            source_tag, "notify.alert",
            f"Dividend alert sent for {payload.ticker} via {', '.join(ch for ch, ok in results.items() if ok)}",
            ticker=payload.ticker,
        )
    else:
        log_error(
            source_tag,
            f"All notification channels failed for filing {filing_id} (ticker {ticker})",
        )
        notified_cache.record_failure(filing_id, retry_cap)


_logged_unconfigured = False


def _build_ticker_date_maps(
    active_targets: list[dict],
) -> tuple[dict[str, set[date]], dict[str, date]]:
    """Group active targets by ticker: every target date that ticker is
    watched for (a ticker can have more than one, e.g. interim + final
    dividend), plus the earliest one, used to bound that ticker's own DB
    query without widening every other ticker's (see fetch_matching_filings)."""
    ticker_target_dates: dict[str, set[date]] = {}
    ticker_earliest: dict[str, date] = {}
    for t in active_targets:
        ticker = t["ticker"]
        d = date.fromisoformat(t["target_date"])
        ticker_target_dates.setdefault(ticker, set()).add(d)
        if ticker not in ticker_earliest or d < ticker_earliest[ticker]:
            ticker_earliest[ticker] = d
    return ticker_target_dates, ticker_earliest


def run_match_and_notify_step(active_targets: list[dict]) -> None:
    global _logged_unconfigured

    # A missing API key or notification channel is a *setup* state, not a
    # per-filing failure: skip the whole step rather than burning
    # max_extraction_retries attempts and permanently writing off filings
    # the user still wants alerts for once setup is finished.
    cfg = get_config()
    if not cfg.deepseek_api_key or not configured_channels():
        if not _logged_unconfigured:
            missing = []
            if not cfg.deepseek_api_key:
                missing.append("DeepSeek API key")
            if not configured_channels():
                missing.append("notification channel")
            log_error(
                "daemon.match",
                f"Skipping match/notify: no {' and no '.join(missing)} configured yet "
                "(set in the dashboard's Settings tab; no filings are being consumed meanwhile)",
            )
            _logged_unconfigured = True
        return
    _logged_unconfigured = False

    if not health():
        log_error("daemon.db", "SurrealDB health check failed; skipping this cycle's DB query")
        return

    if not active_targets:
        return

    # A ticker can have more than one active target on different dates (e.g.
    # watching for both an interim and a final dividend) -- track every date
    # per ticker, not just one, and bound each ticker's DB query by its own
    # earliest date so one old/forgotten target doesn't widen every other
    # ticker's query too (see fetch_matching_filings's docstring).
    ticker_target_dates, ticker_earliest = _build_ticker_date_maps(active_targets)

    try:
        filings = fetch_matching_filings(ticker_earliest)
    except SurrealDBError as exc:
        log_error("daemon.db", f"Failed to fetch filings: {exc}")
        return
    log_event(
        "daemon.match", "db.match",
        f"{len(filings)} candidate filing(s) fetched for {len(ticker_earliest)} ticker(s)",
        level="debug",
    )

    for filing in filings:
        filing_id = filing.get("filingId")
        stock_code = filing.get("stockCode")
        if not filing_id or not stock_code:
            continue
        if notified_cache.is_seen(filing_id):
            continue

        target_dates = ticker_target_dates.get(stock_code)
        if not target_dates:
            continue

        filed_on = filing_hkt_date(filing)
        if filed_on not in target_dates:
            continue

        cfg = get_config()

        # Instant "filing detected" ping -- same mechanism race mode uses,
        # now applied here too so any watched ticker's exact-date filing
        # notifies immediately, not just same-day/racing ones.
        if not notified_cache.is_pinged(filing_id):
            log_event(
                "daemon.match", "filing.detected",
                f"{stock_code}: new filing detected — {filing.get('title')}",
                ticker=stock_code, meta={"filing_id": filing_id},
            )
            ping = FilingPing(
                ticker=stock_code,
                stock_name=filing.get("stockName"),
                title=filing.get("title"),
                document_url=filing.get("documentUrl"),
                detected_at=datetime.now(HKT),
            )
            if not _send_filing_ping(filing_id, ping):
                log_error("daemon.match", f"Ping failed on all channels for filing {filing_id}; will retry")
                continue

        document_text = filing.get("documentText") or ""
        if not document_text.strip():
            # Metadata-only record (the fast scan doesn't extract text) --
            # this is the one filing out of the whole scrape window that
            # actually matters, so pull its document now instead of
            # waiting on the upstream scraper's slow full backfill.
            document_url = filing.get("documentUrl") or ""
            try:
                document_text = extract_and_save_filing(filing_id, document_url)
            except DocumentExtractionError as exc:
                log_error("daemon.document_extractor", f"Filing {filing_id}: {exc}")
                notified_cache.record_failure(filing_id, cfg.max_extraction_retries)
                continue

        _classify_and_alert(
            filing_id,
            stock_code,
            document_text,
            filing.get("documentUrl"),
            filing.get("stockName"),
            cfg.max_extraction_retries,
            "daemon.match",
            filing_date=filing.get("filingDate"),
            title=filing.get("title"),
        )


def run_cycle() -> None:
    _touch_heartbeat()
    try:
        active_targets = registry.active_targets()
    except Exception as exc:  # noqa: BLE001
        log_error("daemon.registry", f"Failed to load target registry: {exc}")
        return

    if not active_targets:
        return

    try:
        run_scraper_step(active_targets)
    except Exception as exc:  # noqa: BLE001 - never let a bug here kill the daemon
        log_error("daemon.scraper", f"Unexpected error in scraper step: {exc}", exc)

    try:
        run_match_and_notify_step(active_targets)
    except Exception as exc:  # noqa: BLE001
        log_error("daemon.match", f"Unexpected error in match/notify step: {exc}", exc)


def racing_targets(active_targets: list[dict], now: datetime | None = None) -> list[dict]:
    """Active targets whose target_date is *today* (HKT) -- these get tight
    per-ticker HKEX polling instead of waiting for the next full-market
    scrape cycle. See monitor/hkex_search.py for why per-ticker search is
    fast enough for this."""
    now = now or datetime.now(HKT)
    today_iso = now.date().isoformat()
    return [t for t in active_targets if t.get("target_date") == today_iso]


def target_match_status(
    ticker: str,
    target_date: date,
    today: date,
    racing_tickers: set[str],
    dividend_records: list[dict],
) -> str:
    """Classify one watch target's lifecycle state for the Watchlist tab.

    Exact-date matching means a target that never fires just sits there
    looking identical to one that's working fine -- there was previously no
    way to tell "hasn't happened yet" from "happened and got missed" from
    "something's broken". This gives the Watchlist tab that signal:

      - "upcoming": target_date hasn't arrived yet.
      - "racing": target_date is today and this ticker is in race mode.
      - "today": target_date is today but not racing (e.g. race window
        inactive).
      - "seen": target_date has passed and at least one filing was recorded
        for this ticker on that date (dividend or not -- see
        monitor.daemon._classify_and_alert, which records every classified
        filing on a watch date, not just confirmed dividends).
      - "pending": target_date has passed and nothing has been recorded --
        the case that used to be silently indistinguishable from "fine".
    """
    if target_date > today:
        return "upcoming"
    if target_date == today:
        return "racing" if ticker in racing_tickers else "today"
    for record in dividend_records:
        if record.get("ticker") != ticker:
            continue
        if to_iso_date_str(record.get("filingDate")) == target_date.isoformat():
            return "seen"
    return "pending"


def _race_window_active(cfg, now: datetime) -> bool:
    """race_start_hour/race_end_hour are user-editable (Settings tab) HKT
    hours; nonsense values (e.g. after manual .env editing) fall back to
    the full-day default rather than silently disabling race mode."""
    start, end = cfg.race_start_hour, cfg.race_end_hour
    if not (0 <= start < end <= 24):
        start, end = 0, 24
    return start <= now.hour < end


# Per-ticker consecutive-failure state for race mode's HKEX search backoff.
# In-memory only (reset on daemon restart) -- a soft protection against
# hammering HKEX during a transient outage/rate-limit, not persisted state.
_race_state: dict[str, dict[str, object]] = {}
_race_logged_unconfigured = False


def _alert_race_unreachable(
    ticker: str, state: dict[str, object], now: datetime, exc: Exception, cfg
) -> None:
    """Push a notification once consecutive HKEX search failures for `ticker`
    cross cfg.race_alert_failure_threshold -- diagnostics.log alone is easy
    to miss, and a real outage during race mode is exactly the kind of thing
    the user needs to know about promptly rather than after the fact."""
    if state["failures"] < cfg.race_alert_failure_threshold:
        return
    last_alert_at = state.get("last_alert_at")
    if state.get("alerted") and last_alert_at is not None and (
        now - last_alert_at
    ).total_seconds() < cfg.race_alert_cooldown_seconds:
        return  # already told the user; wait out the cooldown before repeating

    payload = RaceOutageAlert(ticker=ticker, recovered=False, failures=state["failures"], error=str(exc))
    results = dispatch_text(payload.render())
    if not any_succeeded(results):
        return  # couldn't tell the user anyway; try again next tick
    state["alerted"] = True
    state["last_alert_at"] = now
    log_event(
        "daemon.race", "race.outage_alert", f"Pushed HKEX-unreachable alert for {ticker} after {state['failures']} failures",
        level="warn", ticker=ticker,
    )
    alert_history.append(
        {
            "timestamp": datetime.now(HKT).isoformat(),
            "kind": "race_error",
            "ticker": ticker,
            "message": f"HKEX unreachable after {state['failures']} consecutive attempts: {exc}",
            "channels": [ch for ch, ok in results.items() if ok],
        }
    )


def _alert_race_recovered(ticker: str, state: dict[str, object]) -> None:
    """Once HKEX search succeeds again after an outage the user was alerted
    about, tell them it recovered -- otherwise the outage alert is the last
    thing they heard, with no signal that race mode is healthy again."""
    if not state.get("alerted"):
        return
    payload = RaceOutageAlert(ticker=ticker, recovered=True)
    results = dispatch_text(payload.render())
    if not any_succeeded(results):
        return
    state["alerted"] = False
    state["last_alert_at"] = None
    log_event("daemon.race", "race.recovered", f"HKEX reachable again for {ticker}", ticker=ticker)
    alert_history.append(
        {
            "timestamp": datetime.now(HKT).isoformat(),
            "kind": "race_recovered",
            "ticker": ticker,
            "message": "HKEX reachable again; race mode resumed normal polling.",
            "channels": [ch for ch, ok in results.items() if ok],
        }
    )


def run_race_tick(targets: list[dict]) -> None:
    """One race-mode tick: hit HKEX's per-stock search directly for each
    distinct racing ticker (a single ~1-2s request, vs. the full-market
    metadata scrape), and on any new filingId, instantly ping every
    notification channel before extracting/classifying it."""
    global _race_logged_unconfigured

    cfg = get_config()
    if not cfg.deepseek_api_key or not configured_channels():
        if not _race_logged_unconfigured:
            log_error(
                "daemon.race",
                "Skipping race tick: DeepSeek API key and/or notification channel not "
                "configured yet (set in the dashboard's Settings tab)",
            )
            _race_logged_unconfigured = True
        return
    _race_logged_unconfigured = False

    now = datetime.now(HKT)
    today = now.date()
    retry_cap = max(cfg.max_extraction_retries, 10)

    for ticker in sorted({t["ticker"] for t in targets}):
        state = _race_state.setdefault(
            ticker, {"failures": 0, "next_attempt": None, "alerted": False, "last_alert_at": None}
        )
        if state["next_attempt"] is not None and now < state["next_attempt"]:
            log_event(
                "daemon.race", "race.backoff", f"Skipping {ticker}: backing off until {state['next_attempt'].isoformat()}",
                level="debug", ticker=ticker,
            )
            continue  # backing off after consecutive failures for this ticker

        tick_started = time.monotonic()
        try:
            records = search_filings_by_ticker(ticker, today, today)
        except HKEXSearchError as exc:
            state["failures"] = int(state["failures"]) + 1
            delay = min(cfg.race_poll_interval_seconds * (2 ** state["failures"]), RACE_MAX_BACKOFF_SECONDS)
            state["next_attempt"] = now + timedelta(seconds=delay)
            log_error(
                "daemon.race",
                f"Race search failed for {ticker} (attempt {state['failures']}, "
                f"backing off {delay}s): {exc}",
            )
            _alert_race_unreachable(ticker, state, now, exc, cfg)
            continue

        _alert_race_recovered(ticker, state)
        state["failures"] = 0
        state["next_attempt"] = None

        duration_ms = round((time.monotonic() - tick_started) * 1000)
        log_event(
            "daemon.race", "hkex.refresh", f"HKEX refresh {ticker}: {len(records)} filing(s) today ({duration_ms} ms)",
            level="info" if records else "debug", ticker=ticker,
            meta={"count": len(records), "duration_ms": duration_ms},
        )

        for rec in records:
            filing_id = rec["filingId"]
            if notified_cache.is_seen(filing_id):
                log_event("daemon.race", "filter.dedup", f"Filing {filing_id} already processed; skipped", level="debug", ticker=ticker)
                continue

            if not notified_cache.is_pinged(filing_id):
                log_event("daemon.race", "filing.detected", f"{ticker}: new filing detected — {rec.get('title')}", ticker=ticker, meta={"filing_id": filing_id})
                try:
                    upsert_filing_metadata([rec])
                except Exception as exc:  # noqa: BLE001 - a bad row must not block the ping
                    log_error("daemon.race", f"Failed to upsert filing {filing_id}: {exc}", exc)

                ping = FilingPing(
                    ticker=ticker,
                    stock_name=rec.get("stockName"),
                    title=rec.get("title"),
                    document_url=rec.get("link"),
                    detected_at=now,
                )
                if not _send_filing_ping(filing_id, ping):
                    log_error(
                        "daemon.race",
                        f"Ping failed on all channels for filing {filing_id} (ticker {ticker}); will retry",
                    )
                    continue  # don't move to stage 2 until the ping actually lands

            log_event("daemon.race", "doc.extract", f"Downloading + extracting document for filing {filing_id}", ticker=ticker)
            try:
                document_text = extract_and_save_filing(filing_id, rec.get("link", ""))
            except DocumentExtractionError as exc:
                log_error("daemon.race", f"Extraction failed for filing {filing_id}: {exc}")
                notified_cache.record_failure(filing_id, retry_cap)
                continue
            log_event(
                "daemon.race", "doc.extract", f"Extracted {len(document_text)} chars for filing {filing_id}",
                level="debug", ticker=ticker,
            )

            _classify_and_alert(
                filing_id,
                ticker,
                document_text,
                rec.get("link"),
                rec.get("stockName"),
                retry_cap,
                "daemon.race",
                filing_date=rec.get("dateTime") or rec.get("date"),
                title=rec.get("title"),
            )


def main() -> None:
    get_config().ensure_data_dir()
    dividend_store.ensure_seeded()
    try:
        ensure_settlement_history_schema()
    except Exception as exc:  # noqa: BLE001 - a DB hiccup at startup must not prevent the daemon from starting
        log_error("daemon.sgx_archive", f"Failed to ensure SGX settlement history schema: {exc}")
    print(
        f"HKEX Dividend Monitor starting. Poll interval: {get_config().poll_interval_seconds}s. "
        f"Disclosure hours: Mon-Fri 06:00-23:00 HKT."
    )
    was_in_window = None
    was_racing = False
    last_full_cycle: datetime | None = None

    while True:
        now = datetime.now(HKT)
        cfg = get_config()
        # Touched unconditionally (not just inside run_cycle) so race mode
        # running outside disclosure hours -- fully possible with a 24h
        # race window -- doesn't let the dashboard's heartbeat go stale.
        _touch_heartbeat()

        # Independent of the dividend-watch pipeline and disclosure-hours
        # window below -- SGX publishes on its own schedule, unrelated to
        # HKEX's. Never allowed to crash the daemon loop.
        try:
            run_sgx_archive_step()
        except Exception as exc:  # noqa: BLE001 - never let a bug here kill the daemon
            log_error("daemon.sgx_archive", f"Unexpected error in SGX archive step: {exc}", exc)

        in_window = within_disclosure_hours(now)

        if in_window != was_in_window:
            state = "entered" if in_window else "exited"
            print(f"[{now.isoformat()}] {state} HKEX disclosure hours window")
            log_event("daemon.main", "window", f"{state.capitalize()} HKEX disclosure hours window")
            was_in_window = in_window

        try:
            active_targets = registry.active_targets()
        except Exception as exc:  # noqa: BLE001
            log_error("daemon.registry", f"Failed to load target registry: {exc}")
            active_targets = []

        racing = racing_targets(active_targets, now)
        is_racing = bool(racing) and _race_window_active(cfg, now)
        if is_racing != was_racing:
            tickers = sorted({t["ticker"] for t in racing}) if is_racing else []
            print(f"[{now.isoformat()}] race mode {'ON' if is_racing else 'OFF'} {tickers}")
            log_event("daemon.main", "race.mode", f"Race mode {'ON' if is_racing else 'OFF'} {tickers}")
            was_racing = is_racing

        if is_racing:
            try:
                run_race_tick(racing)
            except Exception as exc:  # noqa: BLE001 - never let a bug here kill the daemon
                log_error("daemon.race", f"Unexpected error in race tick: {exc}", exc)

            # The normal full-market cycle still runs on its own (slower)
            # cadence as a backup path -- covers non-racing targets, and a
            # safety net if HKEX's per-ticker search endpoint ever changes
            # shape underneath monitor.hkex_search.
            if in_window and (
                last_full_cycle is None
                or (now - last_full_cycle).total_seconds() >= cfg.poll_interval_seconds
            ):
                try:
                    run_cycle()
                except Exception as exc:  # noqa: BLE001 - absolute last line of defense
                    log_error("daemon.main", f"Unhandled exception in run_cycle: {exc}", exc)
                last_full_cycle = now

            # Re-read each loop so a live settings change takes effect on
            # the very next sleep, without a container restart.
            time.sleep(get_config().race_poll_interval_seconds)
            continue

        if in_window:
            try:
                run_cycle()
            except Exception as exc:  # noqa: BLE001 - absolute last line of defense
                log_error("daemon.main", f"Unhandled exception in run_cycle: {exc}", exc)
            last_full_cycle = now

        time.sleep(get_config().poll_interval_seconds)


if __name__ == "__main__":
    main()
