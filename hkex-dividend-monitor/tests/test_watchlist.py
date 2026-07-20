import threading
import time
from datetime import date, timedelta

import monitor.config as config
import monitor.watchlist as watchlist


def _fake_registry(targets=None):
    targets = targets or []
    return lambda: type("R", (), {"active_targets": lambda self: targets})()


def _fake_ticker_store(entries=None):
    entries = entries or []
    tickers = [e["ticker"] for e in entries if e.get("ticker")]
    return lambda: type("S", (), {"load": lambda self: entries, "tickers": lambda self: tickers})()


class TestIngestFilingAsEvent:
    def _rec(self, **overrides):
        rec = {
            "filingId": "abcdef0123456789",
            "title": "Notice of Board Meeting",
            "date": "14/07/2026",
            "stockCode": "00700",
            "stockName": "Tencent",
            "link": "https://www1.hkexnews.hk/a.pdf",
        }
        rec.update(overrides)
        return rec

    def test_other_titles_are_skipped_entirely(self, monkeypatch):
        calls = []
        monkeypatch.setattr(watchlist, "upsert_filing_metadata", lambda recs: calls.append("upsert"))
        monkeypatch.setattr(watchlist, "extract_and_save_filing", lambda fid, url: calls.append("extract"))
        watchlist._ingest_filing_as_event(self._rec(title="Change of Company Secretary"), "00700")
        assert calls == []

    def test_successful_ingest_persists_event(self, monkeypatch):
        monkeypatch.setattr(watchlist, "upsert_filing_metadata", lambda recs: None)
        monkeypatch.setattr(watchlist, "extract_and_save_filing", lambda fid, url: "some filing text")

        class _Extraction:
            event_kind = "board_meeting"
            board_meeting_date = "2026-07-20"
            board_meeting_purpose_approves_results = True
            board_meeting_purpose_considers_dividend = False
            board_meeting_purpose_raw = "to approve results"
            results_period = None
            dividend_type = None
            dividend_amount = None
            ex_date = None
            record_date = None
            payment_date = None
            declared_date = None

        monkeypatch.setattr(watchlist, "extract_announcement", lambda fid, title, text: _Extraction())
        saved = {}
        monkeypatch.setattr(watchlist.history, "upsert_event", lambda ev: saved.update(ev))

        watchlist._ingest_filing_as_event(self._rec(), "00700")
        assert saved["filingId"] == "abcdef0123456789"
        assert saved["eventKind"] == "board_meeting"
        assert saved["boardMeetingDate"] == date(2026, 7, 20)
        assert saved["extractionStatus"] == "ok"
        assert saved["companyTicker"] == "0700.HK"

    def test_extraction_failure_skips_event_persistence(self, monkeypatch):
        monkeypatch.setattr(watchlist, "upsert_filing_metadata", lambda recs: None)

        def _raise(fid, url):
            raise watchlist.DocumentExtractionError("download failed")

        monkeypatch.setattr(watchlist, "extract_and_save_filing", _raise)
        called = []
        monkeypatch.setattr(watchlist.history, "upsert_event", lambda ev: called.append(ev))
        watchlist._ingest_filing_as_event(self._rec(), "00700")
        assert called == []

    def test_llm_failure_falls_back_to_coarse_classification(self, monkeypatch):
        monkeypatch.setattr(watchlist, "upsert_filing_metadata", lambda recs: None)
        monkeypatch.setattr(watchlist, "extract_and_save_filing", lambda fid, url: "text")

        def _raise(fid, title, text):
            raise watchlist.ExtractionError("LLM down")

        monkeypatch.setattr(watchlist, "extract_announcement", _raise)
        saved = {}
        monkeypatch.setattr(watchlist.history, "upsert_event", lambda ev: saved.update(ev))

        watchlist._ingest_filing_as_event(self._rec(title="Notice of Board Meeting"), "00700")
        assert saved["eventKind"] == "board_meeting"  # coarse fallback from classify_title
        assert saved["extractionStatus"] == "failed"


