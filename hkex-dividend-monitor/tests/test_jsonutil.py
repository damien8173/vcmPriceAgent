from monitor.jsonutil import to_iso_date_str


class TestToIsoDateStr:
    def test_iso_datetime_string(self):
        assert to_iso_date_str("2026-08-15T08:30:00Z") == "2026-08-15"

    def test_iso_datetime_with_offset(self):
        assert to_iso_date_str("2026-08-15T08:30:00+00:00") == "2026-08-15"

    def test_hkex_dd_mm_yyyy(self):
        assert to_iso_date_str("15/08/2026") == "2026-08-15"

    def test_hkex_dd_mm_yyyy_with_time(self):
        """Race mode's dateTime field includes a time component
        (space-separated, not slash-separated) -- this must not corrupt the
        date the way a naive split("/") into exactly 3 parts would."""
        assert to_iso_date_str("15/08/2026 16:45") == "2026-08-15"

    def test_empty_and_none(self):
        assert to_iso_date_str("") == ""
        assert to_iso_date_str(None) == ""
