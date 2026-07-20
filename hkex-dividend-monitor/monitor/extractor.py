"""LLM-driven structured extraction of dividend details from filing text.

Uses DeepSeek's OpenAI-compatible Chat Completions API with JSON mode,
validated against a strict Pydantic schema. Any failure (API error,
malformed JSON, schema violation) is caught and logged to the
diagnostics file rather than raised -- the daemon must never crash on
a single bad filing.
"""
from __future__ import annotations

import json
from typing import Optional

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from monitor.config import get_config
from monitor.diagnostics import log_error

SYSTEM_PROMPT = """You are a deterministic financial-document extraction engine for Hong Kong \
Stock Exchange (HKEX) regulatory filings. You will be given the raw text of one filing.

Your task: determine if this filing is a dividend / distribution announcement, and if so, \
extract structured data from it.

Respond with ONLY a single JSON object matching this exact schema -- no markdown, no \
commentary, no code fences:

{
  "is_dividend_announcement": boolean,
  "company_name": string or null,
  "payout_amount": string or null,
  "ex_dividend_date": string or null,
  "payment_date": string or null,
  "ambiguous": boolean
}

Field rules:
- "is_dividend_announcement": true only if the filing announces a cash or stock dividend, \
distribution, or similar payout to shareholders. false for all other filing types (annual \
reports, results announcements without a declared dividend, circulars, notices, etc).
- "company_name": the formal registered name of the issuer as it appears in the filing. \
null if is_dividend_announcement is false or the name cannot be determined.
- "payout_amount": the per-share distribution value exactly as stated, including currency \
(e.g. "HKD 0.45", "$0.12 per share"). null if not applicable or not found.
- "ex_dividend_date": the ex-dividend date in strict "YYYY-MM-DD" format. null if not \
stated or not applicable.
- "payment_date": the distribution/payment date in strict "YYYY-MM-DD" format. null if not \
stated or not applicable.
- "ambiguous": true only if you are genuinely unsure whether this is a dividend announcement, \
or the payout amount/dates are stated inconsistently, conditionally, or unclearly, such that a \
more careful re-read could reasonably reach a different answer. false whenever the filing is a \
clear, unambiguous read either way -- this is the common case.

If is_dividend_announcement is false, set all other fields to null. Never guess or \
fabricate values -- use null when the filing does not state something explicitly. \
Output must be valid JSON and nothing else."""

MAX_DOCUMENT_CHARS = 60_000
_HEAD_CHARS = 40_000
_TAIL_CHARS = 18_000

# Without an explicit timeout the OpenAI SDK defaults to 600s per attempt
# (x2 retries) -- one hung DeepSeek call would freeze the daemon's race
# tick for every racing ticker for up to half an hour, in exactly the
# window race mode exists to win. Generous enough for a 60k-char filing;
# the caller's retry accounting (NotifiedCache.record_failure) handles a
# timed-out filing on the next cycle. monitor/chat.py sets its own,
# shorter budget (DEEPSEEK_CALL_TIMEOUT_SECONDS) for interactive use.
LLM_CALL_TIMEOUT_SECONDS = 120

# Escalation-tier timeout (see _escalate below) -- longer than the fast tier's
# because DeepSeek's thinking mode spends extra tokens on chain-of-thought
# before answering. Not yet live-measured against real filings; a reasonable
# starting point worth tightening or loosening once escalation has actually
# run in production (kept well under the dashboard's 180s manual "Refresh
# Watchlist" client timeout, since monitor.watchlist can call this per filing
# inside that one request).
REASONING_CALL_TIMEOUT_SECONDS = 150

# DeepSeek's thinking mode ignores temperature/top_p/presence_penalty/
# frequency_penalty entirely (accepted without error, but silently a no-op)
# -- see https://api-docs.deepseek.com/guides/thinking_mode/ -- so _escalate
# below omits temperature=0 rather than pass something that would no-op, and
# requests reasoning via reasoning_effort + the `thinking` extra_body flag
# instead, exactly matching DeepSeek's own documented call shape. "high"
# rather than "max": a deliberately bounded middle tier for "ambiguous", not
# the most expensive/slowest setting.
_REASONING_EFFORT = "high"
_REASONING_EXTRA_BODY = {"thinking": {"type": "enabled"}}


class DividendExtraction(BaseModel):
    is_dividend_announcement: bool
    company_name: Optional[str] = Field(default=None)
    payout_amount: Optional[str] = Field(default=None)
    ex_dividend_date: Optional[str] = Field(default=None)
    payment_date: Optional[str] = Field(default=None)
    ambiguous: bool = Field(default=False)