class TestDiscoverUniverse:
    def test_ticker_list_and_active_targets_are_unioned_and_deduped(self, monkeypatch):
        cfg = config.Config()
        monkeypatch.setattr(
            watchlist,
            "WatchlistTickers",
            _fake_ticker_store([{"ticker": "00700", "name": "Tencent"}, {"ticker": "00005", "name": "HSBC"}]),
        )
        monkeypatch.setattr(
            watchlist,
            "TargetRegistry",
            _fake_registry([{"ticker": "00700", "target_date": "2026-08-01", "status": "active"},
                             {"ticker": "01299", "target_date": "2026-08-01", "status": "active"}]),
        )
        tickers, names = watchlist._discover_universe(cfg)
        assert tickers == ["00700", "00005", "01299"]  # ticker-list order first, dedup on overlap
        assert names["00700"] == "Tencent"
        assert names["00005"] == "HSBC"
        assert names["01299"] is None  # no name captured for an alert-only ticker

    def test_capped_at_max_candidates(self, monkeypatch):
        cfg = config.Config(watchlist_max_candidates=2)
        entries = [{"ticker": f"{i:05d}", "name": None} for i in range(5)]
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store(entries))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())
        tickers, _names = watchlist._discover_universe(cfg)
        assert len(tickers) == 2

    def test_active_targets_read_failure_still_returns_ticker_list(self, monkeypatch):
        cfg = config.Config()
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([{"ticker": "00700", "name": None}]))

        def _raise():
            raise RuntimeError("registry read failed")

        monkeypatch.setattr(watchlist, "TargetRegistry", lambda: type("R", (), {"active_targets": lambda self: _raise()})())
        tickers, _names = watchlist._discover_universe(cfg)
        assert tickers == ["00700"]

    def test_empty_universe_returns_empty_list(self, monkeypatch):
        cfg = config.Config()
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([]))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())
        tickers, names = watchlist._discover_universe(cfg)
        assert tickers == []
        assert names == {}


class TestProcessTicker:
    def test_searches_all_three_keywords_and_dedupes_by_filing_id(self, monkeypatch):
        cfg = config.Config()
        today = date(2026, 7, 14)
        captured_keywords = []
        shared_rec = {"filingId": "1" * 16, "stockCode": "00700", "stockName": "Tencent",
                      "title": "Notice of Board Meeting", "date": "14/07/2026", "link": "https://x/a.pdf"}

        def fake_search(ticker, from_date, to_date, title_keyword=""):
            captured_keywords.append(title_keyword)
            return [shared_rec]  # same filing surfaces under every keyword

        monkeypatch.setattr(watchlist, "search_filings_by_ticker", fake_search)
        ingested = []
        monkeypatch.setattr(watchlist, "_ingest_filing_as_event", lambda rec, t: ingested.append(rec["filingId"]))
        monkeypatch.setattr(watchlist.history, "events_for_ticker", lambda t: [{"stub": True}])

        events = watchlist._process_ticker("00700", today, cfg, set())
        assert set(captured_keywords) == {"board meeting", "results", "dividend"}
        assert ingested == ["1" * 16]  # deduped across the 3 searches, not ingested 3x
        assert events == [{"stub": True}]

    def test_known_filings_are_not_reingested(self, monkeypatch):
        cfg = config.Config()
        today = date(2026, 7, 14)
        rec = {"filingId": "1" * 16, "stockCode": "00700", "stockName": "Tencent",
               "title": "Notice of Board Meeting", "date": "14/07/2026", "link": "https://x/a.pdf"}
        monkeypatch.setattr(watchlist, "search_filings_by_ticker", lambda ticker, f, t, title_keyword="": [rec])
        ingested = []
        monkeypatch.setattr(watchlist, "_ingest_filing_as_event", lambda r, t: ingested.append(r["filingId"]))
        monkeypatch.setattr(watchlist.history, "events_for_ticker", lambda t: [])

        watchlist._process_ticker("00700", today, cfg, {"1" * 16})
        assert ingested == []

    def test_bounded_to_max_new_filings_per_ticker(self, monkeypatch):
        cfg = config.Config()
        today = date(2026, 7, 14)
        records = [
            {"filingId": f"{i:016x}", "stockCode": "00700", "stockName": "Tencent",
             "title": "Interim Dividend", "date": "14/07/2026", "link": "https://x"}
            for i in range(30)
        ]
        monkeypatch.setattr(watchlist, "search_filings_by_ticker", lambda ticker, f, t, title_keyword="": records)
        ingested = []
        monkeypatch.setattr(watchlist, "_ingest_filing_as_event", lambda r, t: ingested.append(r["filingId"]))
        monkeypatch.setattr(watchlist.history, "events_for_ticker", lambda t: [])

        watchlist._process_ticker("00700", today, cfg, set())
        assert len(ingested) == watchlist._MAX_NEW_FILINGS_PER_TICKER

    def test_one_keyword_search_failing_does_not_abort_the_others(self, monkeypatch):
        cfg = config.Config()
        today = date(2026, 7, 14)

        def fake_search(ticker, f, t, title_keyword=""):
            if title_keyword == "board meeting":
                raise watchlist.HKEXSearchError("boom")
            return [{"filingId": f"{title_keyword}".ljust(16, "0"), "stockCode": "00700", "stockName": "Tencent",
                     "title": "Annual Results", "date": "14/07/2026", "link": "https://x"}]

        monkeypatch.setattr(watchlist, "search_filings_by_ticker", fake_search)
        ingested = []
        monkeypatch.setattr(watchlist, "_ingest_filing_as_event", lambda r, t: ingested.append(r["filingId"]))
        monkeypatch.setattr(watchlist.history, "events_for_ticker", lambda t: [])

        watchlist._process_ticker("00700", today, cfg, set())
        assert len(ingested) == 2  # "results" and "dividend" searches still ran


