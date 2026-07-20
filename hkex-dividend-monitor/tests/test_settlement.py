import json

import openpyxl
import pytest

import monitor.settlement as settlement


@pytest.fixture(autouse=True)
def _clear_settlement_cache():
    """The module-level _CACHE would otherwise leak a fetch result from one
    test into the next (or mask a monkeypatched fetch entirely)."""
    settlement._CACHE.clear()
    yield
    settlement._CACHE.clear()


class _FakeResponse:
    def __init__(self, *, json_data=None, text="", status_code=200, content=b""):
        self._json = json_data
        self.text = text
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


def _xlsx_bytes(sheets: dict) -> bytes:
    """Build a minimal in-memory workbook: {sheet_name: [[row], [row], ...]}."""
    from io import BytesIO

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestCoerceNumeric:
    def test_passes_through_int_and_float(self):
        assert settlement._coerce_numeric(5) == 5
        assert settlement._coerce_numeric(5.5) == 5.5

    def test_parses_numeric_string(self):
        assert settlement._coerce_numeric("270") == 270.0

    def test_parses_comma_formatted_string(self):
        assert settlement._coerce_numeric("24,999.53") == 24999.53

    def test_leaves_non_numeric_string_unchanged(self):
        assert settlement._coerce_numeric("N/A") == "N/A"

    def test_leaves_none_unchanged(self):
        assert settlement._coerce_numeric(None) is None

    def test_rejects_nan_and_inf_strings(self):
        # float("nan")/float("inf") both succeed in plain Python -- but a
        # settlement price of NaN/Infinity would serialize as invalid JSON
        # and is never a genuine value; these must pass through as strings.
        assert settlement._coerce_numeric("nan") == "nan"
        assert settlement._coerce_numeric("inf") == "inf"
        assert settlement._coerce_numeric("-inf") == "-inf"

    def test_rejects_european_decimal_comma_format(self):
        # Naive comma-stripping would silently turn this into 1.23456 --
        # off by a factor of 1000. Must pass through unchanged, not mangled.
        assert settlement._coerce_numeric("1.234,56") == "1.234,56"

    def test_parses_negative_comma_formatted_string(self):
        assert settlement._coerce_numeric("-1,234.50") == -1234.50


# ============================================================
# HKEX
# ============================================================

_HKEX_TABLE_ID = "D3EAB5EFE4C14F4CB96457F424B1A8BA"
_HKEX_SORT_COL = "71AD4C35FDE4499A81BB93CE0A09DBE1"
_HKEX_CONTRACT_GUID = "24D3E0254E00418993FFAB262FBD1CCE"
_HKEX_HKATS_GUID = "CB5FAD2622274CAE8459B54CC492DFFE"
_HKEX_LTD_GUID = "8C7F068F88C94F4EA1F4BB260A1F8D61"
_HKEX_PRODUCT_TYPE_GUID = "2D8C69A4DBF7457F869B355A702BCDB0"
_HKEX_FSP_GUID = "96BA0920218F4380A377355F93D41256"
_HKEX_PUBLISH_DATE_GUID = "0E7909172A7F422EA1584D56B3993C5C"

_HKEX_SAMPLE_PAGE_HTML = (
    f'<table data-table-id="{_HKEX_TABLE_ID}" data-sort-col="{_HKEX_SORT_COL}" data-sort-dir="1">'
    "</table>"
)

_HKEX_SAMPLE_PAYLOAD = {
    "genDate": 1784010634,
    "maxNumOfFile": 1,
    "searchOptions": {
        _HKEX_CONTRACT_GUID: {"filterOption": "Contract"},
        _HKEX_HKATS_GUID: {"filterOption": "HKATS Code"},
        _HKEX_LTD_GUID: {"filterOption": "Last Trading Date / Expiry Date"},
    },
    "tableInfo": [
        {
            _HKEX_PRODUCT_TYPE_GUID: "<p>Commodities</p>",
            _HKEX_CONTRACT_GUID: "<p>CNH London Zinc Mini Futures</p>",
            _HKEX_HKATS_GUID: "<p>LRZ</p>",
            _HKEX_LTD_GUID: "<p>13-Jul-2026</p>",
            _HKEX_FSP_GUID: "<p>24234</p>",
            _HKEX_PUBLISH_DATE_GUID: "<p>14-Jul-2026</p>",
            _HKEX_SORT_COL: "<p>26071460016</p>",
            "YearMonth": "<p>Jul-26</p>",
            "RowKey": "e52b687d-8c67-4dc7-86b5-e86f07d8965a",
        },
        {
            _HKEX_PRODUCT_TYPE_GUID: "<p>Equity Index</p>",
            _HKEX_CONTRACT_GUID: "<p>Hang Seng Index Futures</p>",
            _HKEX_HKATS_GUID: "<p>HSI</p>",
            _HKEX_LTD_GUID: "<p>30-Jul-2026</p>",
            _HKEX_FSP_GUID: "<p>24,999.53</p>",
            _HKEX_PUBLISH_DATE_GUID: "<p>30-Jul-2026</p>",
            _HKEX_SORT_COL: "<p>26071460017</p>",
            "YearMonth": "<p>Jul-26</p>",
            "RowKey": "aaaa",
        },
    ],
}


class TestHkexDiscovery:
    def test_extracts_table_ids_from_page_html(self, monkeypatch):
        monkeypatch.setattr(
            settlement.requests, "get", lambda *a, **k: _FakeResponse(text=_HKEX_SAMPLE_PAGE_HTML)
        )
        table_id, sort_col = settlement._discover_hkex_table_ids()
        assert table_id == _HKEX_TABLE_ID
        assert sort_col == _HKEX_SORT_COL

    def test_falls_back_on_request_failure(self, monkeypatch):
        import requests

        def _raise(*a, **k):
            raise requests.RequestException("boom")

        monkeypatch.setattr(settlement.requests, "get", _raise)
        table_id, sort_col = settlement._discover_hkex_table_ids()
        assert table_id == settlement._HKEX_FALLBACK_TABLE_ID
        assert sort_col == settlement._HKEX_FALLBACK_SORT_COL

    def test_falls_back_when_table_tag_not_found(self, monkeypatch):
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(text="<html></html>"))
        table_id, sort_col = settlement._discover_hkex_table_ids()
        assert table_id == settlement._HKEX_FALLBACK_TABLE_ID
        assert sort_col == settlement._HKEX_FALLBACK_SORT_COL