class ExtractionError(RuntimeError):
    pass


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_DOCUMENT_CHARS:
        return text, False
    head = text[:_HEAD_CHARS]
    tail = text[-_TAIL_CHARS:]
    truncated = f"{head}\n\n[... document truncated for length ...]\n\n{tail}"
    return truncated, True


def _client(timeout: float = LLM_CALL_TIMEOUT_SECONDS) -> OpenAI:
    cfg = get_config()
    if not cfg.deepseek_api_key:
        raise ExtractionError("DEEPSEEK_API_KEY is not set")
    return OpenAI(
        api_key=cfg.deepseek_api_key,
        base_url=cfg.deepseek_base_url,
        timeout=timeout,
        max_retries=1,
    )


def test_deepseek_connection() -> None:
    """Make a minimal, cheap call to verify the configured DeepSeek
    credentials/base_url/model actually work.

    Unlike checking whether cfg.deepseek_api_key is merely a non-empty
    string (see monitor.config.masked_settings, used for the Dashboard's
    "DeepSeek key: Configured" indicator), this actually exercises the API
    -- a typo'd or expired key otherwise silently blocks every dividend
    detection with no visible sign anything is wrong. Raises
    ExtractionError on any failure; callers should catch this and report it
    (e.g. web.py's /api/test-deepseek).
    """
    client = _client()
    try:
        client.chat.completions.create(
            model=get_config().deepseek_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:  # noqa: BLE001 - any SDK/network error must be caught
        raise ExtractionError(f"DeepSeek connection test failed: {exc}") from exc


def _escalate(schema_cls: type[BaseModel], messages: list[dict[str, str]], filing_id: str) -> BaseModel:
    """Retry the same extraction with the reasoning-tier model
    (Config.deepseek_reasoning_model) -- called when a fast-tier result
    flags itself `ambiguous`. Shared by extract_dividend_info and
    monitor.announcement_extractor.extract_announcement, which both call
    this identically after their own (unchanged) fast-tier call/parse.

    Raises ExtractionError on any failure (bad API call, malformed JSON,
    schema violation); callers should catch this and fall back to the
    already-valid fast-tier result rather than losing it over a failed
    upgrade attempt -- an ambiguous-but-present answer beats none at all.
    """
    client = _client(timeout=REASONING_CALL_TIMEOUT_SECONDS)
    try:
        response = client.chat.completions.create(
            model=get_config().deepseek_reasoning_model,
            reasoning_effort=_REASONING_EFFORT,
            extra_body=_REASONING_EXTRA_BODY,
            response_format={"type": "json_object"},
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001 - any SDK/network error must be caught
        raise ExtractionError(f"DeepSeek reasoning-tier escalation failed for filing {filing_id}: {exc}") from exc

    raw_content = None
    try:
        raw_content = response.choices[0].message.content
        data = json.loads(raw_content)
    except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
        raise ExtractionError(
            f"Failed to parse reasoning-tier JSON response for filing {filing_id}: {exc}. "
            f"Raw content: {str(raw_content)[:1000]}"
        ) from exc

    try:
        return schema_cls.model_validate(data)
    except ValidationError as exc:
        raise ExtractionError(
            f"Reasoning-tier response failed schema validation for filing {filing_id}: {exc}. Raw: {data}"
        ) from exc


def extract_dividend_info(filing_id: str, document_text: str) -> DividendExtraction:
    """Call the LLM and return a validated DividendExtraction.

    Raises ExtractionError on any failure; callers should catch this,
    log it, and continue -- never let a bad filing kill the daemon.

    Escalates once to the reasoning-tier model (see _escalate) when the fast
    tier flags its own answer `ambiguous`. A failed escalation attempt falls
    back to the fast-tier result rather than raising -- that result is
    already valid, and losing it over a failed upgrade would be worse.
    """
    if not document_text or not document_text.strip():
        raise ExtractionError("empty document_text")

    text, was_truncated = _truncate(document_text)
    if was_truncated:
        log_error(
            "extractor",
            f"Document for filing {filing_id} truncated to {MAX_DOCUMENT_CHARS} chars before LLM call",
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]

    try:
        client = _client()
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
        result = DividendExtraction.model_validate(data)
    except ValidationError as exc:
        raise ExtractionError(
            f"LLM response failed schema validation for filing {filing_id}: {exc}. Raw: {data}"
        ) from exc

    if not result.ambiguous:
        return result
    try:
        return _escalate(DividendExtraction, messages, filing_id)
    except ExtractionError as exc:
        log_error(
            "extractor",
            f"Filing {filing_id}: reasoning-tier escalation failed ({exc}); using the fast-tier result instead",
        )
        return result