class TestGenerateWatchlistOrchestration:
    def test_zero_score_candidates_are_dropped(self, monkeypatch):
        cfg = config.Config()
        monkeypatch.setattr(watchlist, "get_config", lambda: cfg)
        monkeypatch.setattr(watchlist.history, "ensure_schema", lambda: None)
        monkeypatch.setattr(watchlist.history, "known_filing_ids", lambda: set())
        monkeypatch.setattr(watchlist, "explain", lambda *a, **k: None)

        today = date(2026, 7, 14)
        events_by_ticker = {
            "00700": [
                {
                    "filingId": "1" * 16,
                    "eventKind": "board_meeting",
                    "boardMeetingDate": today,
                    "boardMeetingPurposeApprovesResults": True,
                    "boardMeetingPurposeConsidersDividend": True,
                    "announcementDate": today,
                    "stockName": "Tencent",
                }
            ],
            "00005": [],  # no signal at all -- must be dropped
        }
        monkeypatch.setattr(
            watchlist, "_discover_universe",
            lambda c: (["00700", "00005"], {"00700": "Tencent", "00005": "HSBC"}),
        )
        monkeypatch.setattr(watchlist, "_process_ticker", lambda ticker, t, c, k: events_by_ticker[ticker])

        saved = {}
        monkeypatch.setattr(watchlist.history, "save_watchlist", lambda d, g, rows: saved.update(date=d, rows=rows))

        rows, generated_at = watchlist.generate_watchlist(today)

        assert len(rows) == 1
        assert rows[0]["stockCode"] == "00700"
        assert rows[0]["rank"] == 1
        assert saved["rows"] == rows
        assert saved["date"] == today
        assert generated_at  # non-empty ISO timestamp

    def test_multiple_candidates_ranked_by_score_descending(self, monkeypatch):
        cfg = config.Config()
        monkeypatch.setattr(watchlist, "get_config", lambda: cfg)
        monkeypatch.setattr(watchlist.history, "ensure_schema", lambda: None)
        monkeypatch.setattr(watchlist.history, "known_filing_ids", lambda: set())
        monkeypatch.setattr(watchlist, "explain", lambda *a, **k: None)
        monkeypatch.setattr(watchlist.history, "save_watchlist", lambda *a, **k: None)

        today = date(2026, 7, 14)
        events = {
            "00001": [
                {
                    "filingId": "1" * 16,
                    "eventKind": "board_meeting",
                    "boardMeetingDate": today + timedelta(days=2),
                    "announcementDate": today,
                    "stockName": "Weak Signal Co",
                }
            ],
            "00002": [
                {
                    "filingId": "2" * 16,
                    "eventKind": "board_meeting",
                    "boardMeetingDate": today,
                    "boardMeetingPurposeApprovesResults": True,
                    "boardMeetingPurposeConsidersDividend": True,
                    "announcementDate": today,
                    "stockName": "Strong Signal Co",
                }
            ],
        }
        monkeypatch.setattr(
            watchlist, "_discover_universe",
            lambda c: (["00001", "00002"], {"00001": None, "00002": None}),
        )
        monkeypatch.setattr(watchlist, "_process_ticker", lambda ticker, t, c, k: events[ticker])

        rows, _ = watchlist.generate_watchlist(today)
        assert [r["stockCode"] for r in rows] == ["00002", "00001"]
        assert rows[0]["rank"] == 1
        assert rows[1]["rank"] == 2
        # The board-meeting notice is cited as evidence for the top row.
        assert any(e["filingId"] == "2" * 16 for e in rows[0]["evidence"])

    def test_repeated_generation_does_not_accumulate_rows(self, monkeypatch):
        """Regression guard: generate_watchlist must build a fresh row list
        each call, not append across calls -- duplicate-avoidance for the
        DELETE-then-insert persistence in monitor.history depends on the
        caller (this function) never handing it a growing list."""
        cfg = config.Config()
        monkeypatch.setattr(watchlist, "get_config", lambda: cfg)
        monkeypatch.setattr(watchlist.history, "ensure_schema", lambda: None)
        monkeypatch.setattr(watchlist.history, "known_filing_ids", lambda: set())
        monkeypatch.setattr(watchlist, "explain", lambda *a, **k: None)
        save_calls = []
        monkeypatch.setattr(watchlist.history, "save_watchlist", lambda d, g, rows: save_calls.append(rows))

        today = date(2026, 7, 14)
        events = [
            {
                "filingId": "1" * 16,
                "eventKind": "board_meeting",
                "boardMeetingDate": today,
                "announcementDate": today,
                "stockName": "Tencent",
            }
        ]
        monkeypatch.setattr(watchlist, "_discover_universe", lambda c: (["00700"], {"00700": None}))
        monkeypatch.setattr(watchlist, "_process_ticker", lambda ticker, t, c, k: events)

        watchlist.generate_watchlist(today)
        watchlist.generate_watchlist(today)

        assert len(save_calls) == 2
        assert len(save_calls[0]) == len(save_calls[1]) == 1  # same size each time, not doubled

    def test_empty_universe_saves_empty_watchlist(self, monkeypatch):
        cfg = config.Config()
        monkeypatch.setattr(watchlist, "get_config", lambda: cfg)
        monkeypatch.setattr(watchlist.history, "ensure_schema", lambda: None)
        monkeypatch.setattr(watchlist.history, "known_filing_ids", lambda: set())
        monkeypatch.setattr(watchlist, "_discover_universe", lambda c: ([], {}))
        saved = {}
        monkeypatch.setattr(watchlist.history, "save_watchlist", lambda d, g, rows: saved.update(rows=rows))

        rows, _ = watchlist.generate_watchlist(date(2026, 7, 14))
        assert rows == []
        assert saved["rows"] == []


