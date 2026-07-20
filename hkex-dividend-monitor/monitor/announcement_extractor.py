"""LLM-driven structured extraction of board-meeting / results / dividend
details from HKEX filing text, for Today's HKEX Dividend Watchlist.

This is the *only* new LLM surface the watchlist feature adds, and it is
deliberately narrow: the model extracts facts stated in the document (a
board-meeting date, a results period, a declared dividend's terms) and
classifies the filing's kind -- it never assigns a score or invents a date
that isn't in the text. Scoring lives entirely in monitor.scoring, over pure
Python features built in monitor.features from what this module extracts.

Reuses monitor.extractor's DeepSeek client, truncation, and error-handling
conventions (JSON mode, temperature=0, catch-and-log-never-raise) rather
than duplicating them.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from monitor.diagnostics import log_error
from monitor.extractor import ExtractionError, _client, _escalate, _truncate

SYSTEM_PROMPT = """You are a deterministic financial-document extraction engine for Hong Kong \
Stock Exchange (HKEX) regulatory filings. You will be given the raw text of one filing.

Your task: classify the filing's primary nature and extract structured facts that are \
EXPLICITLY STATED in the text. Never infer, guess, or fabricate a value that is not \
written in the document -- use null whenever something is not stated.

Respond with ONLY a single JSON object matching this exact schema -- no markdown, no \
commentary, no code fences:

{
  "event_kind": "board_meeting" | "results" | "dividend" | "other",
  "company_name": string or null,
  "board_meeting_date": string ("YYYY-MM-DD") or null,
  "board_meeting_purpose_approves_results": boolean,
  "board_meeting_purpose_considers_dividend": boolean,
  "board_meeting_purpose_raw": string or null,
  "results_period": "annual" | "interim" | "quarterly" | null,
  "dividend_type": "final" | "interim" | "special" | "quarterly" | null,
  "dividend_amount": string or null,
  "declared_date": string ("YYYY-MM-DD") or null,
  "ex_date": string ("YYYY-MM-DD") or null,
  "record_date": string ("YYYY-MM-DD") or null,
  "payment_date": string ("YYYY-MM-DD") or null,
  "ambiguous": boolean
}

Field rules:
- "event_kind": "board_meeting" if this is a notice announcing an upcoming board meeting \
(a future-dated meeting the board will hold). "results" if this is an annual/interim/\
quarterly results announcement (results already released, not just scheduled). "dividend" \
if this filing itself declares/announces a dividend or distribution. "other" for anything \
else (circulars, notices, proxy forms, etc).
- "board_meeting_date": ONLY for a "board meeting notice" filing -- the future date on \
which the board will meet, exactly as stated. null if this filing does not announce an \
upcoming board meeting.
- "board_meeting_purpose_approves_results": true only if the stated purpose of the board \
meeting explicitly includes approving/considering annual, interim, or quarterly results.
- "board_meeting_purpose_considers_dividend": true only if the stated purpose of the board \
meeting explicitly includes considering, declaring, or recommending a dividend or \
distribution.
- "board_meeting_purpose_raw": the purpose text as stated (verbatim or lightly trimmed). \
null if not applicable.
- "results_period": which reporting period this results announcement covers. null if \
event_kind is not "results".
- "dividend_type": the kind of dividend declared. null if event_kind is not "dividend" or \
not stated.
- "dividend_amount": the per-share distribution value exactly as stated, including currency \
(e.g. "HKD 0.45"). null if not applicable or not found.
- "declared_date", "ex_date", "record_date", "payment_date": dates exactly as stated in the \
filing, in strict "YYYY-MM-DD" format. null if not stated.
- "ambiguous": true only if you are genuinely unsure which event_kind applies, or the key \
dates/amounts are stated inconsistently, conditionally, or unclearly, such that a more \
careful re-read could reasonably reach a different answer. false whenever the filing is a \
clear, unambiguous read -- this is the common case.

