"""Target watchlist, notified-filing dedup cache, and dispatched-alert history.

All three files are plain JSON under DATA_DIR and are written atomically
via monitor.jsonutil (temp file + os.replace) so a crash or a concurrent
reader (e.g. the web dashboard) can never observe a corrupt/partial file
-- this matters cross-platform, os.replace is atomic on both POSIX and
Windows/NTFS.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.config import (
    ALERT_HISTORY_FILE,
    CHANNEL_HEALTH_FILE,
    DIVIDENDS_FILE,
    HKT,
    NOTIFIED_FILE,
    TARGETS_FILE,
    WATCHLIST_TICKERS_FILE,
)
from monitor.jsonutil import atomic_write_json as _atomic_write_json
from monitor.jsonutil import load_json as _load_json

VALID_STATUSES = ("active", "inactive")

# Deliberately empty: a fresh install starts with no watch targets. This
# used to seed a real (ticker, date) pair, which left every new install
# with a stale "pending" target that also widened the daemon's scrape
# window forever once its date passed.
DEFAULT_TARGETS: list[dict[str, Any]] = []

DEFAULT_NOTIFIED: dict[str, Any] = {
    "notified": [],
    "processed": [],
    "failed": {},
    "pinged": [],
}


def normalize_ticker(ticker: str) -> str:
    """HKEX stock codes are zero-padded to 5 digits (e.g. 700 -> 00700)."""
    ticker = ticker.strip().upper()
    digits = "".join(ch for ch in ticker if ch.isdigit())
    if not digits:
        raise ValueError(f"Invalid ticker: {ticker!r}")
    return digits.zfill(5)


def validate_date(value: str) -> str:
    """Validate an ISO date string (YYYY-MM-DD), returning it normalized."""
    parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    return parsed.isoformat()


class TargetRegistry:
    """Manages hkex_targets.json -- the active watchlist."""

    def __init__(self, path: Path = TARGETS_FILE) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        return _load_json(self.path, DEFAULT_TARGETS)

    def save(self, targets: list[dict[str, Any]]) -> None:
        _atomic_write_json(self.path, targets)

    def active_targets(self) -> list[dict[str, Any]]:
        return [t for t in self.load() if t.get("status") == "active"]

    def add_target(self, ticker: str, target_date: str, status: str = "active") -> dict[str, Any]:
        ticker = normalize_ticker(ticker)
        target_date = validate_date(target_date)
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")

        targets = self.load()
        for t in targets:
            if t["ticker"] == ticker and t["target_date"] == target_date:
                t["status"] = status
                self.save(targets)
                return t

        entry = {"ticker": ticker, "target_date": target_date, "status": status}
        targets.append(entry)
        self.save(targets)
        return entry

    def set_status(self, ticker: str, status: str) -> int:
        ticker = normalize_ticker(ticker)
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        targets = self.load()
        changed = 0
        for t in targets:
            if t["ticker"] == ticker:
                t["status"] = status
                changed += 1
        if changed:
            self.save(targets)
        return changed

    def remove_target(self, ticker: str) -> int:
        ticker = normalize_ticker(ticker)
        targets = self.load()
        remaining = [t for t in targets if t["ticker"] != ticker]
        removed = len(targets) - len(remaining)
        if removed:
            self.save(remaining)
        return removed


class NotifiedCache:
    """Manages notified_filings.json -- dedup + failure tracking."""

    # Every other store here is capped (alerts 200, dividends 500) but these
    # ID lists used to grow forever on a 24/7 deployment. Dedup only needs
    # recent memory: a filing older than the last few thousand can't come
    # back through any ingestion path (scrape windows and race mode both
    # look at recent dates only), so trimming the OLDEST ids is safe.
    MAX_IDS_PER_LIST = 5_000

    def __init__(self, path: Path = NOTIFIED_FILE) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        data = _load_json(self.path, DEFAULT_NOTIFIED)
        data.setdefault("notified", [])
        data.setdefault("processed", [])
        data.setdefault("failed", {})
        data.setdefault("pinged", [])
        return data

    def save(self, data: dict[str, Any]) -> None:
        for key in ("notified", "processed", "pinged"):
            if len(data.get(key, [])) > self.MAX_IDS_PER_LIST:
                data[key] = data[key][-self.MAX_IDS_PER_LIST :]
        _atomic_write_json(self.path, data)

    def is_seen(self, filing_id: str) -> bool:
        data = self.load()
        return filing_id in data["notified"] or filing_id in data["processed"]

    def is_pinged(self, filing_id: str) -> bool:
        """Race mode's stage-1 instant ping: separate from is_seen so a
        filing can be pinged immediately, then still go through the normal
        stage-2 extract/notify/processed flow exactly once."""
        return filing_id in self.load()["pinged"]

    def mark_pinged(self, filing_id: str) -> None:
        data = self.load()
        if filing_id not in data["pinged"]:
            data["pinged"].append(filing_id)
        self.save(data)

    def mark_notified(self, filing_id: str) -> None:
        data = self.load()
        if filing_id not in data["notified"]:
            data["notified"].append(filing_id)
        data["failed"].pop(filing_id, None)
        self.save(data)

    def mark_processed_no_alert(self, filing_id: str) -> None:
        """LLM determined this filing is not a dividend announcement."""
        data = self.load()
        if filing_id not in data["processed"]:
            data["processed"].append(filing_id)
        data["failed"].pop(filing_id, None)
        self.save(data)

    def record_failure(self, filing_id: str, max_retries: int) -> int:
        """Increment failure count; move to `processed` (give up) once max_retries hit.

        Returns the new attempt count.
        """
        data = self.load()
        attempts = data["failed"].get(filing_id, 0) + 1
        if attempts >= max_retries:
            data["failed"].pop(filing_id, None)
            if filing_id not in data["processed"]:
                data["processed"].append(filing_id)
        else:
            data["failed"][filing_id] = attempts
        self.save(data)
        return attempts


class AlertHistory:
    """Manages alert_history.json -- a human-readable feed of dispatched
    alerts for the web dashboard's "Recent alerts" panel."""

    MAX_ENTRIES = 200

    def __init__(self, path: Path = ALERT_HISTORY_FILE) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        return _load_json(self.path, [])

    def append(self, entry: dict[str, Any]) -> None:
        history = self.load()
        history.append(entry)
        if len(history) > self.MAX_ENTRIES:
            history = history[-self.MAX_ENTRIES :]
        _atomic_write_json(self.path, history)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        history = self.load()
        return list(reversed(history[-limit:]))


