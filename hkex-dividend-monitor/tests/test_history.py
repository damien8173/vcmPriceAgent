from datetime import date

import pytest

import monitor.history as history


class TestSqlLiteral:
    def test_none_renders_as_none_keyword(self):
        assert history._sql_literal(None) == "NONE"

    def test_bool_renders_lowercase(self):
        assert history._sql_literal(True) == "true"
        assert history._sql_literal(False) == "false"

    def test_number_renders_as_is(self):
        assert history._sql_literal(35) == "35"
        assert history._sql_literal(8.5) == "8.5"

    def test_date_renders_as_datetime_literal(self):
        assert history._sql_literal(date(2026, 7, 14)) == "d'2026-07-14'"

    def test_string_is_escaped_and_quoted(self):
        assert history._sql_literal("O'Brien") == "'O\\'Brien'"

    def test_list_of_dicts_renders_nested_object_literals(self):
        out = history._sql_literal([{"signal": "x", "weight": 5}])
        assert out == "[{'signal': 'x', 'weight': 5}]"

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            history._sql_literal(object())


class TestEnsureSchema:
    def test_uses_if_not_exists_so_repeat_calls_never_error(self, monkeypatch):
        """Regression test: a bare `DEFINE TABLE x SCHEMALESS;` (no `IF NOT
        EXISTS`) errors on SurrealDB if the table is already defined --
        ensure_schema is called on every web app startup AND on every
        watchlist generation, so without this clause the second call ever
        made against a real database fails."""
        captured = {}
        monkeypatch.setattr(history, "query", lambda sql: captured.setdefault("sql", sql))
        history.ensure_schema()
        assert "DEFINE TABLE IF NOT EXISTS company_event SCHEMALESS" in captured["sql"]
        assert "DEFINE TABLE IF NOT EXISTS dividend_watchlist SCHEMALESS" in captured["sql"]


class TestUpsertEvent:
    def test_rejects_invalid_filing_id(self):
        with pytest.raises(ValueError):
            history.upsert_event({"filingId": "not-hex"})

    def test_builds_upsert_with_set_clause(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(history, "query", lambda sql: captured.setdefault("sql", sql))
        history.upsert_event({"filingId": "abcdef0123456789", "stockCode": "00700", "dividendAmount": None})
        sql = captured["sql"]
        assert "UPSERT company_event:abcdef0123456789 SET" in sql
        assert "stockCode = '00700'" in sql
        assert "dividendAmount = NONE" in sql
        assert "updatedAt = time::now()" in sql


class TestEventsForTicker:
    def test_builds_select_scoped_to_ticker(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(history, "query", lambda sql: captured.setdefault("sql", sql) or [])
        history.events_for_ticker("00700")
        assert "FROM company_event" in captured["sql"]
        assert "stockCode = '00700'" in captured["sql"]
        assert "ORDER BY announcementDate ASC" in captured["sql"]


class TestKnownFilingIds:
    def test_returns_set_of_filing_ids(self, monkeypatch):
        monkeypatch.setattr(
            history, "query", lambda sql: [{"filingId": "aaaa000000000001"}, {"filingId": "bbbb000000000002"}]
        )
        ids = history.known_filing_ids()
        assert ids == {"aaaa000000000001", "bbbb000000000002"}

    def test_skips_rows_with_no_filing_id(self, monkeypatch):
        monkeypatch.setattr(history, "query", lambda sql: [{"filingId": None}, {}])
        assert history.known_filing_ids() == set()


class TestSaveWatchlist:
    def test_deletes_by_date_then_inserts_each_row(self, monkeypatch):
        statements = []
        monkeypatch.setattr(history, "query", lambda sql: statements.append(sql))
        rows = [
            {"stockCode": "00700", "score": 80, "band": "High"},
            {"stockCode": "00005", "score": 60, "band": "Medium"},
        ]
        history.save_watchlist(date(2026, 7, 14), "2026-07-14T01:00:00Z", rows)

        assert len(statements) == 1  # one combined multi-statement query call
        sql = statements[0]
        assert "DELETE dividend_watchlist WHERE watchlistDate = d'2026-07-14';" in sql
        assert sql.index("DELETE") < sql.index("UPSERT")  # delete happens before inserts
        assert sql.count("UPSERT dividend_watchlist:") == 2
        assert "stockCode = '00700'" in sql
        assert "stockCode = '00005'" in sql

    def test_row_ids_are_deterministic_per_date_and_ticker(self):
        id1 = history._watchlist_row_id(date(2026, 7, 14), "00700")
        id2 = history._watchlist_row_id(date(2026, 7, 14), "00700")
        id3 = history._watchlist_row_id(date(2026, 7, 15), "00700")
        assert id1 == id2  # idempotent across repeated generation the same day
        assert id1 != id3  # a different day gets a different row


class TestLoadWatchlist:
    def test_returns_none_when_no_rows(self, monkeypatch):
        monkeypatch.setattr(history, "query", lambda sql: [])
        assert history.load_watchlist(date(2026, 7, 14)) is None

    def test_returns_generated_at_and_rows(self, monkeypatch):
        monkeypatch.setattr(
            history,
            "query",
            lambda sql: [
                {"stockCode": "00700", "generatedAt": "2026-07-14T01:00:00Z", "rank": 1},
                {"stockCode": "00005", "generatedAt": "2026-07-14T01:00:00Z", "rank": 2},
            ],
        )
        result = history.load_watchlist(date(2026, 7, 14))
        assert result["generatedAt"] == "2026-07-14T01:00:00Z"
        assert len(result["rows"]) == 2


class TestWatchlistExists:
    def test_true_when_rows_present(self, monkeypatch):
        monkeypatch.setattr(history, "query", lambda sql: [{"stockCode": "00700"}])
        assert history.watchlist_exists(date(2026, 7, 14)) is True

    def test_false_when_empty(self, monkeypatch):
        monkeypatch.setattr(history, "query", lambda sql: [])
        assert history.watchlist_exists(date(2026, 7, 14)) is False


class TestLatestWatchlistDate:
    def test_none_when_empty(self, monkeypatch):
        monkeypatch.setattr(history, "query", lambda sql: [])
        assert history.latest_watchlist_date() is None

    def test_parses_returned_date(self, monkeypatch):
        monkeypatch.setattr(history, "query", lambda sql: [{"watchlistDate": "2026-07-14T00:00:00Z"}])
        assert history.latest_watchlist_date() == date(2026, 7, 14)
