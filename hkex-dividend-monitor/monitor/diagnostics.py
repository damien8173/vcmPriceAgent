"""Append-only diagnostics log, size-capped. Never raises -- logging
failures must not crash the daemon they're trying to keep alive."""
from __future__ import annotations

import json
import os
import tempfile
import traceback
from datetime import datetime

from monitor import activity
from monitor.config import DIAGNOSTICS_FILE, HKT

# Unlike alert_history.json/dividends.json (capped at 200/500 entries on
# every write), this file is plain append-only text with no natural place
# to trim -- so instead it's checked cheaply (one stat() call) on every
# write, and only rewritten once it actually exceeds this size, keeping the
# most recent MAX_DIAGNOSTICS_LINES lines. Without this, a 24/7 unattended
# deployment would grow this file forever.
MAX_DIAGNOSTICS_BYTES = 5 * 1024 * 1024
MAX_DIAGNOSTICS_LINES = 5_000


def _rotate_if_needed() -> None:
    try:
        if DIAGNOSTICS_FILE.stat().st_size <= MAX_DIAGNOSTICS_BYTES:
            return
        with open(DIAGNOSTICS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_DIAGNOSTICS_LINES:
            return

        # Atomic replace (temp file + os.replace) since both the daemon and
        # web processes can call log_error concurrently -- a fixed temp
        # filename would risk two rotations colliding.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(DIAGNOSTICS_FILE.parent), prefix=f".{DIAGNOSTICS_FILE.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_DIAGNOSTICS_LINES:])
            os.replace(tmp_name, DIAGNOSTICS_FILE)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        pass  # best-effort -- rotation itself must never break logging


def log_error(source: str, message: str, exc: BaseException | None = None) -> None:
    entry = {
        "timestamp": datetime.now(HKT).isoformat(),
        "source": source,
        "message": message,
    }
    if exc is not None:
        entry["exception"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )

    try:
        DIAGNOSTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(DIAGNOSTICS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate_if_needed()
    except OSError:
        # Last resort: stderr. Still must not raise.
        print(f"[diagnostics-log-failed] {source}: {message}")

    # Mirror into the Activity Log feed so every existing log_error call
    # site (race failures, webhook failures, extraction errors, ...) shows
    # up there for free, without needing separate instrumentation.
    try:
        activity.log_event(source, "error", message, level="error")
    except Exception:
        pass
