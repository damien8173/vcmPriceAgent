from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import monitor.daemon as daemon
import monitor.notifier as notifier

HKT = ZoneInfo("Asia/Hong_Kong")


class TestWithinDisclosureHours:
    def test_weekday_during_hours_is_true(self):
        # 2026-08-17 is a Monday.
        now = datetime(2026, 8, 17, 12, 0, tzinfo=HKT)
        assert daemon.within_disclosure_hours(now) is True

    def test_weekday_before_hours_is_false(self):
        now = datetime(2026, 8, 17, 5, 59, tzinfo=HKT)
        assert daemon.within_disclosure_hours(now) is False

    def test_weekday_after_hours_is_false(self):
        now = datetime(2026, 8, 17, 23, 0, tzinfo=HKT)
        assert daemon.within_disclosure_hours(now) is False

    def test_saturday_is_false(self):
        # 2026-08-22 is a Saturday.
        now = datetime(2026, 8, 22, 12, 0, tzinfo=HKT)
        assert daemon.within_disclosure_hours(now) is False

    def test_sunday_is_false(self):
        # 2026-08-23 is a Sunday.
        now = datetime(2026, 8, 23, 12, 0, tzinfo=HKT)
        assert daemon.within_disclosure_hours(now) is False


class TestRacingTargets:
    def test_only_targets_dated_today_race(self):
        now = datetime(2026, 8, 17, 12, 0, tzinfo=HKT)
        targets = [
            {"ticker": "00700", "target_date": "2026-08-17", "status": "active"},
            {"ticker": "00005", "target_date": "2026-08-18", "status": "active"},
        ]
        racing = daemon.racing_targets(targets, now)
        assert [t["ticker"] for t in racing] == ["00700"]

    def test_no_targets_today_returns_empty(self):
        now = datetime(2026, 8, 17, 12, 0, tzinfo=HKT)
        targets = [{"ticker": "00700", "target_date": "2026-08-18", "status": "active"}]
        assert daemon.racing_targets(targets, now) == []


class TestRaceWindowActive:
    def test_within_configured_window(self):
        cfg = SimpleNamespace(race_start_hour=9, race_end_hour=17)
        now = datetime(2026, 8, 17, 12, 0, tzinfo=HKT)
        assert daemon._race_window_active(cfg, now) is True

    def test_outside_configured_window(self):
        cfg = SimpleNamespace(race_start_hour=9, race_end_hour=17)
        now = datetime(2026, 8, 17, 20, 0, tzinfo=HKT)
        assert daemon._race_window_active(cfg, now) is False

    def test_nonsense_values_fall_back_to_full_day(self):
        """A user hand-editing .env could set start >= end -- rather than
        silently disabling race mode, it should fall back to the full day."""
        cfg = SimpleNamespace(race_start_hour=20, race_end_hour=5)
        now = datetime(2026, 8, 17, 3, 0, tzinfo=HKT)
        assert daemon._race_window_active(cfg, now) is True


class TestBuildTickerDateMaps:
    def test_single_target_per_ticker(self):
        targets = [{"ticker": "00700", "target_date": "2026-08-15", "status": "active"}]
        dates_map, earliest_map = daemon._build_ticker_date_maps(targets)
        assert dates_map == {"00700": {date(2026, 8, 15)}}
        assert earliest_map == {"00700": date(2026, 8, 15)}

    def test_multiple_dates_for_same_ticker_all_kept(self):
        """Regression test: a ticker watched for two different dates (e.g.
        interim + final dividend) must keep BOTH dates, not silently
        collapse to whichever the dict comprehension saw last."""
        targets = [
            {"ticker": "00700", "target_date": "2026-08-15", "status": "active"},
            {"ticker": "00700", "target_date": "2026-11-01", "status": "active"},
        ]
        dates_map, earliest_map = daemon._build_ticker_date_maps(targets)
        assert dates_map["00700"] == {date(2026, 8, 15), date(2026, 11, 1)}
        assert earliest_map["00700"] == date(2026, 8, 15)  # earliest of the two

    def test_earliest_is_per_ticker_not_global(self):
        """Regression test: one ticker's very old target date must not widen
        another ticker's query window."""
        targets = [
            {"ticker": "00700", "target_date": "2026-08-15", "status": "active"},
            {"ticker": "00005", "target_date": "2020-01-01", "status": "active"},
        ]
        _, earliest_map = daemon._build_ticker_date_maps(targets)
        assert earliest_map["00700"] == date(2026, 8, 15)
        assert earliest_map["00005"] == date(2020, 1, 1)


