from monitor.scoring import DERIVED, ESTIMATED, OFFICIAL, score_candidate

_NO_SIGNAL_FEATURES = {
    "board_meeting_today": False,
    "board_meeting_within_horizon": False,
    "board_meeting_date": None,
    "results_today": False,
    "results_within_horizon": False,
    "results_date": None,
    "results_date_source": None,
    "board_meeting_considers_dividend": False,
    "board_meeting_approves_results": False,
    "board_meeting_purpose_raw": None,
    "avg_declaration_month": None,
    "avg_declaration_interval_days": None,
    "num_observations": 0,
    "regular_quarterly_payer": False,
    "regular_interim_payer": False,
    "regular_final_payer": False,
    "historical_consistency_score": 0.0,
    "has_dividend_history": False,
    "historically_declares_with_results": False,
    "in_historical_declaration_window": False,
    "last_dividend_type": None,
    "last_dividend_amount": None,
    "last_declaration_date": None,
    "evidence_filing_ids": [],
}


def _features(**overrides):
    merged = dict(_NO_SIGNAL_FEATURES)
    merged.update(overrides)
    return merged


class TestNoSignal:
    def test_zero_score_is_low_band_with_no_reasons(self):
        result = score_candidate(_features())
        assert result.score == 0
        assert result.band == "Low"
        assert result.reasons == []


class TestBandThresholds:
    def test_board_meeting_today_alone_is_not_yet_high(self):
        result = score_candidate(_features(board_meeting_today=True))
        assert result.score == 35
        assert result.band == "Low"  # below MEDIUM_THRESHOLD (40)

    def test_strong_stack_of_official_signals_reaches_high_band(self):
        result = score_candidate(
            _features(
                board_meeting_today=True,
                board_meeting_approves_results=True,
                board_meeting_considers_dividend=True,
                results_today=True,
                results_date_source="derived",
            )
        )
        assert result.score >= 70
        assert result.band == "High"

    def test_moderate_signal_lands_in_medium_band(self):
        result = score_candidate(
            _features(
                board_meeting_within_horizon=True,
                board_meeting_approves_results=True,
                historically_declares_with_results=True,
            )
        )
        assert 40 <= result.score < 70
        assert result.band == "Medium"

    def test_score_never_exceeds_100(self):
        result = score_candidate(
            _features(
                board_meeting_today=True,
                board_meeting_approves_results=True,
                board_meeting_considers_dividend=True,
                results_today=True,
                results_date_source="official",
                historically_declares_with_results=True,
                regular_final_payer=True,
                regular_interim_payer=True,
                regular_quarterly_payer=True,
                historical_consistency_score=1.0,
                has_dividend_history=True,
                in_historical_declaration_window=True,
                num_observations=20,
            )
        )
        assert result.score <= 100


class TestProvenanceLabels:
    def test_every_reason_has_a_valid_provenance_label(self):
        result = score_candidate(
            _features(
                board_meeting_today=True,
                board_meeting_approves_results=True,
                historically_declares_with_results=True,
                regular_final_payer=True,
                historical_consistency_score=0.8,
                has_dividend_history=True,
                in_historical_declaration_window=True,
            )
        )
        assert result.reasons  # sanity: signals actually produced reasons
        for reason in result.reasons:
            assert reason.label in {OFFICIAL, DERIVED, ESTIMATED}

    def test_board_meeting_date_is_official(self):
        result = score_candidate(_features(board_meeting_today=True))
        assert result.reasons[0].label == OFFICIAL

    def test_results_date_label_follows_source_official_vs_derived(self):
        derived = score_candidate(_features(results_today=True, results_date_source="derived"))
        official = score_candidate(_features(results_today=True, results_date_source="official"))
        assert derived.reasons[0].label == DERIVED
        assert official.reasons[0].label == OFFICIAL

    def test_declaration_window_reason_is_estimated_not_official(self):
        result = score_candidate(_features(in_historical_declaration_window=True, avg_declaration_month=8))
        assert len(result.reasons) == 1
        assert result.reasons[0].label == ESTIMATED

    def test_regular_payer_reasons_are_derived(self):
        result = score_candidate(_features(regular_final_payer=True, num_observations=3))
        assert result.reasons[0].label == DERIVED


class TestReasonsSortedByWeightDescending(object):
    def test_highest_weight_reason_first(self):
        result = score_candidate(
            _features(
                board_meeting_today=True,  # weight 35, should sort first
                has_dividend_history=True,  # weight 3
            )
        )
        weights = [r.weight for r in result.reasons]
        assert weights == sorted(weights, reverse=True)


class TestMonotonicity:
    def test_adding_a_positive_signal_never_decreases_score(self):
        base = score_candidate(_features(board_meeting_within_horizon=True))
        richer = score_candidate(_features(board_meeting_within_horizon=True, historical_consistency_score=0.9))
        assert richer.score >= base.score
