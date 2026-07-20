import io
import zipfile
from datetime import date, timedelta

import pytest

import monitor.settlement as settlement
import monitor.sgx_daily as sgx_daily


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Isolate the persisted key<->date map (never touch the real data/
    directory) and the shared settlement._CACHE (never leak a fetch result
    -- or a monkeypatch's absence -- between tests)."""
    monkeypatch.setattr(sgx_daily, "SGX_DAILY_KEYS_FILE", tmp_path / "sgx_daily_keys.json")
    settlement._CACHE.clear()
    yield
    settlement._CACHE.clear()


class _FakeResponse:
    def __init__(self, *, json_data=None, status_code=200, content=b""):
        self._json = json_data
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def _zip_csv(text: str, inner_name: str = "0716FUT.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, text)
    return buf.getvalue()


# Mirrors a real SGX daily futures file (verified live): comma-delimited,
# one row per contract month, SETTLE populated even for untraded months.
_MODERN_CSV = (
    "DATE,COM,COM_MM,COM_YY,OPEN,HIGH,LOW,CLOSE,SETTLE,VOLUME,OINT,SERIES\r\n"
    "20260716,NK   ,09,2026,68850,68865,66585,67005,67005,27189,49260,NKU26\r\n"
    "20260716,1MF  ,07,2026,,,,,486.26,0,0,1MFN26\r\n"
)


class TestWeekdaysBetween:
    def test_same_day_is_zero(self):
        assert sgx_daily._weekdays_between(date(2026, 1, 5), date(2026, 1, 5)) == 0

    def test_next_weekday_is_one(self):
        assert sgx_daily._weekdays_between(date(2026, 1, 5), date(2026, 1, 6)) == 1  # Mon -> Tue

    def test_reversed_direction_is_negative(self):
        assert sgx_daily._weekdays_between(date(2026, 1, 6), date(2026, 1, 5)) == -1

    def test_spans_multiple_weekdays(self):
        assert sgx_daily._weekdays_between(date(2026, 1, 5), date(2026, 1, 8)) == 3  # Mon -> Thu

    def test_skips_weekend(self):
        assert sgx_daily._weekdays_between(date(2026, 1, 9), date(2026, 1, 12)) == 1  # Fri -> Mon


class TestParseListFeedItems:
    def test_parses_valid_items(self):
        payload = {"items": [{"key": "7556", "Trade Date": "16 Jul 2026"}]}
        assert sgx_daily._parse_list_feed_items(payload) == {date(2026, 7, 16): 7556}

    def test_skips_malformed_items(self):
        payload = {"items": [{"key": "x", "Trade Date": "bad"}, {}, {"key": "1"}]}
        assert sgx_daily._parse_list_feed_items(payload) == {}

    def test_missing_items_key_returns_empty(self):
        assert sgx_daily._parse_list_feed_items({}) == {}


class TestFetchListFeed:
    def test_parses_and_merges_into_persisted_map(self, monkeypatch):
        payload = {"items": [{"key": "7556", "Trade Date": "16 Jul 2026"}]}
        monkeypatch.setattr(sgx_daily.requests, "get", lambda *a, **k: _FakeResponse(json_data=payload))
        result = sgx_daily._fetch_list_feed_impl()
        assert result == {date(2026, 7, 16): 7556}
        assert sgx_daily._load_key_map() == {"2026-07-16": 7556}

    def test_network_failure_raises_settlement_error(self, monkeypatch):
        import requests as real_requests

        def _raise(*a, **k):
            raise real_requests.RequestException("timeout")

        monkeypatch.setattr(sgx_daily.requests, "get", _raise)
        with pytest.raises(settlement.SettlementError):
            sgx_daily._fetch_list_feed_impl()

    def test_cached_across_calls(self, monkeypatch):
        calls = []

        def fake_get(*a, **k):
            calls.append(1)
            return _FakeResponse(json_data={"items": []})

        monkeypatch.setattr(sgx_daily.requests, "get", fake_get)
        sgx_daily._fetch_list_feed()
        sgx_daily._fetch_list_feed()
        assert len(calls) == 1

    def test_empty_items_list_stays_a_valid_empty_result(self, monkeypatch):
        monkeypatch.setattr(sgx_daily.requests, "get", lambda *a, **k: _FakeResponse(json_data={"items": []}))
        assert sgx_daily._fetch_list_feed_impl() == {}

    def test_nonempty_items_all_malformed_raises(self, monkeypatch):
        # ~60 consecutive business days all failing to parse at once means
        # the feed's own field names changed shape, not that they're all
        # individually malformed -- must surface as a fetch/parse problem,
        # not silently cache an empty result that reads as "no recent files."
        payload = {"items": [{"unexpected_key": "x"}, {"unexpected_key": "y"}]}
        monkeypatch.setattr(sgx_daily.requests, "get", lambda *a, **k: _FakeResponse(json_data=payload))
        with pytest.raises(settlement.SettlementError, match="none were parseable"):
            sgx_daily._fetch_list_feed_impl()


class TestDownloadKeyFile:
    def test_success_returns_content(self, monkeypatch):
        monkeypatch.setattr(sgx_daily.requests, "get", lambda *a, **k: _FakeResponse(content=b"zip bytes"))
        assert sgx_daily._download_key_file("https://example/1/FUTURE.zip") == b"zip bytes"

    def test_404_maps_to_no_file_at_key(self, monkeypatch):
        # Belt-and-braces: live testing found SGX serves 200+HTML rather
        # than a 404 for a key with no real file (see TestFetchKeyImpl's
        # bad-zip test, the actually-observed shape), but a genuine 404
        # should be treated the same way if SGX ever changes that.
        monkeypatch.setattr(sgx_daily.requests, "get", lambda *a, **k: _FakeResponse(status_code=404))
        with pytest.raises(sgx_daily.SGXDailyNoFileAtKey):
            sgx_daily._download_key_file("https://example/999999/FUTURE.zip")

    def test_other_http_error_raises_plain_settlement_error(self, monkeypatch):
        monkeypatch.setattr(sgx_daily.requests, "get", lambda *a, **k: _FakeResponse(status_code=503))
        with pytest.raises(settlement.SettlementError) as exc_info:
            sgx_daily._download_key_file("https://example/1/FUTURE.zip")
        assert not issubclass(exc_info.type, sgx_daily.SGXDailyNoFileAtKey)

    def test_network_exception_raises_plain_settlement_error(self, monkeypatch):
        import requests as real_requests

        def _raise(*a, **k):
            raise real_requests.RequestException("timeout")

        monkeypatch.setattr(sgx_daily.requests, "get", _raise)
        with pytest.raises(settlement.SettlementError) as exc_info:
            sgx_daily._download_key_file("https://example/1/FUTURE.zip")
        assert not issubclass(exc_info.type, sgx_daily.SGXDailyNoFileAtKey)


class TestFetchKeyImpl:
    def test_parses_modern_format(self, monkeypatch):
        monkeypatch.setattr(sgx_daily, "_download_key_file", lambda url, timeout=60.0: _zip_csv(_MODERN_CSV))
        result = sgx_daily._fetch_key_impl(9999)
        assert result["tradeDate"] == date(2026, 7, 16)
        assert result["sourceFileUrl"] == f"{sgx_daily.SGX_DAILY_BASE_URL}/9999/FUTURE.zip"
        assert result["rows"][0] == {
            "ticker": "NK",
            "contractMonth": "2026-09",
            "open": 68850.0,
            "high": 68865.0,
            "low": 66585.0,
            "close": 67005.0,
            "settle": 67005.0,
            "volume": 27189.0,
            "openInterest": 49260.0,
            "series": "NKU26",
        }

    def test_empty_ohlc_becomes_none_but_settle_kept(self, monkeypatch):
        # Real SGX quirk: an untraded contract month still gets a SETTLE
        # mark (like Eurex's D. Settle) even with no OHLC/volume that day.
        monkeypatch.setattr(sgx_daily, "_download_key_file", lambda url, timeout=60.0: _zip_csv(_MODERN_CSV))
        untraded = sgx_daily._fetch_key_impl(9999)["rows"][1]
        assert untraded["ticker"] == "1MF"
        assert untraded["open"] is None
        assert untraded["high"] is None
        assert untraded["settle"] == 486.26

    def test_older_format_raises_the_specific_format_unsupported_error(self, monkeypatch):
        # Pre-~2016 files use a different tab-delimited column layout
        # entirely -- must be rejected explicitly, not silently misparsed,
        # and as the SPECIFIC SGXDailyFormatUnsupported type (a real date,
        # just an unparsed one) -- not lumped in with a generic failure,
        # since resolve_daily_key's verify loop treats the two very
        # differently (see TestResolveDailyKey).
        old_csv = "DATE\tCOM\tCOM_MM\tCOM_YY\tOPEN_1\tOPEN_I1\r\n20130405\tAP\t\t\t000871.89\t \r\n"
        monkeypatch.setattr(sgx_daily, "_download_key_file", lambda url, timeout=60.0: _zip_csv(old_csv, "0405Fut.csv"))
        with pytest.raises(sgx_daily.SGXDailyFormatUnsupported, match="unsupported"):
            sgx_daily._fetch_key_impl(1)

    def test_older_format_error_names_the_recovered_trade_date(self, monkeypatch):
        # DATE is always the first column regardless of delimiter -- even
        # in the unsupported branch, the error should name which date this
        # actually was (best-effort), not just the opaque key.
        old_csv = "DATE\tCOM\tCOM_MM\tCOM_YY\tOPEN_1\tOPEN_I1\r\n20130405\tAP\t\t\t000871.89\t \r\n"
        monkeypatch.setattr(sgx_daily, "_download_key_file", lambda url, timeout=60.0: _zip_csv(old_csv, "0405Fut.csv"))
        with pytest.raises(sgx_daily.SGXDailyFormatUnsupported, match="2013-04-05"):
            sgx_daily._fetch_key_impl(1)

    def test_bad_zip_raises_no_file_at_key(self, monkeypatch):
        # Confirmed live: SGX serves a 200-status HTML error page (not a
        # 404) for a key with no real file, which fails zip parsing here --
        # this is the SPECIFIC SGXDailyNoFileAtKey type (feeds resolve_
        # daily_key's bracket-and-conclude-NotAvailable logic), distinct
        # from a genuine download/network failure (see TestDownloadKeyFile).
        monkeypatch.setattr(sgx_daily, "_download_key_file", lambda url, timeout=60.0: b"not a zip file")
        with pytest.raises(sgx_daily.SGXDailyNoFileAtKey, match="No real SGX"):
            sgx_daily._fetch_key_impl(1)

    def test_no_data_rows_raises(self, monkeypatch):
        header_only = "DATE,COM,COM_MM,COM_YY,OPEN,HIGH,LOW,CLOSE,SETTLE,VOLUME,OINT,SERIES\r\n"
        monkeypatch.setattr(sgx_daily, "_download_key_file", lambda url, timeout=60.0: _zip_csv(header_only))
        with pytest.raises(settlement.SettlementError, match="no data rows"):
            sgx_daily._fetch_key_impl(1)


class TestFetchByKeyCaching:
    def test_caches_and_force_bypasses(self, monkeypatch):
        calls = []

        def fake_download(url, timeout=60.0):
            calls.append(url)
            return _zip_csv(_MODERN_CSV)

        monkeypatch.setattr(sgx_daily, "_download_key_file", fake_download)
        sgx_daily._fetch_by_key(555)
        sgx_daily._fetch_by_key(555)  # cached -- must not re-download
        assert len(calls) == 1
        sgx_daily._fetch_by_key(555, force=True)
        assert len(calls) == 2


class TestFilterDailyRows:
    _ROWS = [
        {"ticker": "NK", "contractMonth": "2026-07", "settle": 67650},
        {"ticker": "NK", "contractMonth": "2026-09", "settle": 67900},
        {"ticker": "NU", "contractMonth": "2026-07", "settle": 100},
    ]

    def test_filters_by_ticker_case_insensitive(self):
        assert len(sgx_daily.filter_daily_rows(self._ROWS, ticker="nk")) == 2

    def test_compound_ticker_input_still_matches_bare_rows(self):
        # Regression pattern established for settlement_history: a caller
        # (e.g. the chat model) may reuse a compound ticker exactly as SGX
        # prints it ("NK/NKO") even though rows are stored under the bare
        # component "NK".
        assert len(sgx_daily.filter_daily_rows(self._ROWS, ticker="NK/NKO")) == 2

    def test_filters_by_contract_month(self):
        out = sgx_daily.filter_daily_rows(self._ROWS, ticker="nk", contract_month="2026-07")
        assert out == [self._ROWS[0]]

    def test_contract_month_unpadded_still_matches(self):
        out = sgx_daily.filter_daily_rows(self._ROWS, ticker="nk", contract_month="2026-7")
        assert out == [self._ROWS[0]]

    def test_no_filters_returns_everything(self):
        assert sgx_daily.filter_daily_rows(self._ROWS) == self._ROWS


class TestKeyMapPersistence:
    def test_merge_persists_new_dates(self):
        sgx_daily._merge_into_key_map({date(2026, 7, 16): 7556})
        assert sgx_daily._load_key_map() == {"2026-07-16": 7556}

    def test_merge_accumulates_without_dropping_existing(self):
        sgx_daily._merge_into_key_map({date(2026, 7, 16): 7556})
        sgx_daily._merge_into_key_map({date(2026, 7, 17): 7557})
        assert sgx_daily._load_key_map() == {"2026-07-16": 7556, "2026-07-17": 7557}


class TestNearestAnchor:
    def test_picks_closest_from_combined_sources(self):
        key_map = {"2026-01-05": 100}
        recent = {date(2026, 7, 16): 7556}
        anchor_date, anchor_key = sgx_daily._nearest_anchor(date(2026, 7, 10), key_map, recent)
        assert (anchor_date, anchor_key) == (date(2026, 7, 16), 7556)

    def test_returns_none_when_nothing_known(self):
        assert sgx_daily._nearest_anchor(date(2026, 7, 10), {}, {}) == (None, None)


class TestResolveDailyKey:
    def test_predates_archive_rejected_immediately_no_network(self):
        # No network mocks set up at all here -- if this reached out to
        # SGX, the test would hang or fail on a real request, which is
        # itself proof the early return happens before any I/O. The floor
        # is EARLIEST_SUPPORTED_TRADE_DATE (2018-01-19, the modern-format
        # boundary this app actually parses), not SGX's own older archive
        # floor (2013-04-05) -- match the real enforced boundary.
        with pytest.raises(sgx_daily.SGXDailyNotAvailable, match="2018-01-19"):
            sgx_daily.resolve_daily_key(date(2010, 1, 1))

    def test_returns_persisted_key_without_any_fetch(self, monkeypatch):
        sgx_daily._merge_into_key_map({date(2026, 7, 16): 7556})

        def _boom(*a, **k):
            raise AssertionError("should not fetch -- key already known")

        monkeypatch.setattr(sgx_daily, "_fetch_list_feed", _boom)
        monkeypatch.setattr(sgx_daily, "_peek_key_date", _boom)
        assert sgx_daily.resolve_daily_key(date(2026, 7, 16)) == 7556

    def test_no_anchor_and_no_network_raises_settlement_error(self, monkeypatch):
        monkeypatch.setattr(
            sgx_daily, "_fetch_list_feed",
            lambda force=False: (_ for _ in ()).throw(settlement.SettlementError("offline")),
        )
        with pytest.raises(settlement.SettlementError, match="No known SGX daily-archive date"):
            sgx_daily.resolve_daily_key(date(2020, 2, 1))

    def test_estimate_off_by_one_is_corrected_by_verify_loop(self, monkeypatch):
        # A tiny fake archive with a deliberate 1-key gap injected after
        # 2026-01-09, simulating SGX's occasional weekend-shift drift --
        # naive weekday arithmetic from the Jan-5 anchor undershoots every
        # date from Jan-12 onward by exactly 1.
        archive: dict[int, date] = {}
        key = 100
        d = date(2026, 1, 5)
        while d < date(2026, 2, 1):
            if d.weekday() < 5:
                archive[key] = d
                key += 1
            if d == date(2026, 1, 9):
                key += 1  # inject the gap
            d += timedelta(days=1)

        monkeypatch.setattr(sgx_daily, "_fetch_list_feed", lambda force=False: {})
        sgx_daily._merge_into_key_map({date(2026, 1, 5): 100})
        monkeypatch.setattr(
            sgx_daily, "_peek_key_date",
            lambda k: archive[k] if k in archive else (_ for _ in ()).throw(settlement.SettlementError("no file")),
        )

        target = date(2026, 1, 20)
        expected_key = next(k for k, v in archive.items() if v == target)
        resolved = sgx_daily.resolve_daily_key(target)
        assert resolved == expected_key
        # Prove the correction actually did something: the uncorrected
        # naive estimate would have landed on a different (wrong) key.
        naive_estimate = 100 + sgx_daily._weekdays_between(date(2026, 1, 5), target)
        assert resolved != naive_estimate

        # Every date walked through along the way should now be persisted.
        persisted = sgx_daily._load_key_map()
        assert persisted[target.isoformat()] == expected_key

    def test_weekend_date_raises_sgx_daily_not_available(self, monkeypatch):
        monkeypatch.setattr(sgx_daily, "_fetch_list_feed", lambda force=False: {})
        sgx_daily._merge_into_key_map({date(2026, 1, 5): 100})
        monkeypatch.setattr(
            sgx_daily, "_peek_key_date",
            lambda k: (_ for _ in ()).throw(sgx_daily.SGXDailyNoFileAtKey("no file at key")),
        )
        with pytest.raises(sgx_daily.SGXDailyNotAvailable):
            sgx_daily.resolve_daily_key(date(2026, 1, 10))  # a Saturday

    def test_probe_network_failure_is_not_misreported_as_a_calendar_fact(self, monkeypatch):
        # The audit's finding: a genuine network/fetch problem while
        # verifying a key (timeout, DNS, 5xx) must NOT collapse into the
        # same "likely a weekend/holiday" message a confirmed absence gets
        # -- that would misrepresent an outage as a fact about SGX's
        # trading calendar. Distinct from SGXDailyNoFileAtKey (see the
        # weekend test above): a bare SettlementError here is the "the
        # request itself failed" case.
        monkeypatch.setattr(sgx_daily, "_fetch_list_feed", lambda force=False: {})
        sgx_daily._merge_into_key_map({date(2026, 1, 5): 100})
        monkeypatch.setattr(
            sgx_daily, "_peek_key_date",
            lambda k: (_ for _ in ()).throw(settlement.SettlementError("connection reset")),
        )
        with pytest.raises(settlement.SettlementError, match="could not verify") as exc_info:
            sgx_daily.resolve_daily_key(date(2026, 1, 8))  # an ordinary trading Thursday
        assert not issubclass(exc_info.type, sgx_daily.SGXDailyNotAvailable)

    def test_unsupported_format_is_not_misreported_as_not_available(self, monkeypatch):
        # Regression (caught by a live smoke test, not a mock): a date
        # whose resolved key lands in the old-format era used to be
        # swallowed by the verify loop's blanket `except SettlementError:
        # break`, misreporting a REAL date as if it simply had no file at
        # all. SGXDailyFormatUnsupported must propagate distinctly instead.
        # Anchor/target both after EARLIEST_SUPPORTED_TRADE_DATE (2018-01-19)
        # so the floor check doesn't short-circuit before reaching the
        # verify loop this test actually exercises; kept close together
        # (unlike other tests here, whose 2026 anchor is realistic for a
        # recent-date lookup) so the estimated key stays positive and
        # actually reaches the mock below instead of being short-circuited
        # by the `key < 1` guard.
        monkeypatch.setattr(sgx_daily, "_fetch_list_feed", lambda force=False: {})
        sgx_daily._merge_into_key_map({date(2018, 6, 10): 5500})
        monkeypatch.setattr(
            sgx_daily, "_peek_key_date",
            lambda k: (_ for _ in ()).throw(sgx_daily.SGXDailyFormatUnsupported("old format at key")),
        )
        with pytest.raises(sgx_daily.SGXDailyFormatUnsupported):
            sgx_daily.resolve_daily_key(date(2018, 6, 14))

    def test_format_unsupported_error_names_the_requested_date(self, monkeypatch):
        # resolve_daily_key re-raises with the REQUESTED date prefixed --
        # _fetch_key_impl's own message only names the (possibly different)
        # probed key's recovered date, which alone could read as if the
        # date the user actually asked about was never even considered.
        monkeypatch.setattr(sgx_daily, "_fetch_list_feed", lambda force=False: {})
        sgx_daily._merge_into_key_map({date(2018, 6, 10): 5500})
        monkeypatch.setattr(
            sgx_daily, "_peek_key_date",
            lambda k: (_ for _ in ()).throw(
                sgx_daily.SGXDailyFormatUnsupported("key 5501 (trade date 2018-06-11) uses an older format")
            ),
        )
        with pytest.raises(sgx_daily.SGXDailyFormatUnsupported, match="2018-06-14"):
            sgx_daily.resolve_daily_key(date(2018, 6, 14))


class TestFetchSgxDaily:
    def test_resolves_key_and_returns_shaped_result(self, monkeypatch):
        sgx_daily._merge_into_key_map({date(2026, 7, 16): 9999})
        monkeypatch.setattr(sgx_daily, "_download_key_file", lambda url, timeout=60.0: _zip_csv(_MODERN_CSV))
        result = sgx_daily.fetch_sgx_daily(date(2026, 7, 16))
        assert result["tradeDate"] == "2026-07-16"
        assert result["sourceFileUrl"] == f"{sgx_daily.SGX_DAILY_BASE_URL}/9999/FUTURE.zip"
        assert len(result["rows"]) == 2

    def test_not_a_trading_day_raises_sgx_daily_not_available(self, monkeypatch):
        monkeypatch.setattr(sgx_daily, "_fetch_list_feed", lambda force=False: {})
        sgx_daily._merge_into_key_map({date(2026, 1, 5): 100})
        monkeypatch.setattr(
            sgx_daily, "_peek_key_date",
            lambda k: (_ for _ in ()).throw(sgx_daily.SGXDailyNoFileAtKey("no file")),
        )
        with pytest.raises(sgx_daily.SGXDailyNotAvailable):
            sgx_daily.fetch_sgx_daily(date(2026, 1, 10))