class TestGetOrGenerateToday:
    def test_reuses_cache_without_regenerating(self, monkeypatch):
        cached = {"generatedAt": "2026-07-14T01:00:00Z", "rows": [{"stockCode": "00700"}]}
        monkeypatch.setattr(watchlist.history, "load_watchlist", lambda d: cached)

        def _fail(today):
            raise AssertionError("generate_watchlist must not run when a cache hit exists")

        monkeypatch.setattr(watchlist, "generate_watchlist", _fail)
        result = watchlist.get_or_generate_today(force=False)
        assert result["status"] == "ready"
        assert result["rows"] == cached["rows"]

    def test_missing_cache_generates_once(self, monkeypatch):
        monkeypatch.setattr(watchlist.history, "load_watchlist", lambda d: None)
        calls = []
        monkeypatch.setattr(watchlist, "generate_watchlist", lambda today: (calls.append(1), ([{"stockCode": "00700"}], "now"))[1])
        result = watchlist.get_or_generate_today(force=False)
        assert len(calls) == 1
        assert result["rows"] == [{"stockCode": "00700"}]

    def test_force_regenerates_even_if_cache_exists(self, monkeypatch):
        cached = {"generatedAt": "old", "rows": [{"stockCode": "stale"}]}
        monkeypatch.setattr(watchlist.history, "load_watchlist", lambda d: cached)
        calls = []
        monkeypatch.setattr(watchlist, "generate_watchlist", lambda today: (calls.append(1), ([{"stockCode": "fresh"}], "new"))[1])
        result = watchlist.get_or_generate_today(force=True)
        assert len(calls) == 1
        assert result["rows"] == [{"stockCode": "fresh"}]


