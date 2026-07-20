from datetime import date

from monitor.features import build_features


def _event(**kwargs):
    base = {
        "filingId": "0" * 16,
        "eventKind": "other",
        "announcementDate": None,
        "boardMeetingDate": None,
        "boardMeetingPurposeApprovesResults": False,
        "boardMeetingPurposeConsidersDividend": False,
        "boardMeetingPurposeRaw": None,
        "resultsPeriod": None,
        "dividendType": None,
        "dividendAmount": None,
        "declaredDate": None,
    }
    base.update(kwargs)
    return base


class TestEmptyHistory:
    def test_no_events_yields_all_false_and_zero_observations(self):
        features = build_features(date(2026, 7, 14), [])
        assert features["board_meeting_today"] is False
        assert features["board_meeting_within_horizon"] is False
        assert features["results_today"] is False
        assert features["has_dividend_history"] is False
        assert features["num_observations"] == 0
        assert features["historical_consistency_score"] == 0.0
        assert features["regular_quarterly_payer"] is False
        assert features["evidence_filing_ids"] == []


class TestBoardMeetingTiming:
    def test_meeting_today_flags_today_and_within_horizon(self):
        today = date(2026, 7, 14)
        events = [_event(filingId="1" * 16, eventKind="board_meeting", boardMeetingDate=today)]
        features = build_features(today, events, horizon_days=3)
        assert features["board_meeting_today"] is True
        assert features["board_meeting_within_horizon"] is True
        assert features["board_meeting_date"] == today

    def test_meeting_in_two_days_is_within_horizon_not_today(self):
        today = date(2026, 7, 14)
        events = [_event(filingId="1" * 16, eventKind="board_meeting", boardMeetingDate=date(2026, 7, 16))]
        features = build_features(today, events, horizon_days=3)
        assert features["board_meeting_today"] is False
        assert features["board_meeting_within_horizon"] is True

    def test_meeting_beyond_horizon_is_neither(self):
        today = date(2026, 7, 14)
        events = [_event(filingId="1" * 16, eventKind="board_meeting", boardMeetingDate=date(2026, 7, 25))]
        features = build_features(today, events, horizon_days=3)
        assert features["board_meeting_today"] is False
        assert features["board_meeting_within_horizon"] is False

    def test_past_board_meeting_is_ignored(self):
        today = date(2026, 7, 14)
        events = [_event(filingId="1" * 16, eventKind="board_meeting", boardMeetingDate=date(2026, 7, 1))]
        features = build_features(today, events, horizon_days=3)
        assert features["board_meeting_date"] is None
        assert features["board_meeting_today"] is False

    def test_accepts_iso_string_dates_from_db_round_trip(self):
        today = date(2026, 7, 14)
        events = [_event(filingId="1" * 16, eventKind="board_meeting", boardMeetingDate="2026-07-14T00:00:00Z")]
        features = build_features(today, events, horizon_days=3)
        assert features["board_meeting_today"] is True


class TestResultsDateProvenance:
    def test_approving_results_derives_results_date_from_board_meeting(self):
        today = date(2026, 7, 14)
        events = [
            _event(
                filingId="1" * 16,
                eventKind="board_meeting",
                boardMeetingDate=today,
                boardMeetingPurposeApprovesResults=True,
            )
        ]
        features = build_features(today, events, horizon_days=3)
        assert features["results_date"] == today
        assert features["results_date_source"] == "derived"
        assert features["results_today"] is True
        assert features["board_meeting_approves_results"] is True

    def test_actual_results_filing_is_official(self):
        today = date(2026, 7, 14)
        events = [_event(filingId="2" * 16, eventKind="results", announcementDate=today)]
        features = build_features(today, events, horizon_days=3)
        assert features["results_date"] == today
        assert features["results_date_source"] == "official"
        assert features["results_today"] is True

    def test_considers_dividend_flag_surfaced(self):
        today = date(2026, 7, 14)
        events = [
            _event(
                filingId="1" * 16,
                eventKind="board_meeting",
                boardMeetingDate=today,
                boardMeetingPurposeConsidersDividend=True,
                boardMeetingPurposeRaw="to consider payment of a final dividend",
            )
        ]
        features = build_features(today, events, horizon_days=3)
        assert features["board_meeting_considers_dividend"] is True
        assert features["board_meeting_purpose_raw"] == "to consider payment of a final dividend"


class TestRegularPayerFlags:
    def test_two_quarterly_dividends_flag_as_regular(self):
        today = date(2026, 7, 14)
        events = [
            _event(filingId=f"{i}" * 16, eventKind="dividend", dividendType="quarterly", declaredDate=date(2025, 1 + i, 1))
            for i in range(2)
        ]
        features = build_features(today, events)
        assert features["regular_quarterly_payer"] is True
        assert features["num_observations"] == 2

    def test_single_special_dividend_is_not_regular(self):
        today = date(2026, 7, 14)
        events = [_event(filingId="1" * 16, eventKind="dividend", dividendType="special", declaredDate=date(2025, 3, 1))]
        features = build_features(today, events)
        assert features["regular_final_payer"] is False
        assert features["regular_interim_payer"] is False
        assert features["regular_quarterly_payer"] is False
        assert features["has_dividend_history"] is True
        assert features["num_observations"] == 1


