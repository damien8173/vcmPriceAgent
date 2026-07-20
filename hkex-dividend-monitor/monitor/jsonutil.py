"""Shared atomic JSON read/write helpers.

Extracted so both monitor.config (settings.json) and monitor.registry
(targets/notified/alert-history) can use the same atomic-write
primitive without a circular import between them.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """Write `data` to `path` atomically (temp file + os.replace).

    os.replace is atomic on both POSIX and Windows/NTFS, so a crash or
    concurrent read mid-write can never observe a corrupt/partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    """Load JSON from `path`, seeding it with `default` (written atomically) if missing."""
    if not path.exists():
        atomic_write_json(path, default)
        return json.loads(json.dumps(default))
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def to_iso_date_str(value: Any) -> str:
    """Best-effort YYYY-MM-DD from a filing date, which shows up in two
    shapes depending on which path ingested it: an ISO datetime string
    (SurrealDB's filingDate) or HKEX's own "DD/MM/YYYY" or
    "DD/MM/YYYY HH:MM" string (race mode / search_hkex_by_ticker). Returns
    "" for falsy input.
    """
    if not value:
        return ""
    s = str(value).strip()
    date_part = s.split(" ")[0]  # drop a trailing " HH:MM" before checking format
    if "/" in date_part:
        parts = date_part.split("/")
        if len(parts) == 3:
            dd, mm, yyyy = parts
            return f"{yyyy}-{mm}-{dd}"
        return s
    return s[:10]
