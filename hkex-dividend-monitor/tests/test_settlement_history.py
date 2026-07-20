from datetime import date

import pytest

import monitor.settlement_history as settlement_history
from monitor import settlement


@pytest.fixture(autouse=True)
def _clear_settlement_cache():
    # archive_range() is cached via settlement._cached_fetch -- the
    # module-level _CACHE would otherwise leak a result between tests.
    settlement._CACHE.clear()
    yield
    settlement._CACHE.clear()


class TestEnsureSchema:
    def test_uses_if_not_exists_so_repeat_calls_never_error(self, monkeypatch):
        # query() always returns a list of result rows (see monitor.db.query)
        # -- the mock must too, since ensure_schema now also runs the
        # slash-date migration's SELECT (an empty list = nothing to migrate).
        calls = []
        monkeypatch.setattr(settlement_history, "query", lambda sql: calls.append(sql) or [])
        settlement_history.ensure_schema()
        assert "DEFINE TABLE IF NOT EXISTS sgx_settlement_history SCHEMALESS" in calls[0]

    def test_runs_the_slash_date_migration(self, monkeypatch):
        calls = []
        monkeypatch.setattr(settlement_history, "query", lambda sql: calls.append(sql) or [])
        settlement_history.ensure_schema()
        assert any("WHERE fspDate CONTAINS '/'" in sql for sql in calls)


class TestMigrateSlashDates:
    def test_empty_select_is_a_no_op(self, monkeypatch):
        calls = []
        monkeypatch.setattr(settlement_history, "query", lambda sql: calls.append(sql) or [])
        settlement_history._migrate_slash_dates()
        assert len(calls) == 1  # only the SELECT -- no second batch call when there's nothing to fix

    def test_migrates_a_slash_date_row_to_iso(self, monkeypatch):
        select_result = [
            {
                "id": "sgx_settlement_history:oldid", "source": "flexc", "ticker": "UC010726",
                "tickerComponents": ["UC010726"], "fspDate": "07/01/2026", "fsp": 6.7985,
                "archivedAt": "2026-07-01T00:00:00Z",
            },
        ]
        calls = []

        def fake_query(sql):
            calls.append(sql)
            return select_result if "SELECT" in sql else []

        monkeypatch.setattr(settlement_history, "query", fake_query)
        settlement_history._migrate_slash_dates()

        assert len(calls) == 2
        batch_sql = calls[1]
        old_id = settlement_history._row_id("07/01/2026", "UC010726", "flexc")
        new_id = settlement_history._row_id("2026-07-01", "UC010726", "flexc")
        assert f"UPSERT sgx_settlement_history:{new_id}" in batch_sql
        assert "fspDate = '2026-07-01'" in batch_sql
        assert f"DELETE sgx_settlement_history:{old_id};" in batch_sql
        # The stale/computed fields (record id, archivedAt) must not be
        # carried over verbatim -- id isn't a settable field and archivedAt
        # is freshly set by the UPSERT itself.
        assert "id = " not in batch_sql

    def test_row_with_slash_but_unparseable_date_is_left_alone(self, monkeypatch):
        select_result = [{"id": "x", "source": "main", "ticker": "NK", "fspDate": "not/a/date", "fsp": 1}]
        calls = []

        def fake_query(sql):
            calls.append(sql)
            return select_result if "SELECT" in sql else []

        monkeypatch.setattr(settlement_history, "query", fake_query)
        settlement_history._migrate_slash_dates()
        assert len(calls) == 1  # SELECT only -- nothing parsed, so nothing to write

    def test_row_missing_ticker_or_source_is_skipped(self, monkeypatch):
        select_result = [{"id": "x", "fspDate": "07/01/2026", "fsp": 1}]  # no ticker, no source
        calls = []

        def fake_query(sql):
            calls.append(sql)
            return select_result if "SELECT" in sql else []

        monkeypatch.setattr(settlement_history, "query", fake_query)
        settlement_history._migrate_slash_dates()
        assert len(calls) == 1