class TestGetOrGenerateTodayConcurrency:
    def test_concurrent_calls_generate_at_most_once(self, monkeypatch):
        store = {}
        calls = []
        gen_started = threading.Event()
        gen_can_finish = threading.Event()

        monkeypatch.setattr(watchlist.history, "load_watchlist", lambda d: store.get(d))

        def fake_generate(today):
            calls.append(1)
            gen_started.set()
            gen_can_finish.wait(timeout=2)
            rows = [{"stockCode": "00700", "score": 10, "band": "Low", "rank": 1}]
            store[today] = {"generatedAt": "now", "rows": rows}
            return rows, "now"

        monkeypatch.setattr(watchlist, "generate_watchlist", fake_generate)

        results = []

        def worker():
            results.append(watchlist.get_or_generate_today(force=False))

        t1 = threading.Thread(target=worker)
        t1.start()
        assert gen_started.wait(timeout=2)

        t2 = threading.Thread(target=worker)
        t2.start()
        time.sleep(0.1)  # give t2 time to block on the lock t1 holds
        gen_can_finish.set()

        t1.join(timeout=2)
        t2.join(timeout=2)

        assert len(calls) == 1  # generate_watchlist ran exactly once
        assert len(results) == 2
        assert results[0]["rows"] == results[1]["rows"]


class _SpyThread:
    instances: list = []

    def __init__(self, target=None, name=None, daemon=None):
        self.target = target
        _SpyThread.instances.append(self)

    def start(self):
        pass


class TestHasTrackedTickers:
    def test_true_when_ticker_list_non_empty(self, monkeypatch):
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([{"ticker": "00700"}]))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())
        assert watchlist.has_tracked_tickers() is True

    def test_true_when_only_an_active_alert_target_exists(self, monkeypatch):
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([]))
        monkeypatch.setattr(
            watchlist, "TargetRegistry",
            _fake_registry([{"ticker": "00700", "target_date": "2026-08-01", "status": "active"}]),
        )
        assert watchlist.has_tracked_tickers() is True

    def test_false_when_both_are_empty(self, monkeypatch):
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([]))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())
        assert watchlist.has_tracked_tickers() is False

    def test_false_on_registry_read_failure_with_empty_ticker_list(self, monkeypatch):
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([]))

        def _raise():
            raise RuntimeError("boom")

        monkeypatch.setattr(watchlist, "TargetRegistry", lambda: type("R", (), {"active_targets": lambda self: _raise()})())
        assert watchlist.has_tracked_tickers() is False


class TestTriggerBackgroundGenerate:
    def setup_method(self):
        _SpyThread.instances = []

    def test_noop_when_already_generating(self, monkeypatch):
        monkeypatch.setattr(watchlist.threading, "Thread", _SpyThread)
        watchlist._generation_lock.acquire()
        try:
            watchlist.trigger_background_generate()
        finally:
            watchlist._generation_lock.release()
        assert _SpyThread.instances == []

    def test_noop_when_nothing_tracked(self, monkeypatch):
        monkeypatch.setattr(watchlist.threading, "Thread", _SpyThread)
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([]))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())
        watchlist.trigger_background_generate()
        assert _SpyThread.instances == []

    def test_noop_when_todays_watchlist_already_exists(self, monkeypatch):
        monkeypatch.setattr(watchlist.threading, "Thread", _SpyThread)
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([{"ticker": "00700"}]))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())
        monkeypatch.setattr(watchlist.history, "watchlist_exists", lambda d: True)
        watchlist.trigger_background_generate()
        assert _SpyThread.instances == []

    def test_starts_one_thread_when_nothing_generated_yet(self, monkeypatch):
        monkeypatch.setattr(watchlist.threading, "Thread", _SpyThread)
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([{"ticker": "00700"}]))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())
        monkeypatch.setattr(watchlist.history, "watchlist_exists", lambda d: False)
        called = []
        monkeypatch.setattr(watchlist, "get_or_generate_today", lambda force=False: called.append(force))

        watchlist.trigger_background_generate()
        assert len(_SpyThread.instances) == 1
        _SpyThread.instances[0].target()  # simulate the thread running its target
        assert called == [False]

    def test_db_check_failure_still_attempts_generation(self, monkeypatch):
        monkeypatch.setattr(watchlist.threading, "Thread", _SpyThread)
        monkeypatch.setattr(watchlist, "WatchlistTickers", _fake_ticker_store([{"ticker": "00700"}]))
        monkeypatch.setattr(watchlist, "TargetRegistry", _fake_registry())

        def _raise(d):
            raise RuntimeError("db down")

        monkeypatch.setattr(watchlist.history, "watchlist_exists", _raise)
        watchlist.trigger_background_generate()
        assert len(_SpyThread.instances) == 1