class TestHkexColumnMapping:
    def test_maps_search_options_and_sniffs_remaining_columns(self):
        mapping = settlement._map_hkex_columns(_HKEX_SAMPLE_PAYLOAD, _HKEX_SORT_COL)
        assert mapping[_HKEX_CONTRACT_GUID] == "contract"
        assert mapping[_HKEX_HKATS_GUID] == "hkatsCode"
        assert mapping[_HKEX_LTD_GUID] == "lastTradingDate"
        assert mapping[_HKEX_SORT_COL] == "sortKey"
        assert mapping[_HKEX_PRODUCT_TYPE_GUID] == "productType"
        assert mapping[_HKEX_FSP_GUID] == "fsp"
        assert mapping[_HKEX_PUBLISH_DATE_GUID] == "publishDate"

    def test_sort_key_column_excluded_from_normalized_row(self):
        mapping = settlement._map_hkex_columns(_HKEX_SAMPLE_PAYLOAD, _HKEX_SORT_COL)
        row = settlement._normalize_hkex_row(_HKEX_SAMPLE_PAYLOAD["tableInfo"][0], mapping)
        assert "sortKey" not in row
        assert set(row) >= {"contract", "hkatsCode", "lastTradingDate", "productType", "fsp", "publishDate"}

    def test_odd_first_row_cell_does_not_mislabel_the_whole_column(self):
        """Regression: column kinds used to be sniffed from rows[0] alone,
        so a single "N/A" settlement price in the first row relabelled the
        whole FSP column as productType -- every row's price then came out
        under the wrong field. Majority vote across a sample must shrug
        off the one odd cell."""
        import copy

        payload = copy.deepcopy(_HKEX_SAMPLE_PAYLOAD)
        payload["tableInfo"][0][_HKEX_FSP_GUID] = "<p>N/A</p>"
        mapping = settlement._map_hkex_columns(payload, _HKEX_SORT_COL)
        assert mapping[_HKEX_FSP_GUID] == "fsp"  # row 2's numeric value out-votes row 1's N/A

    def test_blank_first_row_cell_does_not_mislabel_the_whole_column(self):
        import copy

        payload = copy.deepcopy(_HKEX_SAMPLE_PAYLOAD)
        payload["tableInfo"][0][_HKEX_PUBLISH_DATE_GUID] = "<p></p>"
        mapping = settlement._map_hkex_columns(payload, _HKEX_SORT_COL)
        assert mapping[_HKEX_PUBLISH_DATE_GUID] == "publishDate"  # blanks carry no vote

    def test_entirely_empty_column_defaults_to_product_type(self):
        # Must NOT default to publishDate/fsp: a second column mapping to
        # the same field name would overwrite the real column's values in
        # _normalize_hkex_row.
        rows = [{"GUIDX": "<p></p>"}, {"GUIDX": ""}]
        assert settlement._sniff_column_kind(rows, "GUIDX") == "productType"


class TestHkexRowNormalization:
    def test_strips_tags_and_normalizes_dates(self):
        mapping = settlement._map_hkex_columns(_HKEX_SAMPLE_PAYLOAD, _HKEX_SORT_COL)
        row = settlement._normalize_hkex_row(_HKEX_SAMPLE_PAYLOAD["tableInfo"][0], mapping)
        assert row["contract"] == "CNH London Zinc Mini Futures"
        assert row["fsp"] == 24234.0
        assert row["publishDateIso"] == "2026-07-14"
        assert row["lastTradingDateIso"] == "2026-07-13"

    def test_comma_formatted_number_still_detected_as_fsp(self):
        # Row 2's FSP is "24,999.53" -- comma thousands separator must not
        # get misclassified as a non-numeric productType column, and must
        # still be coerced to a float (not left as a comma-bearing string).
        mapping = settlement._map_hkex_columns(_HKEX_SAMPLE_PAYLOAD, _HKEX_SORT_COL)
        assert mapping[_HKEX_FSP_GUID] == "fsp"
        row = settlement._normalize_hkex_row(_HKEX_SAMPLE_PAYLOAD["tableInfo"][1], mapping)
        assert row["fsp"] == 24999.53


class TestParseHkexGenDate:
    def test_parses_epoch_seconds(self):
        # 2026-07-14T06:30:34+00:00 == 2026-07-14T14:30:34+08:00 HKT
        result = settlement._parse_hkex_gen_date(1784010634)
        assert result is not None and result.startswith("2026-07-14T14:30:34")

    def test_treats_implausibly_large_value_as_milliseconds(self):
        seconds_result = settlement._parse_hkex_gen_date(1784010634)
        millis_result = settlement._parse_hkex_gen_date(1784010634000)
        assert millis_result == seconds_result

    def test_non_numeric_value_returns_none(self):
        assert settlement._parse_hkex_gen_date("not a timestamp") is None

    def test_none_value_returns_none(self):
        assert settlement._parse_hkex_gen_date(None) is None

    def test_zero_or_negative_returns_none(self):
        assert settlement._parse_hkex_gen_date(0) is None
        assert settlement._parse_hkex_gen_date(-1) is None