class TestRowId:
    def test_deterministic_per_date_ticker_source(self):
        id1 = settlement_history._row_id("2026-07-10", "NK", "main")
        id2 = settlement_history._row_id("2026-07-10", "NK", "main")
        assert id1 == id2  # re-archiving the same day is a no-op, not a duplicate

    def test_different_date_gets_different_id(self):
        id1 = settlement_history._row_id("2026-07-10", "NK", "main")
        id2 = settlement_history._row_id("2026-07-11", "NK", "main")
        assert id1 != id2

    def test_different_source_gets_different_id(self):
        # main vs flexc rows for the same ticker/date must not collide and
        # silently overwrite each other.
        id1 = settlement_history._row_id("2026-07-10", "UC010726", "main")
        id2 = settlement_history._row_id("2026-07-10", "UC010726", "flexc")
        assert id1 != id2


class TestTickerComponents:
    def test_splits_compound_ticker(self):
        assert settlement_history._ticker_components("NK/NKO") == ["NK", "NKO"]

    def test_single_ticker_unchanged(self):
        assert settlement_history._ticker_components("UC010726") == ["UC010726"]

    def test_uppercases_and_strips(self):
        assert settlement_history._ticker_components(" nk / nko ") == ["NK", "NKO"]


class TestArchiveSgxSnapshot:
    def test_archives_main_and_flexc_rows_in_one_query_call(self, monkeypatch):
        statements = []
        monkeypatch.setattr(settlement_history, "query", lambda sql: statements.append(sql))
        main_rows = [
            {"sheet": "Financials Contracts", "productType": "Equity Index", "contract": "SGX Nikkei 225 Index Futures",
             "ticker": "NK/NKO", "contractMonth": "2026-07-01", "fsp": 69171.55, "fspDate": "2026-07-10"},
        ]
        flexc_rows = [{"ticker": "UC010726", "fsp": 6.7985, "fspDate": "2026-07-01"}]

        count = settlement_history.archive_sgx_snapshot(main_rows, flexc_rows)

        assert count == 2
        assert len(statements) == 1  # one combined multi-statement query call, like history.save_watchlist
        sql = statements[0]
        assert sql.count("UPSERT sgx_settlement_history:") == 2
        assert "source = 'main'" in sql
        assert "source = 'flexc'" in sql
        assert "ticker = 'NK/NKO'" in sql
        assert "tickerComponents = ['NK', 'NKO']" in sql
        assert "contract = 'SGX Nikkei 225 Index Futures'" in sql
        assert "fsp = 69171.55" in sql
        assert "fspDate = '2026-07-10'" in sql
        assert "archivedAt = time::now()" in sql

    def test_skips_rows_missing_fsp_date_or_ticker(self, monkeypatch):
        statements = []
        monkeypatch.setattr(settlement_history, "query", lambda sql: statements.append(sql))
        main_rows = [
            {"ticker": "NK", "fspDate": None, "fsp": 100},  # no fspDate -- nothing stable to key on
            {"ticker": None, "fspDate": "2026-07-10", "fsp": 100},  # no ticker
        ]
        count = settlement_history.archive_sgx_snapshot(main_rows, [])
        assert count == 0
        assert statements == []  # no query call at all when there's nothing to archive

    def test_main_and_flexc_rows_for_same_ticker_and_date_do_not_collide(self, monkeypatch):
        statements = []
        monkeypatch.setattr(settlement_history, "query", lambda sql: statements.append(sql))
        main_rows = [{"ticker": "X", "fspDate": "2026-07-10", "fsp": 1}]
        flexc_rows = [{"ticker": "X", "fspDate": "2026-07-10", "fsp": 2}]
        count = settlement_history.archive_sgx_snapshot(main_rows, flexc_rows)
        assert count == 2
        assert statements[0].count("UPSERT sgx_settlement_history:") == 2

    def test_repeated_archiving_reuses_the_same_row_id(self, monkeypatch):
        # Idempotency: archiving twice for the same day produces the same
        # UPSERT target id both times (a real DB would just overwrite, not
        # duplicate).
        statements = []
        monkeypatch.setattr(settlement_history, "query", lambda sql: statements.append(sql))
        rows = [{"ticker": "NK", "fspDate": "2026-07-10", "fsp": 69171.55}]
        settlement_history.archive_sgx_snapshot(rows, [])
        settlement_history.archive_sgx_snapshot(rows, [])
        id1 = statements[0].split("UPSERT sgx_settlement_history:")[1].split(" ")[0]
        id2 = statements[1].split("UPSERT sgx_settlement_history:")[1].split(" ")[0]
        assert id1 == id2


