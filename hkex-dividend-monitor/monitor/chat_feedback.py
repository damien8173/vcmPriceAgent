"""User-flagged chat turns, appended as JSON Lines for offline review.

The dashboard's Chat tab puts a thumbs-down button on every assistant
reply; clicking it records the COMPLETE turn here -- the question, every
tool call with its raw arguments and raw result exactly as the model saw
them, the reply, the preceding conversation, and an optional user note
about what was wrong. Capturing the raw tool results is the point: it's
what lets a later reader distinguish "the tool returned bad data" from
"the data was right and the model misread it" -- the first question
every hallucination hunt in this project has had to answer.

Intended workflow (documented in the README's Chat section): flag bad
replies as they happen, download the file from the Chat tab, hand it to
an AI coding session in this repo ("each line is one flagged chat turn;
diagnose each and fix the causes"), then clear the log and repeat.

One self-contained JSON object per line, append-only. Deliberately
file-based rather than SurrealDB: feedback must be recordable while the
DB is down, and the whole point of the file is to be downloaded and read
outside this app. Unlike monitor.activity/diagnostics logging (best-
effort, never raises), a failed write here DOES raise -- recording the
flag is the entire operation the user asked for, so a silent loss would
be worse than an error message.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from monitor.config import CHAT_FEEDBACK_FILE, HKT

# A turn's tool results are each already capped at ~12K chars by the chat
# layer, so a genuine entry is at most a few hundred KB -- anything bigger
# is a malformed/hostile request, not real feedback.
MAX_ENTRY_BYTES = 2_000_000


def record_feedback(
    user_message: str,
    reply: str,
    *,
    note: Optional[str] = None,
    tool_activity: Optional[list[dict[str, Any]]] = None,
    prior_transcript: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Append one flagged turn; returns the stored entry.

    Raises ValueError for an oversized entry and OSError on write failure
    -- see module docstring for why this must not be best-effort.
    """
    entry: dict[str, Any] = {
        "flaggedAt": datetime.now(HKT).isoformat(),
        "userMessage": user_message,
        "reply": reply,
    }
    if note:
        entry["note"] = note
    if tool_activity:
        entry["toolActivity"] = tool_activity
    if prior_transcript:
        entry["priorTranscript"] = prior_transcript

    line = json.dumps(entry, ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_ENTRY_BYTES:
        raise ValueError(
            "feedback entry is implausibly large -- a real turn's tool results "
            "are size-capped well below this"
        )

    CHAT_FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHAT_FEEDBACK_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return entry


def feedback_stats() -> dict[str, Any]:
    """{"count": flagged turns, "bytes": file size} -- {0, 0} when the
    file doesn't exist yet (nothing flagged, or just cleared)."""
    try:
        size = CHAT_FEEDBACK_FILE.stat().st_size
        with open(CHAT_FEEDBACK_FILE, "r", encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
    except OSError:
        return {"count": 0, "bytes": 0}
    return {"count": count, "bytes": size}


def clear_feedback() -> int:
    """Delete the log (meant for after the user has downloaded and acted
    on it). Returns how many entries were discarded; already-missing is 0,
    not an error."""
    removed = feedback_stats()["count"]
    try:
        CHAT_FEEDBACK_FILE.unlink()
    except FileNotFoundError:
        return 0
    return removed