class TestRaceOutageAlerting:
    """Covers _alert_race_unreachable/_alert_race_recovered: threshold before
    alerting, cooldown between repeats, and recovery notification."""

    @pytest.fixture
    def cfg(self):
        return SimpleNamespace(race_alert_failure_threshold=3, race_alert_cooldown_seconds=1800)

    @pytest.fixture
    def fresh_state(self):
        return {"failures": 0, "next_attempt": None, "alerted": False, "last_alert_at": None}

    @pytest.fixture
    def capture(self, monkeypatch):
        sent = []
        monkeypatch.setattr(daemon, "dispatch_text", lambda message: sent.append(message) or {"slack": True})
        monkeypatch.setattr(daemon.alert_history, "append", lambda entry: sent.append(("history", entry["kind"])))
        return sent

    def test_no_alert_below_threshold(self, fresh_state, cfg, capture):
        now = datetime.now(timezone.utc)
        for i in (1, 2):
            fresh_state["failures"] = i
            daemon._alert_race_unreachable("00700", fresh_state, now, RuntimeError("boom"), cfg)
        assert capture == []
        assert fresh_state["alerted"] is False

    def test_alert_fires_exactly_at_threshold(self, fresh_state, cfg, capture):
        now = datetime.now(timezone.utc)
        fresh_state["failures"] = 3
        daemon._alert_race_unreachable("00700", fresh_state, now, RuntimeError("boom"), cfg)
        assert fresh_state["alerted"] is True
        assert fresh_state["last_alert_at"] == now
        rendered = [m for m in capture if isinstance(m, str)]
        assert len(rendered) == 1
        assert "UNREACHABLE" in rendered[0]
        assert ("history", "race_error") in capture

    def test_cooldown_suppresses_repeat_alert(self, fresh_state, cfg, capture):
        now = datetime.now(timezone.utc)
        fresh_state["failures"] = 3
        daemon._alert_race_unreachable("00700", fresh_state, now, RuntimeError("boom"), cfg)
        capture.clear()

        # Still within the cooldown window -- must not alert again.
        later = now + timedelta(seconds=10)
        fresh_state["failures"] = 4
        daemon._alert_race_unreachable("00700", fresh_state, later, RuntimeError("boom"), cfg)
        assert capture == []

    def test_alert_resumes_after_cooldown_elapses(self, fresh_state, cfg, capture):
        now = datetime.now(timezone.utc)
        fresh_state["failures"] = 3
        daemon._alert_race_unreachable("00700", fresh_state, now, RuntimeError("boom"), cfg)
        capture.clear()

        after_cooldown = now + timedelta(seconds=cfg.race_alert_cooldown_seconds + 1)
        fresh_state["failures"] = 4
        daemon._alert_race_unreachable("00700", fresh_state, after_cooldown, RuntimeError("boom"), cfg)
        assert any(isinstance(m, str) and "UNREACHABLE" in m for m in capture)

    def test_recovery_alert_fires_and_resets_state(self, fresh_state, cfg, capture):
        now = datetime.now(timezone.utc)
        fresh_state["failures"] = 3
        daemon._alert_race_unreachable("00700", fresh_state, now, RuntimeError("boom"), cfg)
        capture.clear()

        daemon._alert_race_recovered("00700", fresh_state)
        assert fresh_state["alerted"] is False
        assert fresh_state["last_alert_at"] is None
        assert any(isinstance(m, str) and "RECOVERED" in m for m in capture)
        assert ("history", "race_recovered") in capture

    def test_recovery_is_noop_when_not_previously_alerted(self, fresh_state, capture):
        daemon._alert_race_recovered("00700", fresh_state)
        assert capture == []

    def test_no_alert_recorded_if_all_channels_fail(self, fresh_state, cfg, monkeypatch):
        sent = []
        monkeypatch.setattr(daemon, "dispatch_text", lambda message: {"slack": False})
        monkeypatch.setattr(daemon.alert_history, "append", lambda entry: sent.append(entry))
        now = datetime.now(timezone.utc)
        fresh_state["failures"] = 3
        daemon._alert_race_unreachable("00700", fresh_state, now, RuntimeError("boom"), cfg)
        assert fresh_state["alerted"] is False  # never confirmed as told
        assert sent == []


