import hashlib
import json
from datetime import date

import pytest

import monitor.hkex_search as hkex_search


@pytest.fixture(autouse=True)
def _clear_stock_id_cache():
    """lookup_stock_id caches code->info in-process; without clearing, one
    test's fake response would leak into the next test's lookups."""
    hkex_search._STOCK_ID_CACHE.clear()
    yield
    hkex_search._STOCK_ID_CACHE.clear()


class _FakeTitleSearchResponse:
    """Stands in for the titleSearchServlet HTTP response: `.json()` returns
    the outer payload, whose `result` field is itself a JSON string of the
    record list (mirroring HKEX's actual double-encoded shape)."""

    def __init__(self, records, *, record_cnt=None, has_next=False, result_override=None):
        self._payload = {
            "result": result_override if result_override is not None else json.dumps(records),
            "recordCnt": record_cnt if record_cnt is not None else len(records),
            "loadedRecord": len(records),
            "hasNextRow": has_next,
        }

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestSearchFilingsMarketwide:
    _REC = {
        "DATE_TIME": "13/07/2026 18:04",
        "STOCK_CODE": "01346<br/>",
        "STOCK_NAME": "LEVER STYLE<br/>",
        "TITLE": "Interim Dividend Announcement",
        "FILE_LINK": "/listedco/listconews/sehk/2026/0713/x.pdf",
    }

    def test_uses_all_company_sentinel_and_returns_meta(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["params"] = params
            return _FakeTitleSearchResponse([self._REC], record_cnt=44, has_next=False)

        monkeypatch.setattr(hkex_search.requests, "get", fake_get)
        parsed, meta = hkex_search.search_filings_marketwide(
            date(2026, 7, 7), date(2026, 7, 14), title_keyword="dividend"
        )
        # market-wide search must NOT scope to a stock, and uses the general search type
        assert captured["params"]["stockId"] == "-1"
        assert captured["params"]["searchType"] == "0"
        assert captured["params"]["title"] == "dividend"
        assert len(parsed) == 1
        assert parsed[0]["stockCode"] == "01346"
        assert parsed[0]["filingId"] == hkex_search.filing_id_for(parsed[0])
        assert meta["recordCount"] == 44
        assert meta["hasNextRow"] is False

    def test_null_result_treated_as_empty_not_error(self, monkeypatch):
        # An invalid/empty query makes HKEX return the literal string "null"
        # for `result`; that must parse to zero rows, not blow up.
        monkeypatch.setattr(
            hkex_search.requests,
            "get",
            lambda *a, **k: _FakeTitleSearchResponse([], result_override="null"),
        )
        parsed, meta = hkex_search.search_filings_marketwide(date(2026, 7, 7), date(2026, 7, 14))
        assert parsed == []

    def test_hasnextrow_surfaced_for_truncated_windows(self, monkeypatch):
        monkeypatch.setattr(
            hkex_search.requests,
            "get",
            lambda *a, **k: _FakeTitleSearchResponse([self._REC], record_cnt=880, has_next=True),
        )
        _parsed, meta = hkex_search.search_filings_marketwide(date(2026, 6, 1), date(2026, 7, 1))
        assert meta["hasNextRow"] is True
        assert meta["recordCount"] == 880


class TestSearchFilingsByTickerStillScopes:
    """The market-wide refactor shares a param builder with the per-ticker
    search; guard that per-ticker still resolves and scopes to its stock."""

    def test_passes_resolved_stockid_and_stock_scoped_search_type(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(hkex_search, "lookup_stock_id", lambda t: {"stockId": 12345})

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["params"] = params
            return _FakeTitleSearchResponse([])

        monkeypatch.setattr(hkex_search.requests, "get", fake_get)
        result = hkex_search.search_filings_by_ticker("700", date(2026, 1, 1), date(2026, 12, 31))
        assert captured["params"]["stockId"] == "12345"
        assert captured["params"]["searchType"] == "1"
        assert result == []  # returns a bare list, not the (records, meta) tuple


class TestParseRecord:
    def test_parses_realistic_title_search_record(self):
        raw = {
            "DATE_TIME": "15/08/2026 16:45",
            "STOCK_CODE": "00700<br/>",
            "STOCK_NAME": "TENCENT   HOLDINGS<br/>",
            "TITLE": "Announcements and Notices - Final  Dividend &amp; Closure of Books",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0815/2026081500123.pdf",
        }
        rec = hkex_search._parse_record(raw)
        assert rec["date"] == "15/08/2026"
        assert rec["dateTime"] == "15/08/2026 16:45"
        assert rec["stockCode"] == "00700"
        assert rec["stockName"] == "TENCENT HOLDINGS"  # whitespace squashed
        assert rec["title"] == "Announcements and Notices - Final Dividend & Closure of Books"
        assert rec["link"] == (
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0815/2026081500123.pdf"
        )

    def test_absolute_link_left_untouched(self):
        raw = {
            "DATE_TIME": "01/01/2026 09:00",
            "STOCK_CODE": "00005",
            "STOCK_NAME": "HSBC",
            "TITLE": "Test",
            "FILE_LINK": "https://example.com/already-absolute.pdf",
        }
        rec = hkex_search._parse_record(raw)
        assert rec["link"] == "https://example.com/already-absolute.pdf"

    def test_missing_date_time_yields_empty_date(self):
        raw = {"STOCK_CODE": "00005", "STOCK_NAME": "HSBC", "TITLE": "Test", "FILE_LINK": ""}
        rec = hkex_search._parse_record(raw)
        assert rec["date"] == ""


class TestFilingIdFor:
    def test_matches_documented_md5_recipe(self):
        """filingId MUST stay byte-for-byte compatible with the upstream
        scraper's own hash so rows dedupe cleanly regardless of which path
        ingested them -- see the module docstring."""
        rec = {"stockCode": "00700", "date": "15/08/2026", "title": "Final Dividend"}
        expected = hashlib.md5(b"0070015/08/2026Final Dividend").hexdigest()[:16]
        assert hkex_search.filing_id_for(rec) == expected

    def test_is_16_lowercase_hex_chars(self):
        rec = {"stockCode": "00005", "date": "01/01/2026", "title": "Anything"}
        fid = hkex_search.filing_id_for(rec)
        assert len(fid) == 16
        assert fid == fid.lower()
        int(fid, 16)  # raises ValueError if not valid hex

    def test_changing_any_field_changes_the_hash(self):
        base = {"stockCode": "00700", "date": "15/08/2026", "title": "Final Dividend"}
        base_id = hkex_search.filing_id_for(base)
        assert hkex_search.filing_id_for({**base, "stockCode": "00701"}) != base_id
        assert hkex_search.filing_id_for({**base, "date": "16/08/2026"}) != base_id
        assert hkex_search.filing_id_for({**base, "title": "Interim Dividend"}) != base_id

    def test_missing_title_defaults_to_empty_string(self):
        rec = {"stockCode": "00700", "date": "15/08/2026"}
        expected = hashlib.md5(b"0070015/08/2026").hexdigest()[:16]
        assert hkex_search.filing_id_for(rec) == expected


class TestLookupStockId:
    def test_unwraps_jsonp_and_matches_by_zero_padded_code(self, monkeypatch):
        payload = {
            "stockInfo": [
                {"stockId": 12345, "code": "700", "name": "TENCENT HOLDINGS LIMITED"},
                {"stockId": 99999, "code": "70000", "name": "SOME OTHER CO"},
            ]
        }

        class FakeResponse:
            status_code = 200
            text = f"cb({json.dumps(payload)});"

            def raise_for_status(self):
                pass

        monkeypatch.setattr(hkex_search.requests, "get", lambda *a, **k: FakeResponse())
        info = hkex_search.lookup_stock_id("700")
        assert info["stockId"] == 12345

    def test_second_lookup_is_served_from_cache(self, monkeypatch):
        payload = {"stockInfo": [{"stockId": 12345, "code": "700", "name": "TENCENT"}]}
        calls = []

        class FakeResponse:
            status_code = 200
            text = f"cb({json.dumps(payload)});"

            def raise_for_status(self):
                pass

        def fake_get(*a, **k):
            calls.append(1)
            return FakeResponse()

        monkeypatch.setattr(hkex_search.requests, "get", fake_get)
        hkex_search.lookup_stock_id("700")
        hkex_search.lookup_stock_id("00700")  # same code, different formatting
        assert len(calls) == 1  # second call never hit the network

    def test_failed_lookup_is_not_cached(self, monkeypatch):
        class FakeResponse:
            status_code = 200
            text = 'cb({"stockInfo": []});'

            def raise_for_status(self):
                pass

        monkeypatch.setattr(hkex_search.requests, "get", lambda *a, **k: FakeResponse())
        try:
            hkex_search.lookup_stock_id("700")
        except hkex_search.HKEXSearchError:
            pass
        assert hkex_search._STOCK_ID_CACHE == {}  # a miss must stay retryable

    def test_raises_when_no_matching_stock(self, monkeypatch):
        payload = {"stockInfo": []}

        class FakeResponse:
            status_code = 200
            text = f"cb({json.dumps(payload)});"

            def raise_for_status(self):
                pass

        monkeypatch.setattr(hkex_search.requests, "get", lambda *a, **k: FakeResponse())
        try:
            hkex_search.lookup_stock_id("99999")
            assert False, "expected HKEXSearchError"
        except hkex_search.HKEXSearchError:
            pass


class TestUpsertFilingMetadata:
    def _rec(self, **overrides):
        rec = {
            "date": "14/07/2026",
            "dateTime": "14/07/2026 16:30",
            "stockCode": "00700",
            "stockName": "Tencent",
            "title": "Notice of Board Meeting",
            "link": "https://www1.hkexnews.hk/a.pdf",
        }
        rec.update(overrides)
        rec["filingId"] = hkex_search.filing_id_for(rec)
        return rec

    def test_upserts_valid_record(self, monkeypatch):
        captured = []
        monkeypatch.setattr(hkex_search, "query", lambda sql: captured.append(sql))
        assert hkex_search.upsert_filing_metadata([self._rec()]) == 1
        assert "UPSERT exchange_filing:" in captured[0]
        assert "d'2026-07-14'" in captured[0]

    def test_non_numeric_date_parts_are_refused(self, monkeypatch):
        """Date parts land inside a d'...' literal where _escape_sql_string
        doesn't apply -- anything non-numeric must be dropped, not trusted."""
        captured = []
        monkeypatch.setattr(hkex_search, "query", lambda sql: captured.append(sql))
        bad = self._rec(date="14/07/2026' or malicious")
        assert hkex_search.upsert_filing_metadata([bad]) == 0
        assert captured == []


class _FakeLatestFeedResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestFetchLatestFilings:
    """The JSON feed behind HKEXnews' "Latest Listed Company Information"
    front page -- shapes mirror real captured records."""

    _PAYLOAD = {
        "genDate": 1784010634,
        "maxNumOfFile": 1,
        "newsInfoLst": [
            {
                "newsId": 12247523,
                "lTxt": "Announcements and Notices - [Other - Business Update]",
                "title": "VOLUNTARY ANNOUNCEMENT\nSIGNED MULTIPLE LLM PROJECTS",
                "webPath": "/listedco/listconews/sehk/2026/0717/2026071700075.pdf",
                "stock": [{"sc": "09678", "sn": "UNISOUND"}],
                "relTime": "17/07/2026 08:13",
            },
            {
                "newsId": 12247524,
                "lTxt": "Announcements and Notices",
                "title": "DUAL COUNTER FILING",
                "webPath": "/listedco/listconews/sehk/2026/0717/2026071700076.pdf",
                # One feed item, two stocks -- must expand to one record each,
                # like titleSearchServlet reports the same filing.
                "stock": [{"sc": "00700", "sn": "TENCENT"}, {"sc": "80700", "sn": "TENCENT-R"}],
                "relTime": "17/07/2026 08:10",
            },
            {
                "newsId": 12247525,
                "lTxt": "Announcements and Notices",
                "title": "THIRD ITEM",
                "webPath": "/listedco/listconews/sehk/2026/0717/2026071700077.pdf",
                "stock": [{"sc": "00005", "sn": "HSBC"}],
                "relTime": "17/07/2026 08:00",
            },
        ],
    }

    def test_parses_and_expands_multi_stock_items(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            return _FakeLatestFeedResponse(self._PAYLOAD)

        monkeypatch.setattr(hkex_search.requests, "get", fake_get)
        recs = hkex_search.fetch_latest_filings(limit=20)

        assert "lcisehk1relsde_1.json" in captured["url"]  # today, release-time desc, EN, page 1
        assert len(recs) == 4  # 3 items, one of which expands to 2 stocks
        first = recs[0]
        assert first["stockCode"] == "09678"
        assert first["title"] == "VOLUNTARY ANNOUNCEMENT SIGNED MULTIPLE LLM PROJECTS"  # \n squashed
        assert first["dateTime"] == "17/07/2026 08:13"
        assert first["date"] == "17/07/2026"
        assert first["link"] == "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0717/2026071700075.pdf"
        assert first["category"].startswith("Announcements and Notices")
        assert first["filingId"] == hkex_search.filing_id_for(first)
        dual = [r for r in recs if r["title"] == "DUAL COUNTER FILING"]
        assert {r["stockCode"] for r in dual} == {"00700", "80700"}
        assert dual[0]["filingId"] != dual[1]["filingId"]  # per-stock ids, like titlesearch

    def test_limit_slices_feed_items_before_expansion(self, monkeypatch):
        monkeypatch.setattr(
            hkex_search.requests, "get", lambda url, headers=None, timeout=None: _FakeLatestFeedResponse(self._PAYLOAD)
        )
        recs = hkex_search.fetch_latest_filings(limit=2)
        # 2 feed items taken; the second expands to 2 stocks -> 3 records.
        assert len(recs) == 3

    def test_days_seven_uses_seven_day_feed(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            return _FakeLatestFeedResponse(self._PAYLOAD)

        monkeypatch.setattr(hkex_search.requests, "get", fake_get)
        hkex_search.fetch_latest_filings(limit=5, days=7)
        assert "lcisehk7relsde_1.json" in captured["url"]

    def test_unexpected_shape_raises(self, monkeypatch):
        monkeypatch.setattr(
            hkex_search.requests, "get",
            lambda url, headers=None, timeout=None: _FakeLatestFeedResponse({"SEHK Page": 123}),
        )
        try:
            hkex_search.fetch_latest_filings()
            raise AssertionError("should have raised")
        except hkex_search.HKEXSearchError as exc:
            assert "newsInfoLst" in str(exc)
