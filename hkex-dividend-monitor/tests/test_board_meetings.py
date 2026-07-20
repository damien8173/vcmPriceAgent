import pytest

import monitor.board_meetings as bm
import monitor.settlement as settlement


@pytest.fixture(autouse=True)
def _clear_settlement_cache():
    settlement._CACHE.clear()
    yield
    settlement._CACHE.clear()


class _FakeResponse:
    def __init__(self, *, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


# Minimal but structurally faithful reproduction of the real page's markup
# (verified live 2026-07-17 via view-source, not guessed): a title block,
# a "Date :" line, then a <table> whose rows are each six
# <td><font ...>CONTENT</font></td> cells -- header row, a "----" divider
# row (both share the data rows' six-cell shape but aren't real data),
# then real rows covering: normal 3-digit code (nbsp-padded), a row with
# no Period, and an already-5-digit code (no padding needed, e.g. a
# dual-counter "-R" security).
_SAMPLE_HTML = (
    "<font class=textfont><br/>\n"
    "Date : 16/07/2026<br/>\n"
    "<br/>\n"
    "</font><br/>\n"
    "<table class=textfont>"
    "<tr><td><font face='monospace' style='font-size: 12'>BM Date</font></td>"
    "<td><font face='monospace' style='font-size: 12'></font></td>"
    "<td><font face='monospace' style='font-size: 12'>Stock Short Name<td>"
    "<font face='monospace' style='font-size: 12'>&nbsp;Code</font></td>"
    "<td><font face='monospace' style='font-size: 12'>Purpose</font></td>"
    "<td><font face='monospace' style='font-size: 12'>Period</font></td></font></td></tr>"
    "<tr><td><font face='monospace' style='font-size: 12'>----------</font></td>"
    "<td><font face='monospace' style='font-size: 12'></font></td>"
    "<td><font face='monospace' style='font-size: 12'>----------------</font></td>"
    "<td><font face='monospace' style='font-size: 12'>-----</font></td>"
    "<td><font face='monospace' style='font-size: 12'>-------------------</font></td>"
    "<td><font face='monospace' style='font-size: 12'>---------------------</font></td></tr>"
    "<tr><td width=75 valign=top><font face='monospace' style='font-size: 12'>17/07/2026</font></td>"
    "<td width=30 valign=top align=right><font face='monospace' style='font-size: 12'></font></td>"
    "<td width=120 valign=top><font face='monospace' style='font-size: 12'>CHINA PPT INV</font></td>"
    "<td width=50 valign=top><font face='monospace' style='font-size: 12'>&nbsp;&nbsp;736</font></td>"
    "<td width=140 valign=top><font face='monospace' style='font-size: 12'>FIN RES</font></td>"
    "<td valign=top><font face='monospace' style='font-size: 12'>Y.E.31/03/26</font></td></tr>"
    "<tr><td width=75 valign=top><font face='monospace' style='font-size: 12'>21/07/2026</font></td>"
    "<td width=30 valign=top align=right><font face='monospace' style='font-size: 12'></font></td>"
    "<td width=120 valign=top><font face='monospace' style='font-size: 12'>FULU HOLDINGS</font></td>"
    "<td width=50 valign=top><font face='monospace' style='font-size: 12'>&nbsp;2101</font></td>"
    "<td width=140 valign=top><font face='monospace' style='font-size: 12'>SPECIAL DIVIDEND</font></td>"
    "<td valign=top><font face='monospace' style='font-size: 12'></font></td></tr>"
    "<tr><td width=75 valign=top><font face='monospace' style='font-size: 12'>12/08/2026</font></td>"
    "<td width=30 valign=top align=right><font face='monospace' style='font-size: 12'></font></td>"
    "<td width=120 valign=top><font face='monospace' style='font-size: 12'>TENCENT-R</font></td>"
    "<td width=50 valign=top><font face='monospace' style='font-size: 12'>80700</font></td>"
    "<td width=140 valign=top><font face='monospace' style='font-size: 12'>INT RES/DIV</font></td>"
    "<td valign=top><font face='monospace' style='font-size: 12'>6-MTH-ENDED30/06/26</font></td></tr>"
    "</table>\n"
    "<br/>\n"
)


class TestParseBoardMeetingsHtml:
    def test_parses_generated_date(self):
        _rows, generated_date = bm.parse_board_meetings_html(_SAMPLE_HTML)
        assert generated_date == "2026-07-16"

    def test_skips_header_and_separator_rows(self):
        rows, _ = bm.parse_board_meetings_html(_SAMPLE_HTML)
        assert len(rows) == 3  # only the 3 real data rows, not header/divider

    def test_normalizes_date_and_zero_pads_code(self):
        rows, _ = bm.parse_board_meetings_html(_SAMPLE_HTML)
        first = rows[0]
        assert first["bmDate"] == "2026-07-17"
        assert first["stockCode"] == "00736"  # nbsp-padded "  736" -> zero-padded "00736"
        assert first["stockName"] == "CHINA PPT INV"
        assert first["purpose"] == "FIN RES"
        assert first["period"] == "Y.E.31/03/26"
        assert first["likelyDividend"] is False

    def test_blank_period_becomes_none(self):
        rows, _ = bm.parse_board_meetings_html(_SAMPLE_HTML)
        assert rows[1]["period"] is None

    def test_already_five_digit_code_needs_no_padding(self):
        rows, _ = bm.parse_board_meetings_html(_SAMPLE_HTML)
        assert rows[2]["stockCode"] == "80700"
        assert rows[2]["stockName"] == "TENCENT-R"

    def test_div_in_purpose_flags_likely_dividend(self):
        rows, _ = bm.parse_board_meetings_html(_SAMPLE_HTML)
        assert rows[1]["purpose"] == "SPECIAL DIVIDEND"
        assert rows[1]["likelyDividend"] is True
        assert rows[2]["purpose"] == "INT RES/DIV"
        assert rows[2]["likelyDividend"] is True

    def test_no_generated_date_line_returns_none(self):
        rows, generated_date = bm.parse_board_meetings_html("<table></table>")
        assert rows == []
        assert generated_date is None


class TestFetchBoardMeetings:
    def test_end_to_end_fetch(self, monkeypatch):
        monkeypatch.setattr(bm.requests, "get", lambda *a, **k: _FakeResponse(text=_SAMPLE_HTML))
        data = bm.fetch_board_meetings()
        assert data["generatedDate"] == "2026-07-16"
        assert data["sourceUrl"] == bm.BOARD_MEETINGS_URL
        assert len(data["rows"]) == 3
        assert data["asOf"].endswith("+08:00")  # HKT, not UTC/server-local

    def test_result_is_cached_until_force(self, monkeypatch):
        calls = []

        def fake_get(*a, **k):
            calls.append(1)
            return _FakeResponse(text=_SAMPLE_HTML)

        monkeypatch.setattr(bm.requests, "get", fake_get)
        bm.fetch_board_meetings()
        bm.fetch_board_meetings()
        assert len(calls) == 1
        bm.fetch_board_meetings(force=True)
        assert len(calls) == 2

    def test_network_failure_raises_board_meetings_error(self, monkeypatch):
        import requests as real_requests

        def _raise(*a, **k):
            raise real_requests.RequestException("timeout")

        monkeypatch.setattr(bm.requests, "get", _raise)
        with pytest.raises(bm.BoardMeetingsError):
            bm.fetch_board_meetings()

    def test_html_with_zero_parseable_rows_raises(self, monkeypatch):
        # A structurally-changed page (or a maintenance/error page) that
        # fetches fine but yields no rows must not be reported as "no
        # board meetings scheduled anywhere on HKEX" -- that's never
        # genuinely true.
        monkeypatch.setattr(bm.requests, "get", lambda *a, **k: _FakeResponse(text="<html>nothing here</html>"))
        with pytest.raises(bm.BoardMeetingsError, match="no parseable rows"):
            bm.fetch_board_meetings()

    def test_bad_status_code_raises(self, monkeypatch):
        monkeypatch.setattr(bm.requests, "get", lambda *a, **k: _FakeResponse(text="", status_code=503))
        with pytest.raises(bm.BoardMeetingsError):
            bm.fetch_board_meetings()


class TestFilterBoardMeetingRows:
    _ROWS = [
        {"bmDate": "2026-07-17", "stockName": "CHINA PPT INV", "stockCode": "00736",
         "purpose": "FIN RES", "period": "Y.E.31/03/26", "likelyDividend": False},
        {"bmDate": "2026-07-21", "stockName": "FULU HOLDINGS", "stockCode": "02101",
         "purpose": "SPECIAL DIVIDEND", "period": None, "likelyDividend": True},
        {"bmDate": "2026-08-12", "stockName": "TENCENT-R", "stockCode": "80700",
         "purpose": "INT RES/DIV", "period": "6-MTH-ENDED30/06/26", "likelyDividend": True},
    ]

    def test_no_filters_returns_everything(self):
        assert bm.filter_board_meeting_rows(self._ROWS) == self._ROWS

    def test_filters_by_ticker_normalizes_input(self):
        # "736" (unpadded, as a user/model might type it) must still match
        # the zero-padded stored form "00736".
        out = bm.filter_board_meeting_rows(self._ROWS, ticker="736")
        assert [r["stockCode"] for r in out] == ["00736"]

    def test_invalid_ticker_returns_empty_not_an_error(self):
        assert bm.filter_board_meeting_rows(self._ROWS, ticker="not-a-ticker") == []

    def test_filters_by_date_from(self):
        out = bm.filter_board_meeting_rows(self._ROWS, date_from="2026-07-21")
        assert [r["stockCode"] for r in out] == ["02101", "80700"]

    def test_filters_by_date_to(self):
        out = bm.filter_board_meeting_rows(self._ROWS, date_to="2026-07-21")
        assert [r["stockCode"] for r in out] == ["00736", "02101"]

    def test_filters_by_date_range(self):
        out = bm.filter_board_meeting_rows(self._ROWS, date_from="2026-07-18", date_to="2026-08-01")
        assert [r["stockCode"] for r in out] == ["02101"]

    def test_dividend_only_filter(self):
        out = bm.filter_board_meeting_rows(self._ROWS, dividend_only=True)
        assert {r["stockCode"] for r in out} == {"02101", "80700"}

    def test_combined_filters(self):
        out = bm.filter_board_meeting_rows(self._ROWS, dividend_only=True, date_to="2026-07-31")
        assert [r["stockCode"] for r in out] == ["02101"]
