from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import monitor.activity as activity
import monitor.config as config
import monitor.web as web
from monitor.extractor import ExtractionError
from monitor.hkex_search import HKEXSearchError
from monitor.registry import (
    AlertHistory,
    ChannelHealth,
    DividendStore,
    NotifiedCache,
    TargetRegistry,
    WatchlistTickers,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient wired to tmp_path-backed stores and an isolated config,
    so these tests never touch the real data/ directory, real env vars, or
    make a real DeepSeek/HKEX call."""
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    config._cached_config = None
    config._cached_settings_mtime = None
    for var in (
        "DEEPSEEK_API_KEY",
        "SLACK_WEBHOOK_URL",
        "DISCORD_WEBHOOK_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(var, raising=False)

    targets_path = tmp_path / "targets.json"
    targets_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(web, "registry", TargetRegistry(path=targets_path))
    monkeypatch.setattr(web, "notified_cache", NotifiedCache(path=tmp_path / "notified.json"))
    monkeypatch.setattr(web, "alert_history", AlertHistory(path=tmp_path / "alerts.json"))
    monkeypatch.setattr(web, "dividend_store", DividendStore(path=tmp_path / "dividends.json"))
    monkeypatch.setattr(web, "channel_health", ChannelHealth(path=tmp_path / "channel_health.json"))
    monkeypatch.setattr(web, "watchlist_tickers", WatchlistTickers(path=tmp_path / "watchlist_tickers.json"))
    # The lifespan hook ensures the watchlist schema and kicks off
    # best-effort Dividend Watchlist generation on startup
    # (monitor.history.ensure_schema / monitor.watchlist.trigger_background_generate);
    # neutralize both here so unrelated tests don't make a real SurrealDB
    # call. TestDividendWatchlist below re-patches what it needs per test.
    monkeypatch.setattr(web.history, "ensure_schema", lambda: None)
    monkeypatch.setattr(web.watchlist, "trigger_background_generate", lambda: None)
    # Same reasoning for the SGX settlement-history schema call.
    monkeypatch.setattr(web.settlement_history, "ensure_schema", lambda: None)
    # ...and for the board meetings startup refresh (a real HKEXnews fetch
    # in a background thread); individual tests re-patch fetch_board_meetings.
    monkeypatch.setattr(web.board_meetings, "trigger_background_refresh", lambda: None)

    with TestClient(web.app) as c:
        yield c

    config._cached_config = None
    config._cached_settings_mtime = None


class TestResolveTicker:
    def test_valid_ticker_resolves(self, client, monkeypatch):
        monkeypatch.setattr(web, "lookup_stock_id", lambda code: {"stockId": 1, "code": "00700", "name": "TENCENT HOLDINGS LIMITED"})
        resp = client.get("/api/resolve-ticker/700")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "00700"
        assert body["name"] == "TENCENT HOLDINGS LIMITED"

    def test_unknown_ticker_returns_404(self, client, monkeypatch):
        def _raise(code):
            raise HKEXSearchError(f"No HKEX-listed stock found for code {code}")

        monkeypatch.setattr(web, "lookup_stock_id", _raise)
        resp = client.get("/api/resolve-ticker/99999")
        assert resp.status_code == 404

    def test_invalid_ticker_format_returns_400(self, client):
        resp = client.get("/api/resolve-ticker/ABC")
        assert resp.status_code == 400


class TestDeepseekConnection:
    def test_success_returns_ok(self, client, monkeypatch):
        monkeypatch.setattr(web, "test_deepseek_connection", lambda: None)
        resp = client.post("/api/test-deepseek")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_failure_returns_400_with_detail(self, client, monkeypatch):
        def _raise():
            raise ExtractionError("DeepSeek connection test failed: bad key")

        monkeypatch.setattr(web, "test_deepseek_connection", _raise)
        resp = client.post("/api/test-deepseek")
        assert resp.status_code == 400
        assert "bad key" in resp.json()["detail"]


class TestTargetsEnrichedWithMatchStatus:
    def test_future_target_is_upcoming(self, client):
        future = (date.today() + timedelta(days=30)).isoformat()
        client.post("/api/targets", json={"ticker": "700", "target_date": future})
        resp = client.get("/api/targets")
        assert resp.status_code == 200
        targets = resp.json()
        assert targets[0]["match_status"] == "upcoming"

    def test_past_target_with_nothing_recorded_is_pending(self, client):
        client.post("/api/targets", json={"ticker": "700", "target_date": "2020-01-01"})
        resp = client.get("/api/targets")
        targets = resp.json()
        assert targets[0]["match_status"] == "pending"

    def test_inactive_target_reports_inactive(self, client):
        client.post("/api/targets", json={"ticker": "700", "target_date": "2020-01-01"})
        web.registry.set_status("700", "inactive")  # no deactivate endpoint; CLI/chat-only today
        resp = client.get("/api/targets")
        targets = resp.json()
        assert targets[0]["status"] == "inactive"
        assert targets[0]["match_status"] == "inactive"


class TestStatusIncludesChannelHealth:
    def test_channel_health_key_present(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert "channel_health" in resp.json()
        assert resp.json()["channel_health"] == {}

    def test_status_reports_tls_trust_store(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        # One of the known bootstrap states (see monitor._ssl_bootstrap); in the
        # test env truststore is installed, so it should be "injected".
        assert resp.json()["tls_trust_store"] in {
            "injected", "disabled", "unavailable", "not-run",
        } or resp.json()["tls_trust_store"].startswith("failed")


class TestLatestFilings:
    _RECORDS = [
        {
            "filingId": "fid1",
            "date": "17/07/2026",
            "dateTime": "17/07/2026 08:13",
            "stockCode": "09678",
            "stockName": "UNISOUND",
            "title": "VOLUNTARY ANNOUNCEMENT SIGNED MULTIPLE LLM PROJECTS",
            "category": "Announcements and Notices - [Other - Business Update]",
            "link": "https://www1.hkexnews.hk/a.pdf",
        }
    ]

    def test_returns_shaped_overview_and_ingests(self, client, monkeypatch):
        ingested = {}
        monkeypatch.setattr(web, "fetch_latest_filings", lambda limit=20, days=1: self._RECORDS)
        monkeypatch.setattr(web, "upsert_filing_metadata", lambda recs: ingested.setdefault("n", len(recs)))

        resp = client.get("/api/latest-filings?limit=20")
        assert resp.status_code == 200
        body = resp.json()
        assert body["days"] == 1
        assert body["showing"] == 1
        assert body["filings"][0]["stockCode"] == "09678"
        assert body["filings"][0]["dateTime"] == "17/07/2026 08:13"
        assert body["filings"][0]["documentUrl"] == "https://www1.hkexnews.hk/a.pdf"
        assert ingested["n"] == 1  # results are ingested, not just returned

    def test_limit_param_is_clamped_and_days_passthrough(self, client, monkeypatch):
        captured = {}

        def fake_fetch(limit=20, days=1):
            captured.update(limit=limit, days=days)
            return []

        monkeypatch.setattr(web, "fetch_latest_filings", fake_fetch)
        monkeypatch.setattr(web, "upsert_filing_metadata", lambda recs: len(recs))

        resp = client.get("/api/latest-filings?limit=9999&days=7")
        assert resp.status_code == 200
        assert captured["limit"] == web.LATEST_FILINGS_MAX_LIMIT
        assert captured["days"] == 7

    def test_db_ingest_failure_does_not_blank_the_list(self, client, monkeypatch):
        # The feed itself worked -- a DB hiccup on the best-effort ingest
        # must not turn a good HKEX response into an error for the browser.
        monkeypatch.setattr(web, "fetch_latest_filings", lambda limit=20, days=1: self._RECORDS)

        def _raise(recs):
            raise RuntimeError("DB down")

        monkeypatch.setattr(web, "upsert_filing_metadata", _raise)
        resp = client.get("/api/latest-filings")
        assert resp.status_code == 200
        assert resp.json()["showing"] == 1

    def test_hkex_outage_surfaces_as_502(self, client, monkeypatch):
        def _raise(limit=20, days=1):
            raise HKEXSearchError("HKEX latest-filings feed failed: connection reset")

        monkeypatch.setattr(web, "fetch_latest_filings", _raise)
        resp = client.get("/api/latest-filings")
        assert resp.status_code == 502
        assert "HKEX" in resp.json()["detail"]


class TestDividendWatchlist:
    _CACHED = {
        "generatedAt": "2026-07-14T01:00:00+00:00",
        "rows": [{"stockCode": "00700", "stockName": "Tencent", "score": 80, "band": "High", "rank": 1}],
    }

    def test_returns_cached_ranking_when_present(self, client, monkeypatch):
        monkeypatch.setattr(web.history, "load_watchlist", lambda d: self._CACHED)
        triggered = {"called": False}
        monkeypatch.setattr(web.watchlist, "trigger_background_generate", lambda: triggered.__setitem__("called", True))

        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["generatedAt"] == self._CACHED["generatedAt"]
        assert body["rows"][0]["stockCode"] == "00700"
        assert triggered["called"] is False  # already have data -- must not kick off generation

    def test_missing_ranking_triggers_background_generate_and_reports_generating(self, client, monkeypatch):
        monkeypatch.setattr(web.history, "load_watchlist", lambda d: None)
        monkeypatch.setattr(web.watchlist, "has_tracked_tickers", lambda: True)
        triggered = {"called": False}
        monkeypatch.setattr(web.watchlist, "trigger_background_generate", lambda: triggered.__setitem__("called", True))

        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "generating"
        assert body["rows"] == []
        assert triggered["called"] is True

    def test_nothing_tracked_returns_ready_empty_without_triggering_generation(self, client, monkeypatch):
        monkeypatch.setattr(web.history, "load_watchlist", lambda d: None)
        monkeypatch.setattr(web.watchlist, "has_tracked_tickers", lambda: False)
        triggered = {"called": False}
        monkeypatch.setattr(web.watchlist, "trigger_background_generate", lambda: triggered.__setitem__("called", True))

        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["rows"] == []
        assert triggered["called"] is False

    def test_db_outage_on_get_surfaces_as_502(self, client, monkeypatch):
        def _raise(d):
            raise web.SurrealDBError("connection refused")

        monkeypatch.setattr(web.history, "load_watchlist", _raise)
        resp = client.get("/api/watchlist")
        assert resp.status_code == 502

    def test_refresh_forces_regeneration(self, client, monkeypatch):
        captured = {}

        def fake_get_or_generate(force=False):
            captured["force"] = force
            return {"status": "ready", "generatedAt": "2026-07-14T02:00:00+00:00", "rows": []}

        monkeypatch.setattr(web.watchlist, "get_or_generate_today", fake_get_or_generate)
        resp = client.post("/api/watchlist/refresh")
        assert resp.status_code == 200
        assert captured["force"] is True
        assert resp.json()["status"] == "ready"

    def test_refresh_hkex_outage_surfaces_as_502(self, client, monkeypatch):
        def _raise(force=False):
            raise HKEXSearchError("HKEX unreachable")

        monkeypatch.setattr(web.watchlist, "get_or_generate_today", _raise)
        resp = client.post("/api/watchlist/refresh")
        assert resp.status_code == 502


class TestWatchlistTickerEndpoints:
    def test_add_ticker_normalizes_and_resolves_name(self, client, monkeypatch):
        monkeypatch.setattr(web, "lookup_stock_id", lambda code: {"stockId": 1, "code": "00700", "name": "TENCENT HOLDINGS LIMITED"})
        resp = client.post("/api/watchlist/tickers", json={"ticker": "700"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "00700"
        assert body["name"] == "TENCENT HOLDINGS LIMITED"

    def test_add_ticker_with_unresolvable_name_still_adds(self, client, monkeypatch):
        def _raise(code):
            raise HKEXSearchError("No HKEX-listed stock found")

        monkeypatch.setattr(web, "lookup_stock_id", _raise)
        resp = client.post("/api/watchlist/tickers", json={"ticker": "700"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "00700"
        assert body["name"] is None

    def test_add_invalid_ticker_returns_400(self, client):
        resp = client.post("/api/watchlist/tickers", json={"ticker": "ABC"})
        assert resp.status_code == 400

    def test_list_tickers_returns_added_entries(self, client, monkeypatch):
        monkeypatch.setattr(web, "lookup_stock_id", lambda code: {"name": "Tencent"})
        client.post("/api/watchlist/tickers", json={"ticker": "700"})
        resp = client.get("/api/watchlist/tickers")
        assert resp.status_code == 200
        assert resp.json()[0]["ticker"] == "00700"

    def test_remove_ticker(self, client, monkeypatch):
        monkeypatch.setattr(web, "lookup_stock_id", lambda code: {"name": "Tencent"})
        client.post("/api/watchlist/tickers", json={"ticker": "700"})
        resp = client.delete("/api/watchlist/tickers/700")
        assert resp.status_code == 200
        assert resp.json()["removed"] == 1
        assert client.get("/api/watchlist/tickers").json() == []

    def test_remove_unknown_ticker_returns_404(self, client):
        resp = client.delete("/api/watchlist/tickers/700")
        assert resp.status_code == 404


class TestActivity:
    @pytest.fixture(autouse=True)
    def _activity_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.log")

    def test_returns_events_newest_first(self, client):
        activity.log_event("test.source", "kind", "first event")
        activity.log_event("test.source", "kind", "second event")

        resp = client.get("/api/activity")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert [e["message"] for e in events] == ["second event", "first event"]

    def test_limit_is_clamped(self, client):
        for i in range(10):
            activity.log_event("test.source", "kind", f"event {i}")

        resp = client.get("/api/activity", params={"limit": 3})
        assert len(resp.json()["events"]) == 3

    def test_level_filters_to_warn_and_above(self, client):
        activity.log_event("s", "k", "a debug event", level="debug")
        activity.log_event("s", "k", "a warn event", level="warn")
        activity.log_event("s", "k", "an error event", level="error")

        resp = client.get("/api/activity", params={"level": "warn"})
        messages = [e["message"] for e in resp.json()["events"]]
        assert messages == ["an error event", "a warn event"]


class TestSettlementPrices:
    """Covers /api/settlement/* -- monitor.settlement's own fetchers are
    unit-tested against real-shaped fixtures in test_settlement.py; these
    tests only cover the web layer's wiring (params, error mapping,
    resolve-then-fetch flow)."""

    def test_hkex_returns_fetch_result(self, client, monkeypatch):
        fake = {"asOf": "now", "rows": [{"contract": "DAX"}], "contracts": ["DAX"], "productTypes": ["Equity Index"]}
        captured = {}

        def fake_fetch(force=False):
            captured["force"] = force
            return fake

        monkeypatch.setattr(web.settlement, "fetch_hkex_fsp", fake_fetch)
        resp = client.get("/api/settlement/hkex")
        assert resp.status_code == 200
        assert resp.json() == fake
        assert captured["force"] is False

    def test_hkex_refresh_param_forces_bypass(self, client, monkeypatch):
        captured = {}

        def fake_fetch(force=False):
            captured["force"] = force
            return {"rows": []}

        monkeypatch.setattr(web.settlement, "fetch_hkex_fsp", fake_fetch)
        client.get("/api/settlement/hkex", params={"refresh": "true"})
        assert captured["force"] is True

    def test_hkex_upstream_failure_surfaces_as_502(self, client, monkeypatch):
        def _raise(force=False):
            raise web.SettlementError("HKEX unreachable")

        monkeypatch.setattr(web.settlement, "fetch_hkex_fsp", _raise)
        resp = client.get("/api/settlement/hkex")
        assert resp.status_code == 502
        assert "HKEX unreachable" in resp.json()["detail"]

    def test_sgx_combines_main_and_flexc(self, client, monkeypatch):
        monkeypatch.setattr(
            web.settlement, "fetch_sgx_fsp",
            lambda force=False: {"asOf": "now", "sourceFileUrl": "https://x/main.xlsx", "rows": [{"contract": "A"}]},
        )
        monkeypatch.setattr(
            web.settlement, "fetch_sgx_flexc",
            lambda force=False: {"asOf": "now", "sourceFileUrl": "https://x/flexc.xlsx", "rows": [{"ticker": "UC010726"}]},
        )
        resp = client.get("/api/settlement/sgx")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == [{"contract": "A"}]
        assert body["flexc"]["rows"] == [{"ticker": "UC010726"}]

    def test_sgx_upstream_failure_surfaces_as_502(self, client, monkeypatch):
        def _raise(force=False):
            raise web.SettlementError("SGX unreachable")

        monkeypatch.setattr(web.settlement, "fetch_sgx_fsp", _raise)
        resp = client.get("/api/settlement/sgx")
        assert resp.status_code == 502

    def test_sgx_daily_happy_path(self, client, monkeypatch):
        monkeypatch.setattr(
            web.sgx_daily, "fetch_sgx_daily",
            lambda trade_date, force=False: {
                "tradeDate": "2026-07-09", "sourceFileUrl": "https://x/FUTURE.zip",
                "rows": [
                    {"ticker": "NK", "contractMonth": "2026-07", "settle": 67650},
                    {"ticker": "NU", "contractMonth": "2026-07", "settle": 100},
                ],
            },
        )
        resp = client.get("/api/settlement/sgx/daily?date=2026-07-09&search=NK")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tradeDate"] == "2026-07-09"
        assert body["rows"] == [{"ticker": "NK", "contractMonth": "2026-07", "settle": 67650}]

    def test_sgx_daily_missing_date_is_422(self, client):
        resp = client.get("/api/settlement/sgx/daily")
        assert resp.status_code == 422

    def test_sgx_daily_invalid_date_is_400(self, client):
        resp = client.get("/api/settlement/sgx/daily?date=not-a-date")
        assert resp.status_code == 400

    def test_sgx_daily_not_available_is_200_with_note_not_an_error(self, client, monkeypatch):
        # A holiday/weekend is expected input, not a gateway failure --
        # must not be a 502 the dashboard would render as a broken fetch.
        monkeypatch.setattr(
            web.sgx_daily, "fetch_sgx_daily",
            lambda trade_date, force=False: (_ for _ in ()).throw(
                web.sgx_daily.SGXDailyNotAvailable("2026-07-12 is a weekend")
            ),
        )
        resp = client.get("/api/settlement/sgx/daily?date=2026-07-12")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == []
        assert body["note"] == "2026-07-12 is a weekend"

    def test_sgx_daily_upstream_failure_surfaces_as_502(self, client, monkeypatch):
        monkeypatch.setattr(
            web.sgx_daily, "fetch_sgx_daily",
            lambda trade_date, force=False: (_ for _ in ()).throw(web.SettlementError("network down")),
        )
        resp = client.get("/api/settlement/sgx/daily?date=2026-07-09")
        assert resp.status_code == 502

    def test_sgx_daily_format_unsupported_is_200_with_note_not_502(self, client, monkeypatch):
        # An older-format date (predates 2018-01-19) is an app parsing
        # limitation, not an upstream/gateway failure -- SGXDailyFormatUnsupported
        # subclasses SettlementError, so it must be caught BEFORE the
        # generic SettlementError->502 clause or it'd wrongly fall there.
        monkeypatch.setattr(
            web.sgx_daily, "fetch_sgx_daily",
            lambda trade_date, force=False: (_ for _ in ()).throw(
                web.sgx_daily.SGXDailyFormatUnsupported(
                    "2016-10-14: uses an older, unsupported column format"
                )
            ),
        )
        resp = client.get("/api/settlement/sgx/daily?date=2016-10-14")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"] == []
        assert "unsupported column format" in body["note"]

    def test_eurex_products_annotates_resolved_flag(self, client, monkeypatch):
        monkeypatch.setattr(
            web.settlement, "fetch_eurex_products",
            lambda: [{"code": "FDAX", "name": "DAX Futures", "group": "INDEX FUTURES", "currency": "EUR"},
                     {"code": "ZZZZ", "name": "Unknown", "group": "X", "currency": "EUR"}],
        )
        monkeypatch.setattr(web.settlement, "resolve_eurex_product_id", lambda code: 34642 if code == "FDAX" else None)
        resp = client.get("/api/settlement/eurex/products")
        assert resp.status_code == 200
        products = {p["code"]: p["resolved"] for p in resp.json()["products"]}
        assert products == {"FDAX": True, "ZZZZ": False}

    def test_eurex_unresolved_product_returns_404_with_hint(self, client, monkeypatch):
        monkeypatch.setattr(web.settlement, "resolve_eurex_product_id", lambda code: None)
        resp = client.get("/api/settlement/eurex", params={"product": "ZZZZ"})
        assert resp.status_code == 404
        assert "resolve" in resp.json()["detail"].lower()

    def test_eurex_resolved_product_fetches_settlement(self, client, monkeypatch):
        monkeypatch.setattr(web.settlement, "resolve_eurex_product_id", lambda code: 34642)
        captured = {}

        def fake_fetch(product_id, busdate=None, force=False):
            captured.update(product_id=product_id, busdate=busdate, force=force)
            return {"productCode": "FDAX", "rows": []}

        monkeypatch.setattr(web.settlement, "fetch_eurex_settlement", fake_fetch)
        resp = client.get("/api/settlement/eurex", params={"product": "fdax", "busdate": "20260715"})
        assert resp.status_code == 200
        assert resp.json()["productCode"] == "FDAX"
        assert captured == {"product_id": 34642, "busdate": "20260715", "force": False}

    def test_eurex_settlement_upstream_failure_surfaces_as_502(self, client, monkeypatch):
        monkeypatch.setattr(web.settlement, "resolve_eurex_product_id", lambda code: 34642)

        def _raise(product_id, busdate=None, force=False):
            raise web.SettlementError("Eurex unreachable")

        monkeypatch.setattr(web.settlement, "fetch_eurex_settlement", _raise)
        resp = client.get("/api/settlement/eurex", params={"product": "FDAX"})
        assert resp.status_code == 502

    def test_eurex_resolve_persists_and_logs(self, client, monkeypatch):
        monkeypatch.setattr(web.settlement, "resolve_eurex_product_id_from_url", lambda code, url: 99887)
        resp = client.post(
            "/api/settlement/eurex/resolve",
            json={"code": "fgbm", "page_url": "https://www.eurex.com/ex-en/markets/x"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": "FGBM", "productId": 99887}

    def test_eurex_resolve_failure_returns_400(self, client, monkeypatch):
        def _raise(code, url):
            raise web.SettlementError("not a Eurex product page")

        monkeypatch.setattr(web.settlement, "resolve_eurex_product_id_from_url", _raise)
        resp = client.post(
            "/api/settlement/eurex/resolve", json={"code": "FGBM", "page_url": "https://example.com"}
        )
        assert resp.status_code == 400

    def test_eurex_msci_includes_default_expiry(self, client, monkeypatch):
        monkeypatch.setattr(
            web.settlement, "fetch_eurex_msci_fsp",
            lambda force=False: {
                "asOf": "now", "sourceFileUrl": "https://x/msci.xlsx",
                "expiries": ["FSP MAR26", "FSP JUN26"],
                "rows": [{"indexName": "MSCI World", "settlementPricesByExpiry": {"FSP MAR26": 100.0}}],
            },
        )
        resp = client.get("/api/settlement/eurex/msci")
        assert resp.status_code == 200
        assert resp.json()["defaultExpiry"] == "FSP MAR26"

    def test_eurex_msci_upstream_failure_surfaces_as_502(self, client, monkeypatch):
        def _raise(force=False):
            raise web.SettlementError("Eurex MSCI page unreachable")

        monkeypatch.setattr(web.settlement, "fetch_eurex_msci_fsp", _raise)
        resp = client.get("/api/settlement/eurex/msci")
        assert resp.status_code == 502


class TestChatFeedback:
    """Covers /api/chat/feedback -- monitor.chat_feedback itself is
    unit-tested in test_chat_feedback.py; the log file is already
    redirected to tmp_path by conftest's autouse isolation fixture."""

    _BODY = {
        "message": "When does 0700 report?",
        "reply": "On 2026-08-12.",
        "note": "wrong date",
        "tool_activity": [{"tool": "get_upcoming_board_meetings", "args": {}, "result": {"count": 1}}],
        "prior_transcript": [{"role": "user", "text": "hi"}],
    }

    def test_post_records_and_returns_updated_stats(self, client):
        resp = client.post("/api/chat/feedback", json=self._BODY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["recorded"] is True
        assert body["count"] == 1

        import monitor.chat_feedback as chat_feedback

        line = chat_feedback.CHAT_FEEDBACK_FILE.read_text(encoding="utf-8").strip()
        import json as _json

        entry = _json.loads(line)
        assert entry["userMessage"] == self._BODY["message"]
        assert entry["toolActivity"] == self._BODY["tool_activity"]

    def test_post_blank_message_is_400(self, client):
        resp = client.post("/api/chat/feedback", json={**self._BODY, "message": "   "})
        assert resp.status_code == 400

    def test_post_missing_reply_is_422(self, client):
        resp = client.post("/api/chat/feedback", json={"message": "q"})
        assert resp.status_code == 422

    def test_stats_endpoint(self, client):
        assert client.get("/api/chat/feedback").json() == {"count": 0, "bytes": 0}
        client.post("/api/chat/feedback", json=self._BODY)
        assert client.get("/api/chat/feedback").json()["count"] == 1

    def test_download_404_when_nothing_flagged(self, client):
        resp = client.get("/api/chat/feedback/download")
        assert resp.status_code == 404

    def test_download_roundtrips_the_log(self, client):
        client.post("/api/chat/feedback", json=self._BODY)
        resp = client.get("/api/chat/feedback/download")
        assert resp.status_code == 200
        assert "chat_feedback.log" in resp.headers.get("content-disposition", "")
        import json as _json

        entry = _json.loads(resp.text.strip())
        assert entry["reply"] == self._BODY["reply"]

    def test_delete_clears_the_log(self, client):
        client.post("/api/chat/feedback", json=self._BODY)
        resp = client.request("DELETE", "/api/chat/feedback")
        assert resp.status_code == 200
        assert resp.json() == {"removed": 1}
        assert client.get("/api/chat/feedback").json() == {"count": 0, "bytes": 0}


class TestBoardMeetings:
    """Covers /api/board-meetings -- monitor.board_meetings' own fetch/parse
    is unit-tested against real-shaped fixtures in test_board_meetings.py;
    these tests only cover the web layer's wiring (params, error mapping)."""

    _ROWS = [
        {"bmDate": "2026-07-17", "stockName": "CHINA PPT INV", "stockCode": "00736",
         "purpose": "FIN RES", "period": "Y.E.31/03/26", "likelyDividend": False},
        {"bmDate": "2026-07-21", "stockName": "FULU HOLDINGS", "stockCode": "02101",
         "purpose": "SPECIAL DIVIDEND", "period": None, "likelyDividend": True},
    ]

    def test_returns_fetch_result(self, client, monkeypatch):
        fake = {"asOf": "now", "generatedDate": "2026-07-16", "sourceUrl": "https://x", "rows": self._ROWS}
        captured = {}

        def fake_fetch(force=False):
            captured["force"] = force
            return fake

        monkeypatch.setattr(web.board_meetings, "fetch_board_meetings", fake_fetch)
        resp = client.get("/api/board-meetings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["generatedDate"] == "2026-07-16"
        assert body["rows"] == self._ROWS
        assert captured["force"] is False

    def test_refresh_param_forces_bypass(self, client, monkeypatch):
        captured = {}

        def fake_fetch(force=False):
            captured["force"] = force
            return {"asOf": "now", "generatedDate": None, "sourceUrl": "https://x", "rows": []}

        monkeypatch.setattr(web.board_meetings, "fetch_board_meetings", fake_fetch)
        client.get("/api/board-meetings", params={"refresh": "true"})
        assert captured["force"] is True

    def test_ticker_param_filters_rows(self, client, monkeypatch):
        monkeypatch.setattr(
            web.board_meetings, "fetch_board_meetings",
            lambda force=False: {"asOf": "now", "generatedDate": "2026-07-16", "sourceUrl": "https://x", "rows": self._ROWS},
        )
        resp = client.get("/api/board-meetings", params={"ticker": "736"})
        assert resp.status_code == 200
        assert [r["stockCode"] for r in resp.json()["rows"]] == ["00736"]

    def test_dividend_only_param_filters_rows(self, client, monkeypatch):
        monkeypatch.setattr(
            web.board_meetings, "fetch_board_meetings",
            lambda force=False: {"asOf": "now", "generatedDate": "2026-07-16", "sourceUrl": "https://x", "rows": self._ROWS},
        )
        resp = client.get("/api/board-meetings", params={"dividend_only": "true"})
        assert resp.status_code == 200
        assert [r["stockCode"] for r in resp.json()["rows"]] == ["02101"]

    def test_date_range_params_filter_rows(self, client, monkeypatch):
        monkeypatch.setattr(
            web.board_meetings, "fetch_board_meetings",
            lambda force=False: {"asOf": "now", "generatedDate": "2026-07-16", "sourceUrl": "https://x", "rows": self._ROWS},
        )
        resp = client.get("/api/board-meetings", params={"date_from": "2026-07-20"})
        assert resp.status_code == 200
        assert [r["stockCode"] for r in resp.json()["rows"]] == ["02101"]

    def test_upstream_failure_surfaces_as_502(self, client, monkeypatch):
        def _raise(force=False):
            raise web.board_meetings.BoardMeetingsError("HKEXnews board meetings page unreachable")

        monkeypatch.setattr(web.board_meetings, "fetch_board_meetings", _raise)
        resp = client.get("/api/board-meetings")
        assert resp.status_code == 502
        assert "unreachable" in resp.json()["detail"]