class TestFetchHkexFsp:
    def test_end_to_end_fetch_produces_distinct_lists(self, monkeypatch):
        monkeypatch.setattr(settlement, "_discover_hkex_table_ids", lambda: (_HKEX_TABLE_ID, _HKEX_SORT_COL))
        monkeypatch.setattr(settlement, "_fetch_hkex_json", lambda t, s: _HKEX_SAMPLE_PAYLOAD)
        result = settlement.fetch_hkex_fsp()
        assert len(result["rows"]) == 2
        assert result["contracts"] == ["CNH London Zinc Mini Futures", "Hang Seng Index Futures"]
        assert result["productTypes"] == ["Commodities", "Equity Index"]

    def test_result_is_cached_until_force(self, monkeypatch):
        calls = []
        monkeypatch.setattr(settlement, "_discover_hkex_table_ids", lambda: (_HKEX_TABLE_ID, _HKEX_SORT_COL))

        def fake_fetch(t, s):
            calls.append(1)
            return _HKEX_SAMPLE_PAYLOAD

        monkeypatch.setattr(settlement, "_fetch_hkex_json", fake_fetch)
        settlement.fetch_hkex_fsp()
        settlement.fetch_hkex_fsp()
        assert len(calls) == 1
        settlement.fetch_hkex_fsp(force=True)
        assert len(calls) == 2

    def test_json_fetch_failure_raises_settlement_error(self, monkeypatch):
        import requests

        monkeypatch.setattr(settlement, "_discover_hkex_table_ids", lambda: (_HKEX_TABLE_ID, _HKEX_SORT_COL))
        monkeypatch.setattr(
            settlement.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.RequestException("down"))
        )
        with pytest.raises(settlement.SettlementError):
            settlement.fetch_hkex_fsp()

    def test_asof_carries_hkt_offset(self, monkeypatch):
        # The audit's timezone finding: asOf must be Hong Kong time (the
        # exchange's own calendar), not UTC or server-local -- a +08:00
        # offset is the concrete, assertable evidence of that.
        monkeypatch.setattr(settlement, "_discover_hkex_table_ids", lambda: (_HKEX_TABLE_ID, _HKEX_SORT_COL))
        monkeypatch.setattr(settlement, "_fetch_hkex_json", lambda t, s: _HKEX_SAMPLE_PAYLOAD)
        result = settlement.fetch_hkex_fsp()
        assert result["asOf"].endswith("+08:00")

    def test_missing_tableinfo_key_raises_settlement_error(self, monkeypatch):
        # A payload shape change (maintenance page, redesigned endpoint)
        # must surface as a fetch problem, not silently parse as "HKEX has
        # zero settlement rows today" -- a present-but-empty tableInfo list
        # (a genuinely different, if implausible, state) stays valid.
        monkeypatch.setattr(settlement, "_discover_hkex_table_ids", lambda: (_HKEX_TABLE_ID, _HKEX_SORT_COL))
        monkeypatch.setattr(settlement, "_fetch_hkex_json", lambda t, s: {"searchOptions": {}})
        with pytest.raises(settlement.SettlementError, match="tableInfo"):
            settlement.fetch_hkex_fsp()

    def test_present_but_empty_tableinfo_list_stays_valid(self, monkeypatch):
        monkeypatch.setattr(settlement, "_discover_hkex_table_ids", lambda: (_HKEX_TABLE_ID, _HKEX_SORT_COL))
        monkeypatch.setattr(settlement, "_fetch_hkex_json", lambda t, s: {"searchOptions": {}, "tableInfo": []})
        result = settlement.fetch_hkex_fsp()
        assert result["rows"] == []


