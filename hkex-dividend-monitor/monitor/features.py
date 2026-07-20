"""Deterministic feature engineering for Today's HKEX Dividend Watchlist.

Pure functions only -- no I/O, no LLM calls. Takes the structured
announcement history monitor.history/monitor.announcement_extractor have
already built for one ticker and derives the signals monitor.scoring uses.
Keeping this pure and separate from scoring is what makes every point of
the final score traceable back to a specific, inspectable fact rather than
an opaque model.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Optional

# How close (in days) a dividend declaration's announcementDate must be to
# a same-ticker "results" event's announcementDate to count as "declared
# alongside results" -- HKEX dividend declarations are near-universally
# released the same day as (or the day after) the results they accompany.
_RESULTS_COINCIDENCE_WINDOW_DAYS = 2

# Minimum repeat count of one dividend_type before it counts as a "regular"
# pattern rather than a one-off.
_REGULAR_PAYER_MIN_COUNT = 2

# +/- day tolerance (projected onto the current year) for "today falls
# within this company's historical declaration window".
_DECLARATION_WINDOW_TOLERANCE_DAYS = 10


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _days_until(target: Optional[date], today: date) -> Optional[int]:
    if target is None:
        return None
    return (target - today).days


def _circular_stats(dates: list[date]) -> tuple[Optional[float], float]:
    """Mean day-of-year angle and consistency R (0-1) across `dates`,
    treating each date's day-of-year as a point on a circle -- this avoids
    a naive arithmetic mean of month/day distorting results for a company
    that declares every December/January (which straddles year-end).
    R=1.0 means every date fell on (near) the same day of year; R=0.0 means
    they're spread uniformly around the whole year. Returns (None, 0.0) for
    an empty input.
    """
    if not dates:
        return None, 0.0
    angles = [2 * math.pi * (d.timetuple().tm_yday - 1) / 365.0 for d in dates]
    mean_cos = sum(math.cos(a) for a in angles) / len(angles)
    mean_sin = sum(math.sin(a) for a in angles) / len(angles)
    r = math.hypot(mean_cos, mean_sin)
    mean_angle = math.atan2(mean_sin, mean_cos)
    if mean_angle < 0:
        mean_angle += 2 * math.pi
    return mean_angle, r


def _angle_to_day_of_year(angle: float) -> int:
    day = round(angle / (2 * math.pi) * 365.0) + 1
    return max(1, min(365, day))


def build_features(today: date, events: list[dict[str, Any]], horizon_days: int = 3) -> dict[str, Any]:
    """Derive deterministic signals for one ticker from its company_event
    history. `events` is every row monitor.history.events_for_ticker
    returned for that ticker (board-meeting notices, results
    announcements, and declared dividends) -- order doesn't matter, this
    sorts internally.
    """
    events = sorted(events, key=lambda e: _parse_date(e.get("announcementDate")) or date.min)

    board_meeting_events = [
        e for e in events if e.get("eventKind") == "board_meeting" and _parse_date(e.get("boardMeetingDate"))
    ]
    upcoming_board_meetings = sorted(
        (e for e in board_meeting_events if _parse_date(e["boardMeetingDate"]) >= today),
        key=lambda e: _parse_date(e["boardMeetingDate"]),
    )
    board_meeting_event = upcoming_board_meetings[0] if upcoming_board_meetings else None
    board_meeting_date = _parse_date(board_meeting_event["boardMeetingDate"]) if board_meeting_event else None

    results_events = [e for e in events if e.get("eventKind") == "results"]
    upcoming_results = sorted(
        (
            e
            for e in results_events
            if _parse_date(e.get("announcementDate")) and _parse_date(e["announcementDate"]) >= today
        ),
        key=lambda e: _parse_date(e["announcementDate"]),
    )

    # Results are near-universally released the same day the board meets to
    # approve them, so a board meeting notice whose purpose includes
    # approving results is itself the strongest signal of *when* results
    # will land -- prefer it over an (unlikely) separately-dated upcoming
    # results row. results_date_source distinguishes the two for scoring's
    # Official/Derived labelling: a date read off an actual results filing
    # is Official; a date inferred from board-meeting purpose is Derived.
    results_date = None
    results_date_source = None
    if board_meeting_event and board_meeting_event.get("boardMeetingPurposeApprovesResults"):
        results_date = board_meeting_date
        results_date_source = "derived"
    elif upcoming_results:
        results_date = _parse_date(upcoming_results[0]["announcementDate"])
        results_date_source = "official"

    board_meeting_days_until = _days_until(board_meeting_date, today)
    results_days_until = _days_until(results_date, today)

    dividend_events = [e for e in events if e.get("eventKind") == "dividend"]
    declaration_dates = [
        d for d in (_parse_date(e.get("declaredDate") or e.get("announcementDate")) for e in dividend_events) if d
    ]
    num_observations = len(declaration_dates)

    if num_observations >= 2:
        mean_angle, consistency = _circular_stats(declaration_dates)
    elif num_observations == 1:
        mean_angle = 2 * math.pi * (declaration_dates[0].timetuple().tm_yday - 1) / 365.0
        consistency = 0.0
    else:
        mean_angle = None
        consistency = 0.0

    avg_declaration_month = None
    in_historical_declaration_window = False
    if mean_angle is not None:
        mean_day_of_year = _angle_to_day_of_year(mean_angle)
        avg_declaration_month = date.fromordinal(date(2001, 1, 1).toordinal() + mean_day_of_year - 1).month
        if num_observations >= 2:
            projected = date.fromordinal(date(today.year, 1, 1).toordinal() + mean_day_of_year - 1)
            in_historical_declaration_window = abs((today - projected).days) <= _DECLARATION_WINDOW_TOLERANCE_DAYS

    sorted_decl = sorted(declaration_dates)
    intervals = [(b - a).days for a, b in zip(sorted_decl, sorted_decl[1:])]
    avg_declaration_interval_days = round(sum(intervals) / len(intervals)) if intervals else None

    type_counts: dict[str, int] = {}
    for e in dividend_events:
        t = e.get("dividendType")
        if t:
            type_counts[t] = type_counts.get(t, 0) + 1

    coincide_count = 0
    for e in dividend_events:
        dt = _parse_date(e.get("announcementDate"))
        if dt is None:
            continue
        if any(
            abs((dt - _parse_date(r.get("announcementDate"))).days) <= _RESULTS_COINCIDENCE_WINDOW_DAYS
            for r in results_events
            if _parse_date(r.get("announcementDate"))
        ):
            coincide_count += 1
    historically_declares_with_results = num_observations > 0 and (coincide_count / num_observations) >= 0.5

    last_dividend_event = dividend_events[-1] if dividend_events else None
    last_results_event = (
        max(results_events, key=lambda e: _parse_date(e.get("announcementDate")) or date.min)
        if results_events
        else None
    )

    # filingId references for the specific events behind each headline date,
    # so callers (monitor.watchlist) can cite exact source filings as
    # evidence without re-deriving "which event produced this fact".
    evidence_filing_ids = [
        fid
        for fid in (
            board_meeting_event.get("filingId") if board_meeting_event else None,
            last_dividend_event.get("filingId") if last_dividend_event else None,
            last_results_event.get("filingId") if last_results_event else None,
        )
        if fid
    ]

    return {
        "board_meeting_today": board_meeting_days_until == 0,
        "board_meeting_within_horizon": (
            board_meeting_days_until is not None and 0 <= board_meeting_days_until <= horizon_days
        ),
        "board_meeting_date": board_meeting_date,
        "results_today": results_days_until == 0,
        "results_within_horizon": results_days_until is not None and 0 <= results_days_until <= horizon_days,
        "results_date": results_date,
        "results_date_source": results_date_source,
        "board_meeting_considers_dividend": bool(
            board_meeting_event and board_meeting_event.get("boardMeetingPurposeConsidersDividend")
        ),
        "board_meeting_approves_results": bool(
            board_meeting_event and board_meeting_event.get("boardMeetingPurposeApprovesResults")
        ),
        "board_meeting_purpose_raw": board_meeting_event.get("boardMeetingPurposeRaw") if board_meeting_event else None,
        "avg_declaration_month": avg_declaration_month,
        "avg_declaration_interval_days": avg_declaration_interval_days,
        "num_observations": num_observations,
        "regular_quarterly_payer": type_counts.get("quarterly", 0) >= _REGULAR_PAYER_MIN_COUNT,
        "regular_interim_payer": type_counts.get("interim", 0) >= _REGULAR_PAYER_MIN_COUNT,
        "regular_final_payer": type_counts.get("final", 0) >= _REGULAR_PAYER_MIN_COUNT,
        "historical_consistency_score": round(consistency, 3),
        "has_dividend_history": num_observations > 0,
        "historically_declares_with_results": historically_declares_with_results,
        "in_historical_declaration_window": in_historical_declaration_window,
        "last_dividend_type": last_dividend_event.get("dividendType") if last_dividend_event else None,
        "last_dividend_amount": last_dividend_event.get("dividendAmount") if last_dividend_event else None,
        "last_declaration_date": sorted_decl[-1] if sorted_decl else None,
        "evidence_filing_ids": evidence_filing_ids,
    }