class TestHistoryForTicker:
    def test_builds_select_scoped_to_ticker_newest_first(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(settlement_history, "query", lambda sql: captured.setdefault("sql", sql) or [])
        settlement_history.history_for_ticker("nk")
        sql = captured["sql"]
        assert "FROM sgx_settlement_history" in sql
        # Matches tickerComponents (not the raw ticker field), so "nk" also
        # finds a row archived under the compound ticker "NK/NKO" -- and is
        # upper-cased since components are stored upper-cased.
        assert "tickerComponents CONTAINS 'NK'" in sql
        assert "ORDER BY fspDate DESC" in sql
        assert "LIMIT 90" in sql

    def test_optional_source_filter(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(settlement_history, "query", lambda sql: captured.setdefault("sql", sql) or [])
        settlement_history.history_for_ticker("NK", source="flexc", limit=5)
        assert "source = 'flexc'" in captured["sql"]
        assert "LIMIT 5" in captured["sql"]

    def test_compound_ticker_input_matches_either_component(self, monkeypatch):
        # Regression: typing the exact compound ticker as SGX itself prints
        # it ("NK/NKO") -- e.g. a chat model reusing what a prior tool
        # result showed it -- used to match nothing at all, since the whole
        # unsplit string was compared against tickerComponents.
        captured = {}
        monkeypatch.setattr(settlement_history, "query", lambda sql: captured.setdefault("sql", sql) or [])
        settlement_history.history_for_ticker("NK/NKO")
        sql = captured["sql"]
        assert "tickerComponents CONTAINS 'NK'" in sql
        assert "tickerComponents CONTAINS 'NKO'" in sql
        assert " OR " in sql


class TestHistoryForDate:
    def test_builds_select_scoped_to_date(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(settlement_history, "query", lambda sql: captured.setdefault("sql", sql) or [])
        settlement_history.history_for_date(date(2026, 7, 10))
        sql = captured["sql"]
        assert "FROM sgx_settlement_history" in sql
        assert "fspDate = '2026-07-10'" in sql

    def test_optional_source_filter(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(settlement_history, "query", lambda sql: captured.setdefault("sql", sql) or [])
        settlement_history.history_for_date(date(2026, 7, 10), source="main")
        assert "source = 'main'" in captured["sql"]


class TestArchiveRange:
    def test_returns_earliest_and_latest_fspdate(self, monkeypatch):
        monkeypatch.setattr(
            settlement_history, "query",
            lambda sql: [{"fspDate": "2026-06-01"}, {"fspDate": "2026-07-15"}],
        )
        assert settlement_history.archive_range() == ("2026-06-01", "2026-07-15")

    def test_empty_archive_returns_none(self, monkeypatch):
        monkeypatch.setattr(settlement_history, "query", lambda sql: [])
        assert settlement_history.archive_range() is None

    def test_single_row_archive_returns_that_date_twice(self, monkeypatch):
        # Both ORDER BY ASC/DESC LIMIT 1 statements pick the same sole row.
        monkeypatch.setattr(settlement_history, "query", lambda sql: [{"fspDate": "2026-07-10"}] * 2)
        assert settlement_history.archive_range() == ("2026-07-10", "2026-07-10")

    def test_result_is_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            settlement_history, "query",
            lambda sql: calls.append(1) or [{"fspDate": "2026-06-01"}, {"fspDate": "2026-07-15"}],
        )
        settlement_history.archive_range()
        settlement_history.archive_range()
        assert len(calls) == 1
        settlement_history.archive_range(force=True)
        assert len(calls) == 2

    def test_db_error_propagates_not_swallowed(self, monkeypatch):
        from monitor.db import SurrealDBError

        def _raise(sql):
            raise SurrealDBError("connection refused")

        monkeypatch.setattr(settlement_history, "query", _raise)
        with pytest.raises(SurrealDBError):
            settlement_history.archive_range()
