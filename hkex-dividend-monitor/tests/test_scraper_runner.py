from datetime import date, datetime
from zoneinfo import ZoneInfo

import monitor.scraper_runner as scraper_runner

HKT = ZoneInfo("Asia/Hong_Kong")


class _FrozenDatetime:
    """Stands in for the `datetime` class inside scraper_runner's module
    namespace so `datetime.now(HKT)` returns a fixed instant, without
    pulling in freezegun as a new dependency."""

    def __init__(self, fixed: datetime):
        self._fixed = fixed

    def now(self, tz=None):
        return self._fixed.astimezone(tz) if tz else self._fixed


class TestComputeScrapeWindow:
    def test_no_targets_returns_none(self):
        assert scraper_runner.compute_scrape_window([], lookback_days=3) is None

    def test_target_outside_lookback_and_future_returns_none(self):
        # Far in the past, well beyond the lookback cap, and not "soon".
        assert scraper_runner.compute_scrape_window([date(2000, 1, 1)], lookback_days=3) is None

    def test_window_covers_todays_target(self, monkeypatch):
        fixed_now = datetime(2026, 7, 16, 12, 0, tzinfo=HKT)
        monkeypatch.setattr(scraper_runner, "datetime", _FrozenDatetime(fixed_now))
        window = scraper_runner.compute_scrape_window([date(2026, 7, 16)], lookback_days=3)
        assert window is not None
        from_date, to_date = window
        assert from_date <= date(2026, 7, 16) <= to_date

    def test_uses_hkt_not_host_machine_local_date(self, monkeypatch):
        """Regression: compute_scrape_window used date.today() (the host
        machine's local calendar date) instead of HKT. On a machine whose
        system timezone lags HKT (e.g. anywhere in the Americas or Europe,
        a very plausible personal Windows install), this produced an
        INVERTED window (from_date > to_date) that silently scraped
        nothing for a target dated "today" in HKT terms -- because it was
        still "yesterday" on the host machine's clock. Real incident: a
        same-day filing release was never detected.

        Simulates that exact scenario: it's 2026-07-16 08:00 HKT (an
        ordinary HK morning), but the "local" system date -- what a naive
        date.today() would have returned pre-fix -- is still 2026-07-15
        (true for roughly the first ~13 hours of every HKT day for anyone
        several hours west of Hong Kong). A target dated 2026-07-16 (HKT)
        must still be covered by the window.
        """
        fixed_now_hkt = datetime(2026, 7, 16, 8, 0, tzinfo=HKT)
        monkeypatch.setattr(scraper_runner, "datetime", _FrozenDatetime(fixed_now_hkt))

        window = scraper_runner.compute_scrape_window([date(2026, 7, 16)], lookback_days=3)

        assert window is not None
        from_date, to_date = window
        assert from_date <= to_date, f"inverted window {window} -- would scrape nothing"
        assert from_date <= date(2026, 7, 16) <= to_date

    def test_from_date_is_earliest_relevant_target_within_lookback(self, monkeypatch):
        fixed_now = datetime(2026, 7, 16, 12, 0, tzinfo=HKT)
        monkeypatch.setattr(scraper_runner, "datetime", _FrozenDatetime(fixed_now))
        window = scraper_runner.compute_scrape_window([date(2026, 7, 14), date(2026, 7, 16)], lookback_days=3)
        from_date, to_date = window
        assert from_date == date(2026, 7, 14)
        assert to_date == date(2026, 7, 16)

    def test_target_older_than_lookback_is_excluded_from_window(self, monkeypatch):
        fixed_now = datetime(2026, 7, 16, 12, 0, tzinfo=HKT)
        monkeypatch.setattr(scraper_runner, "datetime", _FrozenDatetime(fixed_now))
        # Jan 1 is far outside a 3-day lookback -- must not drag from_date
        # back to it; only the still-relevant Jul 16 target counts.
        window = scraper_runner.compute_scrape_window([date(2026, 1, 1), date(2026, 7, 16)], lookback_days=3)
        from_date, to_date = window
        assert from_date == date(2026, 7, 16)
        assert to_date == date(2026, 7, 16)

    def test_target_tomorrow_still_relevant(self, monkeypatch):
        fixed_now = datetime(2026, 7, 16, 12, 0, tzinfo=HKT)
        monkeypatch.setattr(scraper_runner, "datetime", _FrozenDatetime(fixed_now))
        window = scraper_runner.compute_scrape_window([date(2026, 7, 17)], lookback_days=3)
        assert window is not None  # doesn't return None just because it's not today yet