class TestFilterHkexRows:
    _ROWS = [
        {"contract": "DAX Mini Futures", "hkatsCode": "MDX", "publishDateIso": "2026-07-01"},
        {"contract": "Hang Seng Index Futures", "hkatsCode": "HSI", "publishDateIso": "2020-01-01"},
    ]
    # Mirrors HKEX's real naming: the monthly HSCEI contract row never
    # contains the literal "HSCEI" (only tangential rows do), and combined
    # rows carry compound HKATS codes -- the exact shapes behind the
    # wrong-contract incident this filter's matching ladder guards against.
    _HSCEI_ROWS = [
        {
            "contract": "Hang Seng China Enterprises Index / Mini-Hang Seng China Enterprises Index Futures & Options",
            "hkatsCode": "HHI/MCH",
            "publishDateIso": "2026-05-28",
            "lastTradingDateIso": "2026-05-28",
            "yearMonth": "May-26",
            "fsp": 8333.0,
        },
        {
            "contract": "Hang Seng China Enterprises Index / Mini-Hang Seng China Enterprises Index Futures & Options",
            "hkatsCode": "HHI/MCH",
            "publishDateIso": "2026-06-29",
            "lastTradingDateIso": "2026-06-29",
            "yearMonth": "Jun-26",
            "fsp": 7632.0,
        },
        {
            "contract": "Weekly Hang Seng China Enterprises Index Options",
            "hkatsCode": "HHW",
            "publishDateIso": "2026-05-29",
            "lastTradingDateIso": "2026-05-29",
            "yearMonth": "May-26",
            "fsp": 8429.0,
        },
        {"contract": "HSCEI Dividend Point Index Futures", "hkatsCode": "DHH", "publishDateIso": "2026-05-28"},
        {"contract": "Hang Seng Index / Mini-Hang Seng Index Futures & Options", "hkatsCode": "HSI / MHI", "publishDateIso": "2026-06-29"},
    ]

    def test_filters_by_contract_substring_case_insensitive(self):
        out = settlement.filter_hkex_rows(self._ROWS, contract="dax")
        assert [r["contract"] for r in out] == ["DAX Mini Futures"]

    def test_filters_by_hkats_code_exact_case_insensitive(self):
        out = settlement.filter_hkex_rows(self._ROWS, hkats_code="hsi")
        assert [r["contract"] for r in out] == ["Hang Seng Index Futures"]

    def test_filters_by_months_back(self):
        out = settlement.filter_hkex_rows(self._ROWS, months_back=1)
        assert [r["contract"] for r in out] == ["DAX Mini Futures"]

    def test_no_filters_returns_everything(self):
        assert settlement.filter_hkex_rows(self._ROWS) == self._ROWS

    def test_hscei_alias_matches_official_contract_names(self):
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, contract="HSCEI")
        contracts = [r["contract"] for r in out]
        # The monthly futures row (the one the abbreviation almost always
        # means) must be included, not just the literal-substring hits.
        assert any(c.startswith("Hang Seng China Enterprises Index /") for c in contracts)
        assert "HSCEI Dividend Point Index Futures" in contracts
        # HSI rows must NOT ride along on the HSCEI alias.
        assert not any("Mini-Hang Seng Index" in c for c in contracts)

    def test_all_tokens_fallback_for_multiword_query(self):
        # "China Enterprises futures" isn't a contiguous substring of the
        # official name, but every word appears in it.
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, contract="China Enterprises futures")
        assert {r["hkatsCode"] for r in out} == {"HHI/MCH"}

    def test_hkats_code_matches_compound_code_component(self):
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, hkats_code="HHI")
        assert {r["hkatsCode"] for r in out} == {"HHI/MCH"}
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, hkats_code="mhi")
        assert {r["hkatsCode"] for r in out} == {"HSI / MHI"}

    def test_contract_query_also_matches_hkats_component(self):
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, contract="MCH")
        assert {r["hkatsCode"] for r in out} == {"HHI/MCH"}

    def test_code_query_not_shadowed_by_accidental_name_substring(self):
        # "TCH" is a substring of "CK Hutchison" -- a name hit must not
        # shadow the contract actually coded TCH (union, not fallback).
        rows = [
            {"contract": "CK Hutchison Holdings Ltd.", "hkatsCode": "CKH"},
            {"contract": "Tencent Holdings Ltd.", "hkatsCode": "TCH"},
        ]
        out = settlement.filter_hkex_rows(rows, contract="TCH")
        assert {r["hkatsCode"] for r in out} == {"CKH", "TCH"}

    def test_expiry_month_iso_returns_only_that_months_rows(self):
        # The repeat wrong-answer incident: asked for the May 2026 expiry,
        # the model picked the June monthly row (7632) and relabeled its
        # date. With expiry_month the June row must not even be returned.
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, contract="HSCEI", expiry_month="2026-05")
        assert {r.get("fsp") for r in out} == {8333.0, 8429.0}  # May monthly + May weekly only
        monthly = [r for r in out if "HHI" in r["hkatsCode"]]
        assert monthly[0]["fsp"] == 8333.0

    def test_expiry_month_accepts_hkex_yearmonth_wording(self):
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, hkats_code="HHI", expiry_month="jun-26")
        assert [r["fsp"] for r in out] == [7632.0]

    def test_expiry_month_with_no_rows_returns_empty_not_neighbor(self):
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, hkats_code="HHI", expiry_month="2026-04")
        assert out == []

    def test_expiry_month_unpadded_single_digit_still_matches(self):
        # An LLM-supplied "2026-5" (unpadded month) used to silently match
        # zero rows instead of the intended May expiry.
        out = settlement.filter_hkex_rows(self._HSCEI_ROWS, hkats_code="HHI", expiry_month="2026-5")
        assert [r["fsp"] for r in out] == [8333.0]

    def test_months_back_string_from_llm_no_longer_raises(self):
        # months_back arrives as an LLM tool-call arg -- a numeric string
        # ("3") used to raise TypeError instead of being tolerated like an int.
        out = settlement.filter_hkex_rows(self._ROWS, months_back="1")
        assert [r["contract"] for r in out] == ["DAX Mini Futures"]

    def test_months_back_unparseable_string_ignored_not_raised(self):
        out = settlement.filter_hkex_rows(self._ROWS, months_back="not-a-number")
        assert out == self._ROWS

    def test_months_back_negative_no_longer_silently_empties(self):
        # A negative months_back computes a future cutoff and used to
        # silently match nothing; it's now treated as "no filter".
        out = settlement.filter_hkex_rows(self._ROWS, months_back=-1)
        assert out == self._ROWS

    def test_non_string_contract_does_not_raise(self):
        out = settlement.filter_hkex_rows(self._ROWS, contract=123)
        assert out == []  # no match, but no AttributeError either


# ============================================================
# SGX
# ============================================================

_SGX_APPCONFIG = {
    "endpoints": {"CMS_API_URL": "https://api2.sgx.com/content-api"},
    "CMS_VERSION": "70f75ec90c030bab34d750ee55d74b016f70d4b6",
}


def _sgx_page_payload(file_url: str) -> dict:
    return {
        "data": {
            "route": {
                "data": {
                    "data": {
                        "widgets": [
                            {"widgetType": "section_title_widget", "title": "Final Settlement Price"},
                            {
                                "widgetType": "final_settlement_price",
                                "downloadItems": [
                                    {"data": {"file": {"data": {"file": {"data": {"url": file_url}}}}}}
                                ],
                                "fileCategory": {"data": {"name": "FlexC", "fieldCode": "fsp_flexc"}},
                            },
                        ]
                    }
                }
            }
        }
    }


class TestSgxEndpointChain:
    def test_resolves_main_file_url_through_full_chain(self, monkeypatch):
        file_url = "https://api2.sgx.com/sites/default/files/2026-07/fsp.xlsx"
        responses = {
            settlement.SGX_APPCONFIG_URL: _FakeResponse(json_data=_SGX_APPCONFIG),
        }

        def fake_get(url, params=None, headers=None, timeout=None):
            if url == settlement.SGX_APPCONFIG_URL:
                return responses[url]
            assert params["queryId"] == f"{_SGX_APPCONFIG['CMS_VERSION']}:page"
            variables = json.loads(params["variables"])
            assert variables["path"] == settlement.SGX_CMS_PAGE_PATH
            return _FakeResponse(json_data=_sgx_page_payload(file_url))

        monkeypatch.setattr(settlement.requests, "get", fake_get)
        cms_api_url, cms_version = settlement._sgx_cms_endpoints()
        resolved = settlement._sgx_main_file_url(cms_api_url, cms_version)
        assert resolved == file_url

    def test_missing_widget_raises_settlement_error(self, monkeypatch):
        monkeypatch.setattr(
            settlement.requests, "get",
            lambda *a, **k: _FakeResponse(json_data={"data": {"route": {"data": {"data": {"widgets": []}}}}}),
        )
        with pytest.raises(settlement.SettlementError):
            settlement._sgx_main_file_url("https://api2.sgx.com/content-api", "v1")

    def test_graphql_error_response_raises(self, monkeypatch):
        monkeypatch.setattr(
            settlement.requests, "get",
            lambda *a, **k: _FakeResponse(json_data={"errors": [{"message": "bad queryId"}]}),
        )
        with pytest.raises(settlement.SettlementError):
            settlement._sgx_cms_query("https://api2.sgx.com/content-api", "v1", "page", {})


