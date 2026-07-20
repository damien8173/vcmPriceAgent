import pytest

from monitor.registry import (
    AlertHistory,
    ChannelHealth,
    DividendStore,
    NotifiedCache,
    TargetRegistry,
    WatchlistTickers,
    normalize_ticker,
    validate_date,
)


def test_normalize_ticker_zero_pads():
    assert normalize_ticker("700") == "00700"
    assert normalize_ticker("00700") == "00700"
    assert normalize_ticker(" 700 ") == "00700"
    assert normalize_ticker("hk700") == "00700"


def test_normalize_ticker_rejects_no_digits():
    with pytest.raises(ValueError):
        normalize_ticker("ABC")


def test_validate_date_normalizes_iso_format():
    assert validate_date("2026-08-15") == "2026-08-15"


def test_validate_date_rejects_bad_format():
    with pytest.raises(ValueError):
        validate_date("15/08/2026")


@pytest.fixture
def empty_registry(tmp_path):
    """TargetRegistry backed by a tmp_path file so tests never touch the
    real data/hkex_targets.json."""
    path = tmp_path / "targets.json"
    path.write_text("[]", encoding="utf-8")
    return TargetRegistry(path=path)


def test_fresh_install_seeds_no_targets(tmp_path):
    """A missing targets file must seed empty -- a stale default target
    would sit as "pending" forever and widen the daemon's scrape window."""
    reg = TargetRegistry(path=tmp_path / "targets.json")
    assert reg.load() == []


class TestTargetRegistry:
    def test_add_target_creates_entry(self, empty_registry):
        reg = empty_registry
        entry = reg.add_target("700", "2026-08-15")
        assert entry == {"ticker": "00700", "target_date": "2026-08-15", "status": "active"}
        assert reg.load() == [entry]

    def test_add_target_same_ticker_same_date_updates_status_not_duplicates(self, empty_registry):
        reg = empty_registry
        reg.add_target("700", "2026-08-15")
        reg.add_target("700", "2026-08-15", status="inactive")
        targets = reg.load()
        assert len(targets) == 1
        assert targets[0]["status"] == "inactive"

    def test_add_target_same_ticker_different_date_creates_second_entry(self, empty_registry):
        """A ticker can be watched for more than one date at once (e.g. an
        interim and a final dividend) -- these must NOT collapse into one
        entry. Regression test for the bug where daemon.py's matching logic
        used to silently drop all but one date per ticker."""
        reg = empty_registry
        reg.add_target("700", "2026-08-15")
        reg.add_target("700", "2026-11-01")
        targets = reg.load()
        assert len(targets) == 2
        dates = {t["target_date"] for t in targets}
        assert dates == {"2026-08-15", "2026-11-01"}

    def test_remove_target_removes_all_dates_for_ticker(self, empty_registry):
        reg = empty_registry
        reg.add_target("700", "2026-08-15")
        reg.add_target("700", "2026-11-01")
        reg.add_target("5", "2026-09-01")
        removed = reg.remove_target("700")
        assert removed == 2
        remaining = reg.load()
        assert len(remaining) == 1
        assert remaining[0]["ticker"] == "00005"

    def test_set_status_updates_all_matching_ticker_entries(self, empty_registry):
        reg = empty_registry
        reg.add_target("700", "2026-08-15")
        reg.add_target("700", "2026-11-01")
        changed = reg.set_status("700", "inactive")
        assert changed == 2
        assert all(t["status"] == "inactive" for t in reg.load())

    def test_active_targets_filters_by_status(self, empty_registry):
        reg = empty_registry
        reg.add_target("700", "2026-08-15")
        reg.add_target("5", "2026-09-01", status="inactive")
        active = reg.active_targets()
        assert len(active) == 1
        assert active[0]["ticker"] == "00700"


