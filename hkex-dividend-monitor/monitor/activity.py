"""Append-only, cross-process activity feed for the Dashboard's live
Activity Log panel.

The daemon (monitor/daemon.py) and the web app (monitor/web.py) run as
separate processes -- separate Docker containers, or separate subprocesses
under scripts/run_local.py -- sharing state only through files in
DATA_DIR. This module is that shared channel for "what is the monitor
doing right now": both processes append JSON lines here (HKEX refreshes,
parsing, LLM classification, notification decisions, retries, ...) and the
web app's GET /api/activity tails the file for the dashboard to poll.

Deliberately separate from monitor.diagnostics (data/diagnostics.log):
that file is an established, documented error log surfaced elsewhere in
the UI, and mixing routine per-tick activity into it would both bloat it
past its 5,000-line cap within a single race day and blur its "something
went wrong" meaning. monitor.diagnostics.log_error mirrors an error-level
event in here instead, so existing error call sites show up in the feed
for free without duplicate instrumentation.

Like log_error, log_event must never raise -- a logging failure must
never crash the daemon or web request it's trying to describe.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Any, Optional

from monitor.config import ACTIVITY_FILE, HKT

# Race mode polls as often as every 30s per ticker (race_poll_interval_seconds
# default); an 8h race window across a handful of tickers can produce several
# thousand events per day, so this needs a much bigger cap than diagnostics.log
# -- sized to comfortably retain a full race day, still rewritten in one atomic
# pass (see _rotate_if_needed) so it never grows unbounded on a 24/7 deployment.
MAX_ACTIVITY_BYTES = 10 * 1024 * 1024
MAX_ACTIVITY_LINES = 20_000

_LEVEL_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}


def _rotate_if_needed() -> None:
    try:
        if ACTIVITY_FILE.stat().st_size <= MAX_ACTIVITY_BYTES:
            return
        with open(ACTIVITY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_ACTIVITY_LINES:
            return

        # Atomic replace (temp file + os.replace), same as diagnostics.py --
        # both the daemon and web processes can append concurrently, so a
        # fixed temp filename would risk two rotations colliding.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(ACTIVITY_FILE.parent), prefix=f".{ACTIVITY_FILE.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_ACTIVITY_LINES:])
            os.replace(tmp_name, ACTIVITY_FILE)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        pass  # best-effort -- rotation itself must never break logging


def log_event(
    source: str,
    kind: str,
    message: str,
    *,
    level: str = "info",
    ticker: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """Append one activity event. Never raises."""
    entry: dict[str, Any] = {
        "timestamp": datetime.now(HKT).isoformat(),
        "source": source,
        "kind": kind,
        "level": level,
        "message": message,
    }
    if ticker:
        entry["ticker"] = ticker
    if meta:
        entry["meta"] = meta

    try:
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTIVITY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate_if_needed()
    except OSError:
        # Last resort: stderr. Still must not raise.
        print(f"[activity-log-failed] {source}: {message}")


def read_recent(limit: int = 200, min_level: Optional[str] = None) -> list[dict[str, Any]]:
    """Return up to `limit` most recent events, newest first.

    Tail-reads instead of loading the whole (up to 10MB) file: seeks near
    the end based on a generous per-line size estimate, since the file is
    already capped in size by _rotate_if_needed.
    """
    try:
        size = ACTIVITY_FILE.stat().st_size
    except OSError:
        return []

    min_rank = _LEVEL_RANK.get(min_level, -1) if min_level else -1
    # 1KB/line is deliberately generous: mirrored error events carry full
    # exception messages and routinely exceed 512 bytes, and under-reading
    # would silently return fewer than `limit` during an error storm.
    read_size = max(64 * 1024, limit * 1024)

    try:
        with open(ACTIVITY_FILE, "rb") as f:
            if size > read_size:
                f.seek(size - read_size)
                f.readline()  # drop a possibly-partial first line
            raw = f.read()
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a rotation race can truncate a line -- skip, don't fail the feed
        if _LEVEL_RANK.get(entry.get("level"), 1) < min_rank:
            continue
        events.append(entry)

    events.reverse()  # file is oldest-first; feed is newest-first
    return events[:limit]