class TestSgxAppconfigCaching:
    def test_appconfig_fetched_once_across_repeated_calls(self, monkeypatch):
        # A cold fetch_sgx_fsp() + fetch_sgx_flexc() each independently
        # resolve their own CMS endpoints via _sgx_cms_endpoints() -- this
        # used to mean appconfig.json (a file that barely changes) was
        # downloaded twice for one settlement-price question.
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            if url == settlement.SGX_APPCONFIG_URL:
                calls.append(1)
                return _FakeResponse(json_data=_SGX_APPCONFIG)
            raise AssertionError(f"unexpected URL {url}")

        monkeypatch.setattr(settlement.requests, "get", fake_get)
        settlement._sgx_cms_endpoints()
        settlement._sgx_cms_endpoints()
        assert len(calls) == 1

    def test_fetch_sgx_appconfig_itself_is_cached(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(1)
            return _FakeResponse(json_data=_SGX_APPCONFIG)

        monkeypatch.setattr(settlement.requests, "get", fake_get)
        first = settlement._fetch_sgx_appconfig()
        second = settlement._fetch_sgx_appconfig()
        assert len(calls) == 1
        assert first == second == _SGX_APPCONFIG


class TestSgxWorkbookParsing:
    def test_parses_both_sheets_and_normalizes_dates(self):
        import datetime as dt

        raw = _xlsx_bytes(
            {
                "Financials Contracts": [
                    ["Product Type", "Contract", "Ticker Symbol", "Contract Mth/Yr", "FSP", "FSP Date"],
                    ["FX", "SGX AUD/JPY Futures", "AJ", dt.datetime(2026, 7, 1), 112.45, dt.datetime(2026, 7, 13)],
                ],
                "Commodities Contracts": [
                    ["Product Type", "Contract", "Ticker Symbol", "Contract Mth/Yr", "FSP", "FSP Date"],
                    ["Rubber", "SGX SICOM RSS3 Futures", "RT", dt.datetime(2026, 7, 1), "270", dt.datetime(2026, 6, 30)],
                ],
            }
        )
        rows = settlement._parse_sgx_workbook(raw)
        assert len(rows) == 2
        fx_row = next(r for r in rows if r["sheet"] == "Financials Contracts")
        assert fx_row["contract"] == "SGX AUD/JPY Futures"
        assert fx_row["fsp"] == 112.45
        assert fx_row["contractMonth"] == "2026-07-01"
        assert fx_row["fspDate"] == "2026-07-13"
        commodities_row = next(r for r in rows if r["sheet"] == "Commodities Contracts")
        assert commodities_row["fsp"] == 270.0  # str-formatted source cell, coerced to float

    def test_blank_leading_cell_rows_are_skipped(self):
        raw = _xlsx_bytes(
            {
                "Financials Contracts": [
                    ["Product Type", "Contract", "Ticker Symbol", "Contract Mth/Yr", "FSP", "FSP Date"],
                    [None, None, None, None, None, None],
                ],
            }
        )
        assert settlement._parse_sgx_workbook(raw) == []

    def test_unparseable_bytes_raise_settlement_error(self):
        with pytest.raises(settlement.SettlementError):
            settlement._parse_sgx_workbook(b"not a real workbook")

    def test_weekly_option_ddmmyy_contract_month_normalized(self):
        # Live-observed: NSE IFSC Nifty weekly options carry a bare DDMMYY
        # text string ("140726" = 14 Jul 2026) in the Contract Mth/Yr
        # column instead of a real date -- contract_month filtering (an
        # ISO "YYYY-MM" prefix match) could never match it as a month.
        raw = _xlsx_bytes(
            {
                "Financials Contracts": [
                    ["Product Type", "Contract", "Ticker Symbol", "Contract Mth/Yr", "FSP", "FSP Date"],
                    ["Index Weekly Options", "NSE IFSC Nifty Weekly Options", "GINW", "140726", 100.0, "140726"],
                ],
            }
        )
        rows = settlement._parse_sgx_workbook(raw)
        assert rows[0]["contractMonth"] == "2026-07"
        # fspDate is intentionally untouched here -- only contractMonth
        # feeds contract_month filtering; normalizing it is out of scope.
        assert rows[0]["fspDate"] == "140726"

    def test_excel_serial_garbage_contract_month_becomes_none(self):
        # Live-observed: one SGX FTSE 5-Year India Government Bond Futures
        # row carries "1900-01-02" (Excel's day-zero, misread as a date) in
        # the Contract Mth/Yr column -- an implausible year, not a real
        # contract month.
        raw = _xlsx_bytes(
            {
                "Financials Contracts": [
                    ["Product Type", "Contract", "Ticker Symbol", "Contract Mth/Yr", "FSP", "FSP Date"],
                    ["Bonds", "SGX FTSE 5-Year India Government Bond Futures", "IN5", "1900-01-02", 100.0, None],
                ],
            }
        )
        rows = settlement._parse_sgx_workbook(raw)
        assert rows[0]["contractMonth"] is None


class TestNormalizeSgxContractMonth:
    def test_ddmmyy_text_becomes_year_month(self):
        assert settlement._normalize_sgx_contract_month("140726") == "2026-07"

    def test_legitimate_iso_date_passes_through_unchanged(self):
        assert settlement._normalize_sgx_contract_month("2026-07-01") == "2026-07-01"

    def test_implausible_year_iso_date_becomes_none(self):
        assert settlement._normalize_sgx_contract_month("1900-01-02") is None

    def test_non_string_value_passes_through_unchanged(self):
        assert settlement._normalize_sgx_contract_month(None) is None


class TestFilterSgxRows:
    _ROWS = [
        {"contract": "SGX Nikkei 225 Index Futures", "ticker": "NK", "contractMonth": "2026-07-01"},
        {"contract": "SGX USD Nikkei 225 Index Futures", "ticker": "NU", "contractMonth": "2026-06-01"},
    ]

    # Live-observed regression fixture: "NK" is a substring of "Bank", so a
    # bare-substring search for the Nikkei ticker used to pull in every
    # SGX NIFTY Bank Index row too.
    _ROWS_WITH_BANK_COLLISION = _ROWS + [
        {"contract": "NSE IFSC Nifty Bank Index Futures", "ticker": "BNF", "contractMonth": "2026-07-01"},
    ]

    def test_filters_by_search_substring_case_insensitive(self):
        out = settlement.filter_sgx_rows(self._ROWS, search="usd nikkei")
        assert [r["ticker"] for r in out] == ["NU"]

    def test_filters_by_ticker_substring(self):
        out = settlement.filter_sgx_rows(self._ROWS, search="nk")
        assert {r["ticker"] for r in out} == {"NK"}

    def test_short_search_does_not_match_inside_an_unrelated_word(self):
        out = settlement.filter_sgx_rows(self._ROWS_WITH_BANK_COLLISION, search="nk")
        assert {r["ticker"] for r in out} == {"NK"}  # NOT the Bank row too

    def test_short_search_matches_exact_compound_ticker_component(self):
        rows = [{"contract": "SGX Nikkei 225 Index Futures / Options", "ticker": "NK/NKO", "contractMonth": "2026-07-01"}]
        out = settlement.filter_sgx_rows(rows, search="NK")
        assert len(out) == 1

    def test_short_search_matches_whole_word_in_contract_name(self):
        rows = [{"contract": "SGX FTSE EM Futures", "ticker": "FEM", "contractMonth": "2026-07-01"}]
        out = settlement.filter_sgx_rows(rows, search="EM")
        assert len(out) == 1

    def test_long_search_still_uses_permissive_substring_match(self):
        # Above the short-needle threshold, a plain substring match is kept
        # (multi-word phrases like "usd nikkei" would never be exact-code
        # or single-word matches).
        out = settlement.filter_sgx_rows(self._ROWS_WITH_BANK_COLLISION, search="nifty bank")
        assert {r["ticker"] for r in out} == {"BNF"}

    def test_filters_by_contract_month(self):
        out = settlement.filter_sgx_rows(self._ROWS, contract_month="2026-06")
        assert [r["ticker"] for r in out] == ["NU"]

    def test_contract_month_with_no_rows_returns_empty(self):
        out = settlement.filter_sgx_rows(self._ROWS, contract_month="2026-01")
        assert out == []

    def test_contract_month_unpadded_single_digit_still_matches(self):
        out = settlement.filter_sgx_rows(self._ROWS, contract_month="2026-6")
        assert [r["ticker"] for r in out] == ["NU"]

    def test_no_filters_returns_everything(self):
        assert settlement.filter_sgx_rows(self._ROWS) == self._ROWS


class TestSgxTickerComponents:
    def test_splits_compound_ticker(self):
        assert settlement._sgx_ticker_components("NK/NKO") == ["NK", "NKO"]

    def test_bare_ticker_returns_single_component(self):
        assert settlement._sgx_ticker_components("NK") == ["NK"]

    def test_empty_string_returns_empty_list(self):
        assert settlement._sgx_ticker_components("") == []


class TestSgxFlexcWorkbookParsing:
    def test_parses_final_settlement_sheet(self):
        # Live-observed shape: FlexC's date column is text, US-style
        # MM/DD/YYYY -- "07/01/2026" means 1 July, not 7 January -- and
        # must come out ISO like every other date this app produces, not
        # passed through as ambiguous text.
        raw = _xlsx_bytes(
            {
                "Final Settlement": [
                    ["Ticker Symbol", "Final Sett Price (FSP)", "Final Sett Price (FSP) Date"],
                    ["UC010726", "6.7985", "07/01/2026"],
                ]
            }
        )
        rows = settlement._parse_sgx_flexc_workbook(raw)
        assert rows == [{"ticker": "UC010726", "fsp": 6.7985, "fspDate": "2026-07-01"}]

    def test_real_date_cell_still_normalizes_via_cell_to_iso(self):
        import datetime as _dt

        raw = _xlsx_bytes(
            {
                "Final Settlement": [
                    ["Ticker Symbol", "Final Sett Price (FSP)", "Final Sett Price (FSP) Date"],
                    ["UC010726", "6.7985", _dt.date(2026, 7, 1)],
                ]
            }
        )
        rows = settlement._parse_sgx_flexc_workbook(raw)
        assert rows[0]["fspDate"] == "2026-07-01"


class TestNormalizeFlexcDate:
    def test_normalizes_mmddyyyy_text(self):
        assert settlement._normalize_flexc_date("07/01/2026") == "2026-07-01"

    def test_normalizes_single_digit_month_and_day(self):
        assert settlement._normalize_flexc_date("7/1/2026") == "2026-07-01"

    def test_leaves_already_iso_string_unchanged(self):
        assert settlement._normalize_flexc_date("2026-07-01") == "2026-07-01"

    def test_leaves_non_date_string_unchanged(self):
        assert settlement._normalize_flexc_date("N/A") == "N/A"

    def test_leaves_none_unchanged(self):
        assert settlement._normalize_flexc_date(None) is None

    def test_invalid_calendar_date_passes_through_unchanged(self):
        assert settlement._normalize_flexc_date("13/40/2026") == "13/40/2026"


# ============================================================
# Eurex
# ============================================================


class TestEurexProductResolution:
    def test_seed_map_resolves_without_any_request(self, monkeypatch):
        def _fail(*a, **k):
            raise AssertionError("must not make an HTTP request for a seeded code")

        monkeypatch.setattr(settlement.requests, "get", _fail)
        assert settlement.resolve_eurex_product_id("fdax") == 34642

    def test_unresolved_code_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settlement, "EUREX_PRODUCT_IDS_FILE", tmp_path / "eurex_product_ids.json")
        assert settlement.resolve_eurex_product_id("ZZZZ") is None

    def test_persisted_store_is_checked_after_seed_map(self, tmp_path, monkeypatch):
        store = tmp_path / "eurex_product_ids.json"
        monkeypatch.setattr(settlement, "EUREX_PRODUCT_IDS_FILE", store)
        settlement._save_resolved_eurex_id("FGBL", 12345)
        assert settlement.resolve_eurex_product_id("fgbl") == 12345

    def test_resolve_from_url_extracts_and_persists(self, tmp_path, monkeypatch):
        store = tmp_path / "eurex_product_ids.json"
        monkeypatch.setattr(settlement, "EUREX_PRODUCT_IDS_FILE", store)
        page_html = '<script type="application/json" data-product="99887"> {"productId": "99887"} </script>'
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(text=page_html))

        product_id = settlement.resolve_eurex_product_id_from_url(
            "FGBM", "https://www.eurex.com/ex-en/markets/idx/dax/DAX-Futures-139902"
        )
        assert product_id == 99887
        assert settlement.resolve_eurex_product_id("FGBM") == 99887
        assert json.loads(store.read_text())["FGBM"] == 99887

    def test_resolve_from_url_rejects_non_eurex_domain(self):
        with pytest.raises(settlement.SettlementError):
            settlement.resolve_eurex_product_id_from_url("FGBM", "https://evil.example.com/page")

    def test_resolve_from_url_raises_when_id_not_found(self, monkeypatch):
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(text="<html></html>"))
        with pytest.raises(settlement.SettlementError):
            settlement.resolve_eurex_product_id_from_url("FGBM", "https://www.eurex.com/ex-en/markets/x")