class TestWatchlistTickers:
    def test_add_normalizes_ticker_and_stores_name(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        entry = store.add("700", "Tencent Holdings Limited")
        assert entry == {"ticker": "00700", "name": "Tencent Holdings Limited"}
        assert store.load() == [entry]

    def test_add_without_name_defaults_to_null(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        entry = store.add("700")
        assert entry["name"] is None

    def test_re_adding_same_ticker_does_not_duplicate(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        store.add("700", "Tencent")
        store.add("700", "Tencent")
        assert len(store.load()) == 1

    def test_re_adding_backfills_a_missing_name(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        store.add("700")  # no name yet
        entry = store.add("700", "Tencent Holdings Limited")
        assert entry["name"] == "Tencent Holdings Limited"
        assert len(store.load()) == 1

    def test_re_adding_does_not_overwrite_an_existing_name(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        store.add("700", "Original Name")
        entry = store.add("700", "Different Name")
        assert entry["name"] == "Original Name"

    def test_tickers_returns_just_the_codes(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        store.add("700", "Tencent")
        store.add("5", "HSBC")
        assert store.tickers() == ["00700", "00005"]

    def test_remove_deletes_entry_and_reports_count(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        store.add("700", "Tencent")
        removed = store.remove("700")
        assert removed == 1
        assert store.load() == []

    def test_remove_missing_ticker_reports_zero(self, tmp_path):
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        assert store.remove("700") == 0

    def test_empty_file_seeds_empty_list_not_defaults(self, tmp_path):
        """Unlike TargetRegistry, a missing file must seed an empty list --
        there is no sensible default ticker to pre-populate."""
        store = WatchlistTickers(path=tmp_path / "watchlist_tickers.json")
        assert store.load() == []


class TestNotifiedCache:
    def test_is_seen_false_until_notified_or_processed(self, tmp_path):
        cache = NotifiedCache(path=tmp_path / "notified.json")
        assert not cache.is_seen("abc123")
        cache.mark_notified("abc123")
        assert cache.is_seen("abc123")

    def test_id_lists_are_capped_keeping_most_recent(self, tmp_path, monkeypatch):
        cache = NotifiedCache(path=tmp_path / "notified.json")
        monkeypatch.setattr(NotifiedCache, "MAX_IDS_PER_LIST", 5)
        data = cache.load()
        data["notified"] = [f"id{i}" for i in range(10)]
        cache.save(data)
        saved = cache.load()
        assert saved["notified"] == ["id5", "id6", "id7", "id8", "id9"]

    def test_is_pinged_independent_of_is_seen(self, tmp_path):
        cache = NotifiedCache(path=tmp_path / "notified.json")
        cache.mark_pinged("abc123")
        assert cache.is_pinged("abc123")
        assert not cache.is_seen("abc123")

    def test_record_failure_gives_up_after_max_retries(self, tmp_path):
        cache = NotifiedCache(path=tmp_path / "notified.json")
        assert cache.record_failure("abc123", max_retries=3) == 1
        assert cache.record_failure("abc123", max_retries=3) == 2
        assert not cache.is_seen("abc123")
        assert cache.record_failure("abc123", max_retries=3) == 3
        # Gave up: moved into `processed`, cleared from `failed`.
        assert cache.is_seen("abc123")
        assert "abc123" not in cache.load()["failed"]

    def test_mark_notified_clears_prior_failure(self, tmp_path):
        cache = NotifiedCache(path=tmp_path / "notified.json")
        cache.record_failure("abc123", max_retries=5)
        cache.mark_notified("abc123")
        assert "abc123" not in cache.load()["failed"]


class TestAlertHistory:
    def test_append_and_recent_order(self, tmp_path):
        history = AlertHistory(path=tmp_path / "alerts.json")
        history.append({"ticker": "1"})
        history.append({"ticker": "2"})
        recent = history.recent(limit=10)
        assert [r["ticker"] for r in recent] == ["2", "1"]

    def test_append_trims_to_max_entries(self, tmp_path, monkeypatch):
        history = AlertHistory(path=tmp_path / "alerts.json")
        monkeypatch.setattr(AlertHistory, "MAX_ENTRIES", 3)
        for i in range(5):
            history.append({"ticker": str(i)})
        stored = history.load()
        assert len(stored) == 3
        assert [r["ticker"] for r in stored] == ["2", "3", "4"]


class TestDividendStore:
    def test_mark_dividend_dedupes_by_filing_id(self, tmp_path):
        store = DividendStore(path=tmp_path / "dividends.json")
        store.mark_dividend({"filingId": "abc123", "ticker": "00700"})
        store.mark_dividend({"filingId": "abc123", "ticker": "00700"})
        assert len(store.load()) == 1

    def test_recent_filters_by_ticker(self, tmp_path):
        store = DividendStore(path=tmp_path / "dividends.json")
        store.mark_dividend({"filingId": "a", "ticker": "00700"})
        store.mark_dividend({"filingId": "b", "ticker": "00005"})
        filtered = store.recent(ticker="00700")
        assert len(filtered) == 1
        assert filtered[0]["ticker"] == "00700"

    def test_ensure_seeded_backfills_from_alert_history_deduped_by_url(self, tmp_path, monkeypatch):
        alerts_path = tmp_path / "alerts.json"
        dividends_path = tmp_path / "dividends.json"
        history = AlertHistory(path=alerts_path)
        history.append(
            {
                "ticker": "00700",
                "company_name": "Tencent",
                "payout_amount": "HKD 1.00",
                "source_url": "https://example.com/a.pdf",
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        )
        # A "ping" entry with the same source_url as an existing record should
        # not create a second, duplicate dividends.json row.
        history.append(
            {
                "kind": "ping",
                "ticker": "00700",
                "source_url": "https://example.com/a.pdf",
                "timestamp": "2026-01-01T00:00:05+00:00",
            }
        )

        # ensure_seeded() reads AlertHistory() at its default path -- point
        # the default constructor at our tmp file for this test only.
        import monitor.registry as registry_module

        monkeypatch.setattr(registry_module, "AlertHistory", lambda: AlertHistory(path=alerts_path))

        store = DividendStore(path=dividends_path)
        store.ensure_seeded()

        records = store.load()
        assert len(records) == 1
        assert records[0]["ticker"] == "00700"

    def test_ensure_seeded_is_noop_if_file_already_exists(self, tmp_path):
        dividends_path = tmp_path / "dividends.json"
        store = DividendStore(path=dividends_path)
        store.mark_dividend({"filingId": "x", "ticker": "00700"})
        store.ensure_seeded()
        assert len(store.load()) == 1


class TestChannelHealth:
    def test_record_success_sets_last_success_and_resets_failures(self, tmp_path):
        health = ChannelHealth(path=tmp_path / "channel_health.json")
        health.record("slack", True)
        data = health.load()
        assert data["slack"]["last_success_at"] is not None
        assert data["slack"]["consecutive_failures"] == 0

    def test_record_failure_accumulates_consecutive_count(self, tmp_path):
        health = ChannelHealth(path=tmp_path / "channel_health.json")
        health.record("slack", False)
        health.record("slack", False)
        data = health.load()
        assert data["slack"]["consecutive_failures"] == 2
        assert data["slack"]["last_failure_at"] is not None

    def test_success_after_failures_resets_streak(self, tmp_path):
        health = ChannelHealth(path=tmp_path / "channel_health.json")
        health.record("slack", False)
        health.record("slack", False)
        health.record("slack", True)
        data = health.load()
        assert data["slack"]["consecutive_failures"] == 0
        assert data["slack"]["last_success_at"] is not None
        # last_failure_at is kept as history, not cleared by a later success.
        assert data["slack"]["last_failure_at"] is not None

    def test_channels_tracked_independently(self, tmp_path):
        health = ChannelHealth(path=tmp_path / "channel_health.json")
        health.record("slack", True)
        health.record("discord", False)
        data = health.load()
        assert data["slack"]["consecutive_failures"] == 0
        assert data["discord"]["consecutive_failures"] == 1

    def test_unused_channel_absent_from_health(self, tmp_path):
        health = ChannelHealth(path=tmp_path / "channel_health.json")
        health.record("slack", True)
        data = health.load()
        assert "discord" not in data