Output must be valid JSON and nothing else."""

MAX_DOCUMENT_CHARS = 60_000


class AnnouncementExtraction(BaseModel):
    event_kind: str = Field(default="other")
    company_name: Optional[str] = None
    board_meeting_date: Optional[str] = None
    board_meeting_purpose_approves_results: bool = False
    board_meeting_purpose_considers_dividend: bool = False
    board_meeting_purpose_raw: Optional[str] = None
    results_period: Optional[str] = None
    dividend_type: Optional[str] = None
    dividend_amount: Optional[str] = None
    declared_date: Optional[str] = None
    ex_date: Optional[str] = None
    record_date: Optional[str] = None
    payment_date: Optional[str] = None
    ambiguous: bool = False


_BOARD_MEETING_KEYWORDS = ("board meeting",)
_RESULTS_KEYWORDS = ("annual results", "interim results", "quarterly results", "final results")
_DIVIDEND_KEYWORDS = ("dividend", "distribution")


def classify_title(title: str) -> str:
    """Cheap deterministic pre-classification from the filing title alone --
    no LLM call, no document download. Used by monitor.watchlist to skip
    extract_announcement on titles that are obviously irrelevant, and as a
    fallback event_kind if the LLM call itself fails."""
    lowered = (title or "").lower()
    if any(kw in lowered for kw in _BOARD_MEETING_KEYWORDS):
        return "board_meeting"
    if any(kw in lowered for kw in _RESULTS_KEYWORDS):
        return "results"
    if any(kw in lowered for kw in _DIVIDEND_KEYWORDS):
        return "dividend"
    return "other"


def extract_announcement(filing_id: str, title: str, document_text: str) -> AnnouncementExtraction:
    """Call the LLM and return a validated AnnouncementExtraction.

    Raises ExtractionError on any failure; callers (monitor.watchlist) should
    catch this, log it, fall back to classify_title(title) for a coarse
    event_kind, and continue -- a single bad filing must never abort
    watchlist generation.

    Escalates once to the reasoning-tier model (see monitor.extractor._escalate)
    when the fast tier flags its own answer `ambiguous`. A failed escalation
    attempt falls back to the fast-tier result rather than raising -- that
    result is already valid, and losing it over a failed upgrade would be worse.
    """
    if not document_text or not document_text.strip():
        raise ExtractionError("empty document_text")

    text, was_truncated = _truncate(document_text)
    if was_truncated:
        log_error(
            "announcement_extractor",
            f"Document for filing {filing_id} truncated to {MAX_DOCUMENT_CHARS} chars before LLM call",
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Filing title: {title}\n\n{text}"},
    ]

    try:
        client = _client()
        from monitor.config import get_config

        response = client.chat.completions.create(
            model=get_config().deepseek_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001 - any SDK/network error must be caught
        raise ExtractionError(f"DeepSeek API call failed for filing {filing_id}: {exc}") from exc

    raw_content = None
    try:
        raw_content = response.choices[0].message.content
        data = json.loads(raw_content)
    except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
        raise ExtractionError(
            f"Failed to parse LLM JSON response for filing {filing_id}: {exc}. "
            f"Raw content: {str(raw_content)[:1000]}"
        ) from exc

    try:
        result = AnnouncementExtraction.model_validate(data)
    except ValidationError as exc:
        raise ExtractionError(
            f"LLM response failed schema validation for filing {filing_id}: {exc}. Raw: {data}"
        ) from exc

    if not result.ambiguous:
        return result
    try:
        return _escalate(AnnouncementExtraction, messages, filing_id)
    except ExtractionError as exc:
        log_error(
            "announcement_extractor",
            f"Filing {filing_id}: reasoning-tier escalation failed ({exc}); using the fast-tier result instead",
        )
        return result


def explain(company_name: str, ticker: str, reasons: list[dict]) -> Optional[str]:
    """Render a short, human-readable explanation of a watchlist ranking
    from its deterministic reasons list. Explanation only -- never used to
    assign or adjust the score. Returns None on any failure (caught here so
    a DeepSeek hiccup never blocks watchlist generation); callers should
    fall back to listing the raw reasons when this returns None.
    """
    if not reasons:
        return None
    try:
        client = _client()
        from monitor.config import get_config

        summary = "; ".join(
            f"{r.get('label', 'Derived')}: {r.get('signal', '')} ({r.get('evidence', '')})" for r in reasons
        )
        response = client.chat.completions.create(
            model=get_config().deepseek_model,
            temperature=0,
            max_tokens=120,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write one concise, factual sentence (max 40 words) explaining why a "
                        "HKEX-listed company was ranked on a dividend-announcement watchlist, using "
                        "ONLY the signals given. Do not invent dates, amounts, or facts not present "
                        "in the signals. Do not assign or mention a numeric score."
                    ),
                },
                {"role": "user", "content": f"Company: {company_name} ({ticker}). Signals: {summary}"},
            ],
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - explanation is best-effort, never fatal
        log_error("announcement_extractor.explain", f"Explanation generation failed for {ticker}: {exc}")
        return None