class TestEurexProductsCatalog:
    def test_maps_catalog_fields(self, monkeypatch):
        payload = {
            "items": [
                {"PRODUCT_ID": "FDAX", "PRODUCT_NAME": "DAX® Futures", "PRODUCT_GROUP": "INDEX FUTURES", "CURRENCY": "EUR"},
                {"PRODUCT_NAME": "missing product id -- must be dropped"},
            ]
        }
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(json_data=payload))
        products = settlement.fetch_eurex_products()
        assert len(products) == 1
        assert products[0] == {"code": "FDAX", "name": "DAX® Futures", "group": "INDEX FUTURES", "currency": "EUR"}

    def test_missing_items_raises_settlement_error(self, monkeypatch):
        # This catalog normally holds ~3000 entries -- a missing/empty
        # `items` key means the payload's shape changed, not that Eurex
        # genuinely has zero products. Left unguarded, this silently blanks
        # every Eurex card in settlement_search's retrieval index.
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(json_data={}))
        with pytest.raises(settlement.SettlementError, match="items"):
            settlement.fetch_eurex_products()

    def test_empty_items_list_raises_settlement_error(self, monkeypatch):
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(json_data={"items": []}))
        with pytest.raises(settlement.SettlementError, match="items"):
            settlement.fetch_eurex_products()


