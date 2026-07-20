from datetime import date, datetime, timezone

import pytest

import monitor.db as db


class TestEscaping:
    def test_escapes_backslash_and_single_quote(self):
        assert db._escape_sql_string("O'Brien") == "O\\'Brien"
        assert db._escape_sql_string("a\\b") == "a\\\\b"

    def test_leaves_other_characters_untouched(self):
        assert db._escape_sql_string("Tencent Holdings Ltd") == "Tencent Holdings Ltd"


class TestValidateFilingId:
    def test_accepts_valid_16_char_lowercase_hex(self):
        assert db._validate_filing_id("abcdef0123456789") == "abcdef0123456789"

    @pytest.mark.parametrize(
        "bad_id",
        [
            "ABCDEF0123456789",  # uppercase not allowed
            "abcdef012345678",  # too short
            "abcdef01234567890",  # too long
            "not-hex-at-all!!",  # non-hex characters
            "",
        ],
    )
    def test_rejects_invalid_ids(self, bad_id):
        with pytest.raises(ValueError):
            db._validate_filing_id(bad_id)


class TestFetchMatchingFilings:
    def test_empty_input_returns_empty_without_querying(self, monkeypatch):
        called = []
        monkeypatch.setattr(db, "query", lambda sql, timeout=30.0: called.append(sql))
        result = db.fetch_matching_filings({})
        assert result == []
        assert not called

    def test_builds_per_ticker_where_clause(self, monkeypatch):
        """Each ticker must be bound by its OWN date, not one global cutoff --
        regression test for the bug where one old watchlist target widened
        the query for every other ticker too."""
        captured = {}

        def fake_query(sql, timeout=30.0):
            captured["sql"] = sql
            return []

        monkeypatch.setattr(db, "query", fake_query)
        db.fetch_matching_filings({"00700": date(2026, 8, 1), "00005": date(2020, 1, 1)})
        sql = captured["sql"]
        assert "stockCode = '00700'" in sql
        assert "stockCode = '00005'" in sql
        assert " OR " in sql
        # Each ticker's own date appears paired with that ticker, not the
        # other one's -- a naive shared-cutoff bug would only ever show the
        # min() of the two dates.
        assert "2026-07-31" in sql or "2026-08-01" in sql  # 00700's date (UTC shift from HKT)
        assert "2019-12-31" in sql or "2020-01-01" in sql  # 00005's date

    def test_single_ticker_query_has_no_or(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(db, "query", lambda sql, timeout=30.0: captured.setdefault("sql", sql) or [])
        db.fetch_matching_filings({"00700": date(2026, 8, 1)})
        assert " OR " not in captured["sql"]
        assert "stockCode = '00700'" in captured["sql"]


class TestFilingHktDate:
    def test_converts_utc_iso_string_to_hkt_calendar_date(self):
        # 20:00 UTC + 8h (HKT) = 04:00 next day HKT.
        filing = {"filingDate": "2026-08-01T20:00:00Z"}
        assert db.filing_hkt_date(filing) == date(2026, 8, 2)

    def test_handles_offset_without_z_suffix(self):
        filing = {"filingDate": "2026-08-01T09:00:00+00:00"}
        assert db.filing_hkt_date(filing) == date(2026, 8, 1)

    def test_returns_none_for_missing_or_unparseable_date(self):
        assert db.filing_hkt_date({}) is None
        assert db.filing_hkt_date({"filingDate": "not-a-date"}) is None
