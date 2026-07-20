"""Transparent, rule-based scoring for Today's HKEX Dividend Watchlist.

Pure functions only -- no I/O, no LLM calls, no randomness. Every point
contributing to a candidate's score is emitted as a `reason` entry so the
dashboard can show exactly why a company was ranked where it was. The LLM
is never involved in this module: monitor.announcement_extractor only
supplies the *facts* (via monitor.features), and this module turns facts
into points using fixed, inspectable weights.

Every reason carries a `label` marking how trustworthy the underlying fact
is:
  Official  -- a date/fact stated verbatim in an HKEX filing.
  Derived   -- a conclusion computed from official facts (e.g. "results
               tend to be released the same day as the board meeting that
               approves them", or a count of past regular payments).
  Estimated -- a projection/extrapolation (e.g. "today falls within this
               company's typical declaration window") that could be wrong.
The dashboard must never render an Estimated reason as if it were Official
-- see monitor/static/index.html's provenance chip styling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

OFFICIAL = "Official"
DERIVED = "Derived"
ESTIMATED = "Estimated"

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40


@dataclass
class Reason:
    signal: str
    weight: float
    evidence: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"signal": self.signal, "weight": self.weight, "evidence": self.evidence, "label": self.label}


@dataclass
class ScoreResult:
    score: int
    band: str
    reasons: list[Reason] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "band": self.band, "reasons": [r.to_dict() for r in self.reasons]}


def _band(score: int) -> str:
    if score >= HIGH_THRESHOLD:
        return "High"
    if score >= MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


def score_candidate(features: dict[str, Any], horizon_days: int = 3) -> ScoreResult:
    """Score one candidate from monitor.features.build_features's output.
    Additive, capped at [0, 100]; see module docstring for the
    Official/Derived/Estimated provenance convention every reason carries.
    """
    reasons: list[Reason] = []
    total = 0.0

    def add(weight: float, signal: str, evidence: str, label: str) -> None:
        nonlocal total
        total += weight
        reasons.append(Reason(signal=signal, weight=weight, evidence=evidence, label=label))

    bm_date = features.get("board_meeting_date")
    results_date = features.get("results_date")
    results_label = OFFICIAL if features.get("results_date_source") == "official" else DERIVED

    if features.get("board_meeting_today"):
        add(35, "Official board meeting today", f"Board meeting scheduled for {bm_date}", OFFICIAL)
    elif features.get("board_meeting_within_horizon"):
        add(
            20,
            f"Official board meeting within {horizon_days} days",
            f"Board meeting scheduled for {bm_date}",
            OFFICIAL,
        )

    if features.get("results_today"):
        add(20, "Results due today", f"Results due {results_date}", results_label)
    elif features.get("results_within_horizon"):
        add(12, f"Results due within {horizon_days} days", f"Results due {results_date}", results_label)

    if features.get("board_meeting_approves_results"):
        add(
            15,
            "Board meeting purpose includes approving results",
            features.get("board_meeting_purpose_raw") or "Stated in board meeting notice",
            OFFICIAL,
        )

    if features.get("board_meeting_considers_dividend"):
        add(
            15,
            "Board meeting purpose includes considering a dividend",
            features.get("board_meeting_purpose_raw") or "Stated in board meeting notice",
            OFFICIAL,
        )

    if features.get("historically_declares_with_results"):
        add(
            10,
            "Historically declares dividend alongside these results",
            f"Based on {features.get('num_observations')} past declarations",
            DERIVED,
        )

    for key, label_text in (
        ("regular_final_payer", "Regular final dividend payer"),
        ("regular_interim_payer", "Regular interim dividend payer"),
        ("regular_quarterly_payer", "Regular quarterly dividend payer"),
    ):
        if features.get(key):
            add(8, label_text, f"Seen in {features.get('num_observations')} historical observations", DERIVED)

    consistency = features.get("historical_consistency_score") or 0.0
    if consistency > 0:
        add(
            round(10 * consistency, 1),
            "Strong historical timing consistency",
            f"Consistency score {consistency:.2f} (1.0 = same time every year)",
            DERIVED,
        )

    if features.get("has_dividend_history"):
        add(
            3,
            "Existing dividend history on record",
            f"{features.get('num_observations')} past declarations",
            DERIVED,
        )

    if features.get("in_historical_declaration_window"):
        month = features.get("avg_declaration_month")
        add(
            8,
            "Falls within historical declaration window",
            f"Typically declares around month {month}" if month else "Within typical declaration window",
            ESTIMATED,
        )

    score = max(0, min(100, round(total)))
    reasons.sort(key=lambda r: r.weight, reverse=True)
    return ScoreResult(score=score, band=_band(score), reasons=reasons)