class TestEurexSettlementParsing:
    _PAYLOAD = {
        "header": {"underlyingClosingPrice": 24999.53, "tradingDates": ["15-07-2026 12:00"]},
        "meta": {"productCode": "FDAX", "isin": "DE0008469594", "productType": "F"},
        "dataRows": [
            {"date": "20260918", "open": 25196.0, "high": 25251.0, "low": 24942.0, "last": 25104.0,
             "dSettle": 25127.0, "volume": 22920.0, "openInt": 56562.0, "contractType": "M"},
        ],
    }

    def test_normalizes_rows_and_meta(self, monkeypatch):
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(json_data=self._PAYLOAD))
        result = settlement.fetch_eurex_settlement(34642)
        assert result["productCode"] == "FDAX"
        assert result["tradingDates"] == ["15-07-2026 12:00"]
        row = result["rows"][0]
        assert row["settlementPrice"] == 25127.0
        assert row["dateIso"] == "2026-09-18"

    def test_error_response_raises_settlement_error(self, monkeypatch):
        monkeypatch.setattr(
            settlement.requests, "get",
            lambda *a, **k: _FakeResponse(json_data={"error": {"message": "No product found for productId: 1"}}),
        )
        with pytest.raises(settlement.SettlementError, match="No product found"):
            settlement.fetch_eurex_settlement(1)

    def test_busdate_passed_through_as_query_param(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["params"] = params
            return _FakeResponse(json_data=self._PAYLOAD)

        monkeypatch.setattr(settlement.requests, "get", fake_get)
        settlement.fetch_eurex_settlement(34642, busdate="20260715")
        assert captured["params"]["busdate"] == "20260715"

    def test_cache_key_is_per_product_and_busdate(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, params.get("busdate")))
            return _FakeResponse(json_data=self._PAYLOAD)

        monkeypatch.setattr(settlement.requests, "get", fake_get)
        settlement.fetch_eurex_settlement(34642)
        settlement.fetch_eurex_settlement(34642)  # cached, no new call
        settlement.fetch_eurex_settlement(34642, busdate="20260101")  # different cache key
        settlement.fetch_eurex_settlement(4663138)  # different product, different cache key
        assert len(calls) == 3