class TestTargetMatchStatus:
    """Covers the Watchlist tab's computed lifecycle badge: upcoming/racing/
    today/seen/pending -- the fix for a past-date target that never matched
    anything being silently indistinguishable from one working fine."""

    TODAY = date(2026, 8, 15)

    def test_future_date_is_upcoming(self):
        status = daemon.target_match_status("00700", date(2026, 9, 1), self.TODAY, set(), [])
        assert status == "upcoming"

    def test_today_and_racing(self):
        status = daemon.target_match_status("00700", self.TODAY, self.TODAY, {"00700"}, [])
        assert status == "racing"

    def test_today_but_not_racing(self):
        status = daemon.target_match_status("00700", self.TODAY, self.TODAY, set(), [])
        assert status == "today"

    def test_past_date_with_matching_dividend_record_is_seen(self):
        records = [{"ticker": "00700", "filingDate": "2026-08-01T09:00:00Z"}]
        status = daemon.target_match_status("00700", date(2026, 8, 1), self.TODAY, set(), records)
        assert status == "seen"

    def test_past_date_with_no_record_is_pending(self):
        status = daemon.target_match_status("00700", date(2026, 8, 1), self.TODAY, set(), [])
        assert status == "pending"

    def test_race_mode_date_format_also_recognized(self):
        """dividend records populated via race mode store filingDate as
        HKEX's own "DD/MM/YYYY HH:MM" string, not an ISO datetime -- must
        still match correctly."""
        records = [{"ticker": "00005", "filingDate": "01/08/2026 16:45"}]
        status = daemon.target_match_status("00005", date(2026, 8, 1), self.TODAY, set(), records)
        assert status == "seen"

    def test_another_tickers_record_does_not_count(self):
        records = [{"ticker": "00005", "filingDate": "2026-08-01T09:00:00Z"}]
        status = daemon.target_match_status("00700", date(2026, 8, 1), self.TODAY, set(), records)
        assert status == "pending"


class TestRunSgxArchiveStep:
    """Independent of the dividend-watch pipeline -- must never raise, so a
    settlement-site or DB hiccup here can never take down the daemon loop
    (see run_sgx_archive_step's docstring)."""

    def test_fetches_both_files_and_archives_them(self, monkeypatch):
        main = {"rows": [{"ticker": "NK", "fspDate": "2026-07-10", "fsp": 69171.55}]}
        flexc = {"rows": [{"ticker": "UC010726", "fspDate": "2026-07-01", "fsp": 6.7985}]}
        captured = {}
        monkeypatch.setattr(daemon, "fetch_sgx_fsp", lambda: main)
        monkeypatch.setattr(daemon, "fetch_sgx_flexc", lambda: flexc)
        monkeypatch.setattr(
            daemon, "archive_sgx_snapshot",
            lambda main_rows, flexc_rows: captured.update(main_rows=main_rows, flexc_rows=flexc_rows) or 2,
        )
        daemon.run_sgx_archive_step()
        assert captured["main_rows"] == main["rows"]
        assert captured["flexc_rows"] == flexc["rows"]

    def test_settlement_error_is_caught_not_raised(self, monkeypatch):
        def _raise():
            raise daemon.SettlementError("SGX unreachable")

        monkeypatch.setattr(daemon, "fetch_sgx_fsp", _raise)
        daemon.run_sgx_archive_step()  # must not raise

    def test_surreal_db_error_is_caught_not_raised(self, monkeypatch):
        monkeypatch.setattr(daemon, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(daemon, "fetch_sgx_flexc", lambda: {"rows": []})

        def _raise(main_rows, flexc_rows):
            raise daemon.SurrealDBError("DB unreachable")

        monkeypatch.setattr(daemon, "archive_sgx_snapshot", _raise)
        daemon.run_sgx_archive_step()  # must not raise