class TestHistoricallyDeclaresWithResults:
    def test_dividend_near_a_results_announcement_counts_as_coincident(self):
        today = date(2026, 7, 14)
        events = [
            _event(filingId="1" * 16, eventKind="results", announcementDate=date(2025, 8, 20)),
            _event(
                filingId="2" * 16,
                eventKind="dividend",
                dividendType="final",
                announcementDate=date(2025, 8, 21),
                declaredDate=date(2025, 8, 21),
            ),
        ]
        features = build_features(today, events)
        assert features["historically_declares_with_results"] is True

    def test_dividend_far_from_any_results_does_not_count(self):
        today = date(2026, 7, 14)
        events = [
            _event(filingId="1" * 16, eventKind="results", announcementDate=date(2025, 3, 1)),
            _event(
                filingId="2" * 16,
                eventKind="dividend",
                dividendType="final",
                announcementDate=date(2025, 8, 21),
                declaredDate=date(2025, 8, 21),
            ),
        ]
        features = build_features(today, events)
        assert features["historically_declares_with_results"] is False


class TestConsistencyAndWindow:
    def test_same_day_of_year_across_years_gives_high_consistency(self):
        today = date(2026, 8, 15)
        events = [
            _event(filingId=f"{i}" * 16, eventKind="dividend", declaredDate=date(2020 + i, 8, 15))
            for i in range(4)
        ]
        features = build_features(today, events)
        assert features["historical_consistency_score"] > 0.95
        assert features["avg_declaration_month"] == 8
        assert features["in_historical_declaration_window"] is True

    def test_scattered_declaration_dates_give_low_consistency(self):
        today = date(2026, 8, 15)
        months = [1, 4, 7, 10]
        events = [
            _event(filingId=f"{i}" * 16, eventKind="dividend", declaredDate=date(2020 + i, m, 15))
            for i, m in enumerate(months)
        ]
        features = build_features(today, events)
        assert features["historical_consistency_score"] < 0.5

    def test_single_observation_has_zero_consistency_but_a_month(self):
        today = date(2026, 8, 15)
        events = [_event(filingId="1" * 16, eventKind="dividend", declaredDate=date(2025, 8, 15))]
        features = build_features(today, events)
        assert features["num_observations"] == 1
        assert features["historical_consistency_score"] == 0.0
        assert features["avg_declaration_month"] == 8
        # A single observation is insufficient evidence for a "window".
        assert features["in_historical_declaration_window"] is False

    def test_declaration_interval_averaged_across_history(self):
        today = date(2026, 8, 15)
        events = [
            _event(filingId="1" * 16, eventKind="dividend", declaredDate=date(2024, 8, 15)),
            _event(filingId="2" * 16, eventKind="dividend", declaredDate=date(2025, 2, 15)),
            _event(filingId="3" * 16, eventKind="dividend", declaredDate=date(2025, 8, 15)),
        ]
        features = build_features(today, events)
        assert features["avg_declaration_interval_days"] == 182  # ~6 months each step

    def test_window_check_requires_today_outside_tolerance_to_fail(self):
        today = date(2026, 2, 15)  # far from the historical August cluster
        events = [
            _event(filingId=f"{i}" * 16, eventKind="dividend", declaredDate=date(2020 + i, 8, 15))
            for i in range(3)
        ]
        features = build_features(today, events)
        assert features["in_historical_declaration_window"] is False


class TestEvidenceFilingIds:
    def test_collects_board_meeting_and_latest_dividend_and_results(self):
        today = date(2026, 7, 14)
        events = [
            _event(filingId="aaaa000000000001", eventKind="board_meeting", boardMeetingDate=today),
            _event(filingId="bbbb000000000002", eventKind="dividend", dividendType="final", declaredDate=date(2025, 8, 1)),
            _event(filingId="cccc000000000003", eventKind="results", announcementDate=date(2025, 8, 1)),
        ]
        features = build_features(today, events)
        ids = features["evidence_filing_ids"]
        assert "aaaa000000000001" in ids
        assert "bbbb000000000002" in ids
        assert "cccc000000000003" in ids

    def test_last_dividend_summary_fields_reflect_most_recent(self):
        today = date(2026, 7, 14)
        events = [
            _event(
                filingId="1" * 16,
                eventKind="dividend",
                dividendType="interim",
                dividendAmount="HKD 0.10",
                declaredDate=date(2024, 8, 1),
                announcementDate=date(2024, 8, 1),
            ),
            _event(
                filingId="2" * 16,
                eventKind="dividend",
                dividendType="final",
                dividendAmount="HKD 0.20",
                declaredDate=date(2025, 8, 1),
                announcementDate=date(2025, 8, 1),
            ),
        ]
        features = build_features(today, events)
        assert features["last_dividend_type"] == "final"
        assert features["last_dividend_amount"] == "HKD 0.20"
        assert features["last_declaration_date"] == date(2025, 8, 1)