class DividendStore:
    """Manages dividends.json -- every dividend the monitor has detected,
    independent of whether the alert actually dispatched (AlertHistory only
    records alerts that succeeded on at least one channel). Powers the web
    dashboard's Dividends tab and the chat's list_dividends tool."""

    MAX_ENTRIES = 500

    def __init__(self, path: Path = DIVIDENDS_FILE) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        return _load_json(self.path, [])

    def save(self, records: list[dict[str, Any]]) -> None:
        _atomic_write_json(self.path, records)

    def mark_dividend(self, record: dict[str, Any]) -> None:
        """Append a detected dividend, deduped by filingId (a filing is
        classified at most once per notified_cache, but this guards against
        any future caller re-recording the same filing)."""
        records = self.load()
        filing_id = record.get("filingId")
        if filing_id and any(r.get("filingId") == filing_id for r in records):
            return
        records.append(record)
        if len(records) > self.MAX_ENTRIES:
            records = records[-self.MAX_ENTRIES :]
        self.save(records)

    def recent(self, limit: int = 100, ticker: str | None = None) -> list[dict[str, Any]]:
        records = self.load()
        if ticker:
            records = [r for r in records if r.get("ticker") == ticker]
        return list(reversed(records[-limit:]))

    def ensure_seeded(self) -> None:
        """One-time migration: if dividends.json doesn't exist yet, backfill
        it from alert_history.json so the table isn't empty for dividends
        detected before this store existed. No-op once the file exists --
        safe to call on every daemon/web startup."""
        if self.path.exists():
            return

        records: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for a in AlertHistory().load():
            url = a.get("source_url")
            if url:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
            records.append(
                {
                    "filingId": None,
                    "ticker": a.get("ticker"),
                    "stockName": a.get("company_name"),
                    "payoutAmount": a.get("payout_amount"),
                    "exDividendDate": a.get("ex_dividend_date"),
                    "paymentDate": a.get("payment_date"),
                    # Best-effort: alert_history.json predates per-filing release
                    # times, so fall back to when the alert was dispatched.
                    "filingDate": a.get("timestamp"),
                    "documentUrl": url,
                    "detectedAt": a.get("timestamp"),
                }
            )
        self.save(records)