class TestEurexMsciParsing:
    def test_finds_blob_link_and_parses_workbook(self, monkeypatch):
        page_html = (
            '<a href="/resource/blob/1633874/244d7fd1d4d741b57d752a7b07c97f5e/'
            'data/msci-fut-settlement-prices.xlsx">Download</a>'
        )
        raw = _xlsx_bytes(
            {
                "FSP MSCI Futures": [
                    [None, "MSCI Futures - Final Settlement Prices"] + [None] * 8,
                    ["Index name", "Regional / Country", "Index type", "Markets*", "Currency",
                     "Dividend reinvestment**", "Eurex Codes", "Futures (BBG)", "FSP MAR26", "FSP JUN26"],
                    ["MSCI World SRI", "Regional", "ESG / SRI", "DM", "USD", "NTR", "FMRW", "CIWA", 5917.12, 6198.54],
                    ["MSCI Old Index", "Regional", "ESG / SRI", "DM", "USD", "NTR", "FMOL", "OLDA", "100.5", None],
                ]
            }
        )

        def fake_get(url, headers=None, timeout=None, params=None):
            if "msci-fut-settlement-prices.xlsx" in url:
                return _FakeResponse(content=raw)
            return _FakeResponse(text=page_html)

        monkeypatch.setattr(settlement.requests, "get", fake_get)
        result = settlement.fetch_eurex_msci_fsp()
        assert result["expiries"] == ["FSP MAR26", "FSP JUN26"]
        assert result["sourceFileUrl"].endswith("msci-fut-settlement-prices.xlsx")
        assert len(result["rows"]) == 2
        world_row = result["rows"][0]
        assert world_row["indexName"] == "MSCI World SRI"
        assert world_row["settlementPricesByExpiry"] == {"FSP MAR26": 5917.12, "FSP JUN26": 6198.54}
        old_row = result["rows"][1]
        assert old_row["settlementPricesByExpiry"] == {"FSP MAR26": 100.5}  # str cell coerced to float

    def test_missing_blob_link_raises(self, monkeypatch):
        monkeypatch.setattr(settlement.requests, "get", lambda *a, **k: _FakeResponse(text="<html></html>"))
        with pytest.raises(settlement.SettlementError):
            settlement.fetch_eurex_msci_fsp()

    def test_legend_footnote_rows_are_skipped(self, monkeypatch):
        # Live-observed: the workbook's trailing legend rows ("* DM =
        # Developed Markets / EM = ... Futures") have text in their first
        # cell (so they pass the blank-leading-cell skip) but no real
        # product code or settlement figures -- left in, they become
        # retrievable "contracts" with a null price.
        page_html = (
            '<a href="/resource/blob/1633874/244d7fd1d4d741b57d752a7b07c97f5e/'
            'data/msci-fut-settlement-prices.xlsx">Download</a>'
        )
        raw = _xlsx_bytes(
            {
                "FSP MSCI Futures": [
                    [None, "MSCI Futures - Final Settlement Prices"] + [None] * 8,
                    ["Index name", "Regional / Country", "Index type", "Markets*", "Currency",
                     "Dividend reinvestment**", "Eurex Codes", "Futures (BBG)", "FSP MAR26"],
                    ["MSCI World", "Regional", "Standard", "DM", "USD", "NTR", "FMWO", "MWDA", 100.0],
                    ["* DM = Developed Markets / EM = Emerging Markets / FM = Frontier Markets Futures",
                     None, None, None, None, None, None, None, None],
                ]
            }
        )

        def fake_get(url, headers=None, timeout=None, params=None):
            if "msci-fut-settlement-prices.xlsx" in url:
                return _FakeResponse(content=raw)
            return _FakeResponse(text=page_html)

        monkeypatch.setattr(settlement.requests, "get", fake_get)
        result = settlement.fetch_eurex_msci_fsp()
        assert len(result["rows"]) == 1
        assert result["rows"][0]["indexName"] == "MSCI World"


class TestLatestPopulatedMsciExpiry:
    def test_picks_rightmost_expiry_with_any_value(self):
        rows = [
            {"settlementPricesByExpiry": {"FSP MAR26": 100.0}},
            {"settlementPricesByExpiry": {}},
        ]
        assert settlement.latest_populated_msci_expiry(rows, ["FSP DEC25", "FSP MAR26", "FSP JUN26"]) == "FSP MAR26"

    def test_returns_none_when_all_expiries_empty(self):
        rows = [{"settlementPricesByExpiry": {}}]
        assert settlement.latest_populated_msci_expiry(rows, ["FSP MAR26"]) is None


# ============================================================
# Shared cache helper
# ============================================================


class TestCachedFetch:
    def test_force_bypasses_cache(self):
        calls = []
        fn = lambda: calls.append(1) or len(calls)  # noqa: E731

        settlement._cached_fetch("k", False, 600.0, fn)
        settlement._cached_fetch("k", False, 600.0, fn)
        assert len(calls) == 1
        settlement._cached_fetch("k", True, 600.0, fn)
        assert len(calls) == 2

    def test_expires_after_ttl(self, monkeypatch):
        calls = []
        fn = lambda: calls.append(1)  # noqa: E731
        t = [1000.0]
        monkeypatch.setattr(settlement.time, "monotonic", lambda: t[0])

        settlement._cached_fetch("k", False, 5.0, fn)
        t[0] += 3
        settlement._cached_fetch("k", False, 5.0, fn)
        assert len(calls) == 1  # still within ttl
        t[0] += 3
        settlement._cached_fetch("k", False, 5.0, fn)
        assert len(calls) == 2  # ttl elapsed

    def test_cache_size_capped_and_evicts_oldest_stored(self, monkeypatch):
        monkeypatch.setattr(settlement, "_CACHE_MAX_ENTRIES", 3)
        t = [1000.0]
        monkeypatch.setattr(settlement.time, "monotonic", lambda: t[0])

        for i in range(3):
            settlement._cached_fetch(f"k{i}", False, 600.0, lambda i=i: i)
            t[0] += 1
        assert set(settlement._CACHE) == {"k0", "k1", "k2"}

        settlement._cached_fetch("k3", False, 600.0, lambda: 3)
        assert set(settlement._CACHE) == {"k1", "k2", "k3"}  # k0 (oldest) evicted

    def test_cache_peek_returns_age_and_value_without_forcing_a_fetch(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(settlement.time, "monotonic", lambda: t[0])
        calls = []
        settlement._cached_fetch("k", False, 600.0, lambda: calls.append(1) or "value")
        t[0] += 10
        assert settlement._cache_peek("k") == (10.0, "value")
        assert len(calls) == 1  # peek must not have triggered a re-fetch

    def test_cache_peek_returns_none_when_absent(self):
        assert settlement._cache_peek("a-key-nothing-has-ever-cached") is None

    def test_concurrent_fetches_do_not_corrupt_the_cache(self):
        import threading as _threading

        errors: list[Exception] = []

        def worker():
            try:
                for _ in range(50):
                    settlement._cached_fetch("shared-key", False, 600.0, lambda: "value")
            except Exception as exc:  # noqa: BLE001 - the assertion is that NOTHING raises
                errors.append(exc)

        threads = [_threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == []
        assert settlement._CACHE["shared-key"][1] == "value"