class WatchlistTickers:
    """Manages watchlist_tickers.json -- the user-curated ticker list for
    Today's HKEX Dividend Watchlist (monitor/watchlist.py).

    Deliberately separate from TargetRegistry: an alert target is a
    (ticker, exact date) pair that fires once, while this is a plain set of
    tickers to *rank* every day with no date attached. Entries are
    {"ticker": "00005", "name": "HSBC Holdings plc" | null} -- name is a
    best-effort display convenience captured at add time, never required.
    """

    def __init__(self, path: Path = WATCHLIST_TICKERS_FILE) -> None:
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        return _load_json(self.path, [])

    def save(self, entries: list[dict[str, Any]]) -> None:
        _atomic_write_json(self.path, entries)

    def tickers(self) -> list[str]:
        return [e["ticker"] for e in self.load() if e.get("ticker")]

    def add(self, ticker: str, name: str | None = None) -> dict[str, Any]:
        ticker = normalize_ticker(ticker)
        entries = self.load()
        for e in entries:
            if e.get("ticker") == ticker:
                if name and not e.get("name"):
                    e["name"] = name  # backfill a name we didn't have before
                    self.save(entries)
                return e
        entry = {"ticker": ticker, "name": name}
        entries.append(entry)
        self.save(entries)
        return entry

    def remove(self, ticker: str) -> int:
        ticker = normalize_ticker(ticker)
        entries = self.load()
        remaining = [e for e in entries if e.get("ticker") != ticker]
        removed = len(entries) - len(remaining)
        if removed:
            self.save(remaining)
        return removed


class ChannelHealth:
    """Tracks the most recent delivery outcome per notification channel
    (slack/discord/telegram).

    monitor.notifier.configured_channels() only reports whether a webhook
    URL/token *string is set* -- it says nothing about whether that channel
    is actually delivering. This store answers that: monitor.notifier's
    dispatch_text records a result here on every send attempt (real alerts
    and the manual "Send test alert" button alike), so the Dashboard can
    show e.g. "last delivered 2 min ago" instead of just "configured", and
    surface a channel that's silently started failing (revoked webhook,
    deleted bot, etc.) instead of that going unnoticed indefinitely.
    """

    def __init__(self, path: Path = CHANNEL_HEALTH_FILE) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        return _load_json(self.path, {})

    def save(self, data: dict[str, Any]) -> None:
        _atomic_write_json(self.path, data)

    def record(self, channel: str, ok: bool) -> None:
        data = self.load()
        entry = data.setdefault(
            channel, {"last_success_at": None, "last_failure_at": None, "consecutive_failures": 0}
        )
        now = datetime.now(HKT).isoformat()
        if ok:
            entry["last_success_at"] = now
            entry["consecutive_failures"] = 0
        else:
            entry["last_failure_at"] = now
            entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
        data[channel] = entry
        self.save(data)
