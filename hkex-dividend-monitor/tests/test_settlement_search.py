import pytest

import monitor.settlement as settlement
import monitor.settlement_search as ss


@pytest.fixture(autouse=True)
def _clear_settlement_cache():
    settlement._CACHE.clear()
    yield
    settlement._CACHE.clear()


# ============================================================
# parse_expiry
# ============================================================


class TestParseExpiry:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("HSCEI may26 expiry level", "2026-05"),
            ("HSCEI May 2026 expiry level", "2026-05"),
            ("HSCEI may-26 expiry level", "2026-05"),
            ("HSCEI 2026-05 expiry level", "2026-05"),
            ("what did FDAX settle at in june 2026", "2026-06"),
            ("FESX december 2027", "2027-12"),
        ],
    )
    def test_recognizes_common_expiry_phrasings(self, query, expected):
        cleaned, expiry, parsed_date = ss.parse_expiry(query)
        assert expiry == expected
        assert expiry not in cleaned
        assert parsed_date is None  # none of these name a specific day

    def test_no_expiry_present(self):
        cleaned, expiry, parsed_date = ss.parse_expiry("HSCEI expiry level")
        assert expiry is None
        assert parsed_date is None
        assert cleaned == "HSCEI expiry level"

    def test_strips_only_the_expiry_phrase(self):
        cleaned, expiry, parsed_date = ss.parse_expiry("HSCEI may26 expiry level")
        assert expiry == "2026-05"
        assert "hscei" in cleaned.lower()
        assert "expiry" in cleaned.lower()
        assert "may" not in cleaned.lower()

    def test_full_iso_date_also_returns_parsed_date(self):
        cleaned, expiry, parsed_date = ss.parse_expiry("HSI 2026-05-15 settlement")
        assert expiry == "2026-05"
        assert parsed_date == "2026-05-15"
        assert "15" not in cleaned
        assert "hsi" in cleaned.lower()
        assert "settlement" in cleaned.lower()

    def test_invalid_calendar_date_keeps_expiry_but_drops_parsed_date(self):
        # "2026-02-30" isn't a real date (February never has 30 days) --
        # the expiry MONTH is still a legitimate read, but the DAY can't be.
        cleaned, expiry, parsed_date = ss.parse_expiry("HSI 2026-02-30 settlement")
        assert expiry == "2026-02"
        assert parsed_date is None


class TestParseExpiryYearWindow:
    """Regression: a plain day-of-month or a wildly off-base year used to
    be silently accepted as a real expiry (e.g. "March 15" -> year 2015,
    "June 30" -> year 2030), poisoning the resulting expiry_month/
    contract_month filter with a wrong month instead of finding nothing."""

    @pytest.mark.parametrize(
        "query",
        [
            "HSCEI expired March 15",  # day 15 read as year 2015
            "what did NK settle at on June 30",  # day 30 read as year 2030
            "HSI may 1926",
            "HSI may 2100",
        ],
    )
    def test_implausible_year_rejected(self, query):
        cleaned, expiry, parsed_date = ss.parse_expiry(query)
        assert expiry is None
        assert parsed_date is None
        assert cleaned == query  # nothing stripped -- the "expiry" was never accepted

    def test_four_digit_past_year_accepted_within_the_wider_window(self):
        # Live audit finding: "august 2022 hsi" used to be silently
        # rejected by the same narrow window a stray "March 15" needs --
        # a spelled-out 4-digit year is never a misread day number, so it
        # gets a much wider backward-looking window instead.
        cleaned, expiry, parsed_date = ss.parse_expiry("august 2022 hsi")
        assert expiry == "2022-08"

    def test_two_digit_year_window_is_unchanged(self):
        # "March 15" must stay rejected even though 2015 would fall inside
        # the WIDE (4-digit) window -- a 2-digit year keeps the narrow one.
        cleaned, expiry, parsed_date = ss.parse_expiry("HSCEI expired March 15")
        assert expiry is None


# ============================================================
# _classify_hkex_variant
# ============================================================


class TestClassifyHkexVariant:
    def test_monthly_combo_is_main(self):
        variant, main = ss._classify_hkex_variant(
            "Hang Seng China Enterprises Index / Mini-Hang Seng China Enterprises Index Futures & Options",
            "Equity Index",
        )
        assert main is True
        assert variant == "monthly futures & options"

    def test_weekly_options_not_main(self):
        variant, main = ss._classify_hkex_variant("Weekly Hang Seng China Enterprises Index Options", "Equity Index")
        assert main is False
        assert variant == "weekly options"

    def test_weekly_in_middle_of_name_is_still_weekly(self):
        # Regression: the old check only recognized "Weekly " as a name
        # PREFIX, so a mid-name occurrence fell through unclassified (and
        # wrongly ended up `main=True`, tying its own monthly contract).
        variant, main = ss._classify_hkex_variant("NSE IFSC Nifty 50 Index Weekly Options", "Equity Index")
        assert main is False
        assert variant == "weekly options"

    def test_futures_options_not_main(self):
        variant, main = ss._classify_hkex_variant("Hang Seng China Enterprises Index Futures Options", "Equity Index")
        assert main is False
        assert variant == "futures options"

    def test_total_return_variants_not_main(self):
        net_variant, net_main = ss._classify_hkex_variant(
            "Hang Seng China Enterprises Index (Net Total Return Index) Futures", "Equity Index"
        )
        gross_variant, gross_main = ss._classify_hkex_variant(
            "Hang Seng China Enterprises Index (Gross Total Return Index) Futures", "Equity Index"
        )
        assert net_main is False and net_variant == "net total return"
        assert gross_main is False and gross_variant == "gross total return"

    def test_dividend_point_not_main(self):
        variant, main = ss._classify_hkex_variant("HSCEI Dividend Point Index Futures", "Equity Index")
        assert main is False
        assert variant == "dividend point"

    def test_etf_not_main_even_when_producttype_is_not_equity_index(self):
        # Real HKEX data quirk: this contract's own productType is "Stock
        # Futures", not "Equity Index" -- the name pattern must still win,
        # or this card wrongly ties the real monthly futures contract as
        # `main` (the actual bug this classifier order fix addresses).
        variant, main = ss._classify_hkex_variant("Hang Seng China Enterprises Index ETF", "Stock Futures")
        assert main is False
        assert variant == "etf"

    def test_single_stock_futures_is_main(self):
        variant, main = ss._classify_hkex_variant("Tencent Holdings Ltd.", "Stock Futures")
        assert main is True
        assert variant == "stock futures"


class TestClassifySgxVariant:
    def test_standard_contract_is_main(self):
        variant, main = ss._classify_sgx_variant("SGX Nikkei 225 Index (SGX Nikkei 225) Futures / Options")
        assert main is True
        assert variant == "standard"

    def test_climate_variant_not_main(self):
        variant, main = ss._classify_sgx_variant("SGX Nikkei 225 Climate PAB Futures")
        assert main is False

    def test_total_return_variant_not_main(self):
        variant, main = ss._classify_sgx_variant("SGX Nikkei 225 Index Total Return Futures")
        assert main is False
        assert variant == "total return"

    def test_dividend_point_variant_not_main(self):
        variant, main = ss._classify_sgx_variant("SGX Nikkei Stock Average Dividend Point Index Futures")
        assert main is False
        assert variant == "dividend point"

    def test_micro_variant_not_main(self):
        variant, main = ss._classify_sgx_variant("SGX Micro Nikkei 225 Index Futures")
        assert main is False

    def test_esg_reit_variant_not_main(self):
        variant, main = ss._classify_sgx_variant("SGX Nikkei ESG-REIT Index Futures")
        assert main is False

    def test_currency_variant_still_main(self):
        # A USD-denominated contract is a legitimate alternative, not an
        # inferior/niche variant -- unlike the ESG/climate/micro/dividend-
        # point siblings, it should NOT be demoted.
        variant, main = ss._classify_sgx_variant("SGX USD Nikkei 225 Index Futures")
        assert main is True

    def test_weekly_variant_not_main(self):
        # Regression: live incident -- "NSE IFSC Nifty 50 Index Weekly
        # Options" was left classified `main=True`, tying its own monthly
        # contract's score with no signal to prefer one over the other.
        variant, main = ss._classify_sgx_variant("NSE IFSC Nifty 50 Index Weekly Options")
        assert main is False
        assert variant == "weekly"


# ============================================================
# build_contract_cards
# ============================================================

_HKEX_ROWS = [
    {
        "contract": "Hang Seng China Enterprises Index / Mini-Hang Seng China Enterprises Index Futures & Options",
        "hkatsCode": "HHI/MCH", "productType": "Equity Index",
    },
    {"contract": "Weekly Hang Seng China Enterprises Index Options", "hkatsCode": "HHI", "productType": "Equity Index"},
    {"contract": "Hang Seng China Enterprises Index ETF", "hkatsCode": "HCF", "productType": "Stock Futures"},
    {"contract": "Tencent Holdings Ltd.", "hkatsCode": "TCH", "productType": "Stock Futures"},
    {"contract": "CK Hutchison Holdings Ltd.", "hkatsCode": "CKH", "productType": "Stock Futures"},
]

_SGX_ROWS = [
    {"contract": "SGX Nikkei 225 Index (SGX Nikkei 225) Futures / Options", "ticker": "NK/NKO",
     "productType": "Equity Index", "sheet": "Financials Contracts", "contractMonth": "2026-07-01"},
    {"contract": "SGX USD Nikkei 225 Index Futures", "ticker": "NU", "productType": "Equity Index",
     "sheet": "Financials Contracts", "contractMonth": "2026-06-01"},
    {"contract": "SGX Nikkei 225 Climate PAB Futures", "ticker": "NC", "productType": "Equity Index",
     "sheet": "Financials Contracts", "contractMonth": "2026-07-01"},
]

_EUREX_PRODUCTS = [
    {"code": "FDAX", "name": "DAX® Futures", "group": "Equity Index", "currency": "EUR"},
    {"code": "F7GS", "name": "iShares Core DAX® UCITS (DE) Futures", "group": "Equity Index", "currency": "EUR"},
    {"code": "FESX", "name": "EURO STOXX 50® Index Futures", "group": "Equity Index", "currency": "EUR"},
    {"code": "ZZZZ", "name": "Unseeded Test Product", "group": "Equity Index", "currency": "EUR"},
    # Same real MSCI World index as _MSCI_ROWS below, also listed in the
    # general catalog (unresolved) -- reproduces the live incident where
    # this beat the purpose-built, no-resolution-needed eurex_msci card.
    {"code": "FMWO", "name": "MSCI World Price Index Futures", "group": "Equity Index", "currency": "USD"},
]

_MSCI_ROWS = [
    {"indexName": "MSCI World", "eurexCode": "FMWO", "indexType": "Standard", "currency": "USD"},
    {"indexName": "MSCI World", "eurexCode": "FMWN", "indexType": "Standard", "currency": "EUR"},
]


def _patch_all_sources(monkeypatch):
    monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": _HKEX_ROWS})
    monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": _SGX_ROWS})
    monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: _EUREX_PRODUCTS)
    monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": _MSCI_ROWS})
    monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})


class TestBuildContractCards:
    def test_assembles_cards_from_all_four_sources(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        data = ss.build_contract_cards()
        exchanges = {c["exchange"] for c in data["cards"]}
        assert exchanges == {"HKEX", "SGX", "Eurex"}
        assert data["sourcesFailed"] == []

    def test_hkex_cards_dedupe_by_contract_name(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        data = ss.build_contract_cards()
        hkex_names = [c["name"] for c in data["cards"] if c["exchange"] == "HKEX"]
        assert len(hkex_names) == len(set(hkex_names))

    def test_eurex_products_carry_resolved_flag(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        data = ss.build_contract_cards()
        by_code = {c["codes"][0]: c for c in data["cards"] if c["source"] == "eurex_products"}
        assert by_code["FDAX"]["resolved"] is True  # in the real seed map
        assert by_code["ZZZZ"]["resolved"] is False
        assert "note" in by_code["ZZZZ"]
        assert "note" not in by_code["FDAX"]

    def test_one_source_failing_does_not_blank_the_others(self, monkeypatch):
        _patch_all_sources(monkeypatch)

        def _raise():
            raise settlement.SettlementError("Eurex catalog down")

        monkeypatch.setattr(settlement, "fetch_eurex_products", _raise)
        data = ss.build_contract_cards()
        sources = {c["source"] for c in data["cards"]}
        # eurex_products failed, but hkex_fsp/sgx_fsp/eurex_msci (a
        # separate fetch) are unaffected.
        assert sources == {"hkex_fsp", "sgx_fsp", "eurex_msci"}
        assert len(data["sourcesFailed"]) == 1
        assert "Eurex catalog down" in data["sourcesFailed"][0]

    def test_msci_cards_fold_currency_into_name_for_disambiguation(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        data = ss.build_contract_cards()
        msci_names = {c["name"] for c in data["cards"] if c["source"] == "eurex_msci"}
        assert msci_names == {"MSCI World Futures (USD)", "MSCI World Futures (EUR)"}


class TestBuildContractCardsParallelism:
    """_build_contract_cards_impl runs its four independent sources
    concurrently (measured live: ~20s cold serial, dominated by per-source
    network latency) -- these verify the concurrency is real, and that it
    doesn't sacrifice deterministic ordering or failure capture to get it."""

    def test_builders_run_concurrently_not_serially(self, monkeypatch):
        import time as _time

        def slow(_n=[0]):
            _time.sleep(0.05)
            return []

        monkeypatch.setattr(
            ss, "_CARD_SOURCES",
            (("A", slow), ("B", slow), ("C", slow), ("D", slow)),
        )
        start = _time.monotonic()
        ss._build_contract_cards_impl()
        elapsed = _time.monotonic() - start
        # Serial would take ~0.20s (4 x 0.05s); concurrent should land
        # close to a single 0.05s slot -- generous headroom for CI jitter.
        assert elapsed < 0.15

    def test_order_is_deterministic_regardless_of_completion_order(self, monkeypatch):
        import time as _time

        def slow_first():
            _time.sleep(0.05)
            return [{"exchange": "HKEX", "name": "slow-first"}]

        def fast_second():
            return [{"exchange": "SGX", "name": "fast-second"}]

        monkeypatch.setattr(ss, "_CARD_SOURCES", (("A", slow_first), ("B", fast_second)))
        data = ss._build_contract_cards_impl()
        # A was submitted first and finishes LAST (it sleeps) -- the
        # assembled list must still start with A's card, not whichever
        # source actually completed first.
        assert [c["name"] for c in data["cards"]] == ["slow-first", "fast-second"]

    def test_one_slow_failing_source_does_not_block_or_blank_the_others(self, monkeypatch):
        import time as _time

        def slow_fail():
            _time.sleep(0.05)
            raise settlement.SettlementError("Eurex catalog down")

        monkeypatch.setattr(
            ss, "_CARD_SOURCES",
            (("HKEX", lambda: [{"exchange": "HKEX", "name": "ok"}]), ("Eurex products", slow_fail)),
        )
        data = ss._build_contract_cards_impl()
        assert data["cards"] == [{"exchange": "HKEX", "name": "ok"}]
        assert len(data["sourcesFailed"]) == 1
        assert "Eurex catalog down" in data["sourcesFailed"][0]


class TestBuildContractCardsFailureRetry:
    """Regression: a build with a sourcesFailed used to sit cached for the
    full ~1h TTL, same as a healthy build -- live-confirmed twice,
    independently, that a single transient outage poisons retrieval for
    up to an hour that way."""

    def _counting_impl(self, monkeypatch):
        calls: list[int] = []
        real_impl = ss._build_contract_cards_impl

        def counting_impl():
            calls.append(1)
            return real_impl()

        monkeypatch.setattr(ss, "_build_contract_cards_impl", counting_impl)
        return calls

    def test_healthy_build_uses_the_full_ttl(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        calls = self._counting_impl(monkeypatch)
        t = [1000.0]
        monkeypatch.setattr(settlement.time, "monotonic", lambda: t[0])

        ss.build_contract_cards()
        t[0] += ss._FAILED_BUILD_RETRY_SECONDS + 1
        ss.build_contract_cards()  # short window elapsed, but this build was healthy
        assert len(calls) == 1

    def test_failed_build_is_retried_after_the_short_window(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        monkeypatch.setattr(
            settlement, "fetch_eurex_products",
            lambda: (_ for _ in ()).throw(settlement.SettlementError("Eurex catalog down")),
        )
        calls = self._counting_impl(monkeypatch)
        t = [1000.0]
        monkeypatch.setattr(settlement.time, "monotonic", lambda: t[0])

        ss.build_contract_cards()
        assert len(calls) == 1
        t[0] += ss._FAILED_BUILD_RETRY_SECONDS + 1
        ss.build_contract_cards()
        assert len(calls) == 2  # retried, unlike a healthy build

    def test_failed_build_is_not_retried_before_the_short_window(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        monkeypatch.setattr(
            settlement, "fetch_eurex_products",
            lambda: (_ for _ in ()).throw(settlement.SettlementError("Eurex catalog down")),
        )
        calls = self._counting_impl(monkeypatch)
        t = [1000.0]
        monkeypatch.setattr(settlement.time, "monotonic", lambda: t[0])

        ss.build_contract_cards()
        t[0] += 10  # well within the short retry window
        ss.build_contract_cards()
        assert len(calls) == 1

    def test_explicit_force_still_rebuilds_a_healthy_cache(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        calls = self._counting_impl(monkeypatch)
        ss.build_contract_cards()
        ss.build_contract_cards(force=True)
        assert len(calls) == 2

    def test_hkex_card_fetch_params_use_contract_name_not_code(self, monkeypatch):
        # Regression: fetching a card by its HKATS code pulled in
        # sibling-variant rows sharing that code (e.g. weekly options share
        # "HHI" with the monthly HHI/MCH contract -- verified live: fetching
        # "HSI" by code returned 24 weekly + only 6 monthly rows). Fetching
        # by the card's own exact contract name returns only that card's
        # own rows.
        _patch_all_sources(monkeypatch)
        data = ss.build_contract_cards()
        monthly = next(
            c for c in data["cards"]
            if c["exchange"] == "HKEX" and c["name"].startswith("Hang Seng China Enterprises Index /")
        )
        assert monthly["fetch"] == {
            "tool": "get_hkex_settlement_prices",
            "params": {"contract": monthly["name"]},
        }


class TestClassifyEurexVariant:
    def test_option_group_not_main(self):
        assert ss._classify_eurex_variant("SINGLE STOCK OPTIONS") == ("options", False)
        assert ss._classify_eurex_variant("INDEX OPTIONS") == ("options", False)

    def test_dividend_group_not_main(self):
        assert ss._classify_eurex_variant("SINGLE STOCK DIVIDEND FUTURES") == ("dividend", False)

    def test_total_return_group_not_main(self):
        assert ss._classify_eurex_variant("EQUITY TOTAL RETURN FUTURES") == ("total return", False)

    def test_plain_futures_group_is_main(self):
        variant, main = ss._classify_eurex_variant("SINGLE STOCK FUTURES")
        assert main is True

    def test_empty_group_is_main(self):
        variant, main = ss._classify_eurex_variant("")
        assert main is True


class TestEurexCardDeduplication:
    """Regression: the general Eurex catalog lists the same company/index
    name more than once across its futures/options/dividend/total-return
    siblings -- live-confirmed 621 duplicate (exchange, name) pairs, e.g.
    "Ferrari" x3. Only MSCI's cards were ever disambiguated; the general
    catalog's own duplicates were left as-is."""

    _FERRARI_PRODUCTS = [
        {"code": "2FE", "name": "Ferrari", "group": "SINGLE STOCK OPTIONS", "currency": "EUR"},
        {"code": "RACF", "name": "Ferrari", "group": "SINGLE STOCK FUTURES", "currency": "EUR"},
        {"code": "T2FE", "name": "Ferrari", "group": "EQUITY TOTAL RETURN FUTURES", "currency": "EUR"},
    ]

    def test_duplicate_names_disambiguated_by_product_type(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: self._FERRARI_PRODUCTS)
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})
        cards = ss._eurex_cards()
        names = {c["name"] for c in cards}
        assert names == {
            "Ferrari (SINGLE STOCK OPTIONS)",
            "Ferrari (SINGLE STOCK FUTURES)",
            "Ferrari (EQUITY TOTAL RETURN FUTURES)",
        }

    def test_disambiguated_cards_are_independently_findable(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: self._FERRARI_PRODUCTS)
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})
        result = ss.search_contracts("ferrari futures")
        assert result["matches"][0]["codes"] == ["RACF"]  # the plain futures sibling, not options/TRF

    def test_non_colliding_name_is_left_unchanged(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [self._FERRARI_PRODUCTS[0]])
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})
        cards = ss._eurex_cards()
        assert cards[0]["name"] == "Ferrari"


class TestMsciCardDisambiguation:
    """Regression: MSCI's workbook can list more than one distinct,
    differently-coded contract under the same (indexName, currency) pair
    even after folding currency into the display name -- e.g. a Net Total
    Return series and a Price-return series both named "MSCI World Futures
    (USD)" (verified live across 7 real index/currency pairs). A retrieval
    match must never be ambiguous between two such real, differently-coded
    contracts.
    """

    _COLLIDING_ROWS = [
        {"indexName": "MSCI World", "eurexCode": "FMWO", "indexType": "Standard", "currency": "USD",
         "dividendReinvestment": "NTR"},
        {"indexName": "MSCI World", "eurexCode": "FMWP", "indexType": "Standard", "currency": "USD",
         "dividendReinvestment": "Price"},
    ]

    def test_colliding_names_are_disambiguated_by_dividend_reinvestment(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": self._COLLIDING_ROWS})
        cards = ss._eurex_msci_cards()
        names = {c["name"] for c in cards}
        assert names == {"MSCI World Futures (USD, NTR)", "MSCI World Futures (USD, Price)"}

    def test_disambiguated_cards_are_independently_findable(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": self._COLLIDING_ROWS})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})
        result = ss.search_contracts("MSCI World USD NTR futures")
        assert result["matches"]
        assert result["matches"][0]["codes"] == ["FMWO"]

    def test_non_colliding_name_is_left_unchanged(self, monkeypatch):
        # Only groups that actually collide get a suffix folded in.
        monkeypatch.setattr(
            settlement, "fetch_eurex_msci_fsp",
            lambda: {"rows": [self._COLLIDING_ROWS[0]]},
        )
        cards = ss._eurex_msci_cards()
        assert cards[0]["name"] == "MSCI World Futures (USD)"

    def test_trailing_whitespace_in_index_name_is_squashed(self, monkeypatch):
        # Live data quirk: "MSCI Emerging Markets " (indexName) carries a
        # trailing space that would otherwise leak into the display name.
        monkeypatch.setattr(
            settlement, "fetch_eurex_msci_fsp",
            lambda: {"rows": [{
                "indexName": "MSCI Emerging Markets ", "eurexCode": "FMEM",
                "indexType": "Standard", "currency": "USD", "dividendReinvestment": "NTR",
            }]},
        )
        cards = ss._eurex_msci_cards()
        assert cards[0]["name"] == "MSCI Emerging Markets Futures (USD)"


class TestMsciVariantClassification:
    """Regression: every MSCI card was hardcoded main=True (no classifier
    the way HKEX/SGX have one) -- live-confirmed this let the model
    fabricate an unfounded "the standard MSCI World contract is the EUR
    NTR variant" for a query naming no currency/return-type at all."""

    def test_ntr_usd_is_main_within_a_multi_member_group(self, monkeypatch):
        rows = [
            {"indexName": "MSCI World", "eurexCode": "FMWN", "indexType": "Standard", "currency": "EUR",
             "dividendReinvestment": "NTR"},
            {"indexName": "MSCI World", "eurexCode": "FMWO", "indexType": "Standard", "currency": "USD",
             "dividendReinvestment": "NTR"},
            {"indexName": "MSCI World", "eurexCode": "FMWP", "indexType": "Standard", "currency": "USD",
             "dividendReinvestment": "Price"},
        ]
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": rows})
        cards = {c["codes"][0]: c for c in ss._eurex_msci_cards()}
        assert cards["FMWO"]["main"] is True   # NTR, USD
        assert cards["FMWN"]["main"] is False  # NTR, but not USD
        assert cards["FMWP"]["main"] is False  # not NTR at all

    def test_sole_card_in_a_group_is_main_even_without_ntr(self, monkeypatch):
        rows = [{"indexName": "MSCI Emerging Markets", "eurexCode": "FMEM", "indexType": "Standard", "currency": "USD"}]
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": rows})
        cards = ss._eurex_msci_cards()
        assert cards[0]["main"] is True

    def test_no_ntr_in_a_multi_member_group_has_no_main(self, monkeypatch):
        # The exact live shape that produced the fabrication: two currency
        # variants, neither one an NTR series -- no honest way to call
        # either "the standard" one.
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": _MSCI_ROWS})
        cards = ss._eurex_msci_cards()
        assert all(c["main"] is False for c in cards)

    def test_style_variant_never_main_even_as_sole_group_member(self, monkeypatch):
        rows = [{"indexName": "MSCI World SRI", "eurexCode": "FMRW", "indexType": "ESG / SRI", "currency": "USD",
                 "dividendReinvestment": "NTR"}]
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": rows})
        cards = ss._eurex_msci_cards()
        assert cards[0]["main"] is False

    def test_legend_row_produces_no_card(self, monkeypatch):
        rows = [
            {"indexName": "* DM = Developed Markets / EM = Emerging Markets Futures",
             "eurexCode": None, "indexType": "", "currency": ""},
            {"indexName": "MSCI World", "eurexCode": "FMWO", "indexType": "Standard", "currency": "USD"},
        ]
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": rows})
        cards = ss._eurex_msci_cards()
        assert len(cards) == 1
        assert cards[0]["codes"] == ["FMWO"]


# ============================================================
# search_contracts
# ============================================================


class TestSearchContracts:
    def test_exact_code_beats_everything(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("TCH")
        assert result["matches"][0]["codes"] == ["TCH"]

    def test_code_not_shadowed_by_accidental_name_substring(self, monkeypatch):
        # "TCH" is a substring of "hutchison" -- must not let CK Hutchison
        # outrank (or even tie) the card actually coded TCH.
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("TCH")
        top_codes = [m["codes"] for m in result["matches"][:1]]
        assert top_codes == [["TCH"]]

    def test_hscei_alias_top_ranks_the_main_monthly_contract(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("HSCEI may26 expiry level")
        assert result["parsedExpiry"] == "2026-05"
        top = result["matches"][0]
        assert top["main"] is True
        assert top["codes"] == ["HHI", "MCH"]

    def test_etf_variant_does_not_tie_the_main_contract_despite_producttype_quirk(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("HSCEI")
        by_name = {m["name"]: m["score"] for m in result["matches"]}
        assert by_name["Hang Seng China Enterprises Index / Mini-Hang Seng China Enterprises Index Futures & Options"] > by_name["Hang Seng China Enterprises Index ETF"]

    def test_precision_prefers_exact_short_name_over_decoy_long_name(self, monkeypatch):
        # "DAX futures" must resolve to the real DAX Futures contract, not
        # the unrelated iShares ETF-tracking product that also contains
        # both words -- this is the actual bug this scoring term fixes.
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("DAX futures")
        assert result["matches"][0]["codes"] == ["FDAX"]

    def test_ready_to_use_msci_card_beats_unresolved_catalog_duplicate(self, monkeypatch):
        # Regression: the same MSCI World index is also a general Eurex
        # catalog entry (unresolved -- needs a manual URL-paste step). Live
        # incident: the model followed the catalog card and told the user
        # to do a manual resolve, when the purpose-built eurex_msci card
        # (get_eurex_msci_fsp, no resolution needed at all) was right there.
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("MSCI World futures final settlement price")
        top = result["matches"][0]
        assert top["source"] == "eurex_msci"
        assert top["fetch"]["tool"] == "get_eurex_msci_fsp"

    def test_nonsense_query_returns_no_matches(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("qwzxjk flibbergast noodleplex")
        assert result["matches"] == []

    def test_main_bonus_never_applies_without_another_signal(self, monkeypatch):
        # Regression: `main` used to be added unconditionally, so every
        # main-flagged card in the whole catalog scored >0 (and was
        # returned as a plausible match) even when nothing else matched.
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("completely unrelated gibberish")
        assert result["matches"] == []

    def test_sgx_contract_reachable_by_ticker(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("NU")
        assert result["matches"][0]["exchange"] == "SGX"
        assert result["matches"][0]["codes"] == ["NU"]

    def test_sgx_compound_ticker_splits_for_exact_match(self, monkeypatch):
        # Regression: SGX's "NK/NKO" compound ticker wasn't split, so a
        # bare "NK" query matched nothing at all (same class of bug the
        # HKEX compound-HKATS-code fix addressed).
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("NK")
        assert result["matches"]
        assert result["matches"][0]["codes"] == ["NK", "NKO"]

    def test_sgx_non_standard_variant_ranks_below_standard_contract(self, monkeypatch):
        # Regression: "Nikkei 225" live-top-ranked the Climate PAB variant
        # over the standard contract with no signal to prefer one.
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("Nikkei 225")
        by_ticker = {m["codes"][0]: m for m in result["matches"]}
        assert by_ticker["NK"]["main"] is True
        assert by_ticker["NC"]["main"] is False
        assert by_ticker["NK"]["score"] > by_ticker["NC"]["score"]

    def test_fetch_field_carries_the_right_tool_and_params(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("FDAX")
        top = result["matches"][0]
        assert top["fetch"] == {"tool": "get_eurex_settlement_prices", "params": {"product_code": "FDAX"}}

    def test_unresolved_eurex_product_surfaces_resolve_note(self, monkeypatch):
        _patch_all_sources(monkeypatch)
        result = ss.search_contracts("ZZZZ")
        top = result["matches"][0]
        assert top["resolved"] is False
        assert "resolve" in top["note"].lower()


class TestSearchContractsAmbiguousCodes:
    """Regression: several real Eurex product codes are common English
    words (verified live against the actual catalog: Air Liquide=AIR,
    Allreal=ALL, Canal+=CAN, Forbo=FOR, Getinge=GET, Nemetschek=NET,
    Pernod-Ricard=PER, Sulzer=SUN). Ordinary lowercase prose containing
    that word must not credit the unrelated company with an exact-code
    match -- but a deliberate ALL-CAPS code, bare or amid other words,
    still must."""

    _PRODUCTS = [
        {"code": "FOR", "name": "Forbo Holding", "group": "Equities", "currency": "CHF"},
        {"code": "FDAX", "name": "DAX® Futures", "group": "Equity Index", "currency": "EUR"},
    ]

    def _patch(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: self._PRODUCTS)
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_lowercase_prose_does_not_credit_ambiguous_code(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("what did FDAX settle at for the may expiry")
        assert result["matches"][0]["codes"] == ["FDAX"]  # not Forbo, despite "for" in the prose

    def test_bare_uppercase_code_still_resolves(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("FOR")
        assert result["matches"][0]["codes"] == ["FOR"]

    def test_deliberate_uppercase_code_amid_other_words_still_credited(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("settlement price FOR")
        assert result["matches"][0]["codes"] == ["FOR"]

    def _patch_give_and_nikkei(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(
            settlement, "fetch_sgx_fsp",
            lambda: {"rows": [{"contract": "SGX Nikkei 225 Index Futures", "ticker": "NK", "productType": "Equity Index"}]},
        )
        monkeypatch.setattr(
            settlement, "fetch_eurex_products",
            lambda: [{"code": "GIVE", "name": "Givaudan", "group": "Equities", "currency": "CHF"}],
        )
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_all_caps_query_does_not_credit_ambiguous_code(self, monkeypatch):
        # Live-confirmed: a query typed ENTIRELY IN CAPS used to make every
        # ambiguous-word code satisfy a bare case check by coincidence of
        # shouting-case, hijacking an unrelated query.
        self._patch_give_and_nikkei(monkeypatch)
        result = ss.search_contracts("GIVE ME THE NIKKEI 225 SETTLEMENT PRICE")
        assert result["matches"][0]["exchange"] == "SGX"
        assert result["matches"][0]["codes"] == ["NK"]

    def test_give_me_nikkei_prose_resolves_nikkei_not_givaudan(self, monkeypatch):
        self._patch_give_and_nikkei(monkeypatch)
        result = ss.search_contracts("give me the nikkei 225 settlement price")
        assert result["matches"][0]["codes"] == ["NK"]

    def test_lowercase_bare_code_resolves(self, monkeypatch):
        # A case-sensitive-only guard could never satisfy this -- the code
        # is always compared uppercase, so a genuinely-typed lowercase code
        # ("sun" for Sunac's own HKATS code, live example) needs the
        # single-token arm of the deliberate-code check.
        self._patch(monkeypatch)  # reuses the FOR/FDAX fixture from setUp-style helper above
        result = ss.search_contracts("for")
        assert result["matches"][0]["codes"] == ["FOR"]

    def test_us_dollar_nikkei_not_hijacked_by_us_code(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(
            settlement, "fetch_sgx_fsp",
            lambda: {
                "rows": [
                    {"contract": "SGX Nikkei 225 Index Futures", "ticker": "NK", "productType": "Equity Index"},
                    {"contract": "SGX USD/SGD Futures", "ticker": "US", "productType": "FX"},
                ]
            },
        )
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})
        result = ss.search_contracts("us dollar nikkei")
        assert result["matches"][0]["codes"] == ["NK"]


class TestSearchContractsCodeSuppression:
    """Regression: an exact-code match on a non-main variant card can still
    be the WRONG contract for a multi-word query -- live-confirmed: "china
    a50 futures" exact-code-matched an HKEX ETF-futures card (code "A50")
    over the real SGX A50 index future, by a ~6x score margin, because
    "A50" isn't an English word so it bypasses the ambiguous-code guard
    entirely. The +100 code bonus must be withheld for a non-main card
    when the query has another plausible reading and doesn't name this
    specific variant."""

    def _patch(self, monkeypatch):
        monkeypatch.setattr(
            settlement, "fetch_hkex_fsp",
            lambda: {
                "rows": [
                    {"contract": "iShares FTSE A50 China Index ETF", "hkatsCode": "A50", "productType": "Stock Futures"},
                ]
            },
        )
        monkeypatch.setattr(
            settlement, "fetch_sgx_fsp",
            lambda: {
                "rows": [
                    {"contract": "SGX FTSE China A50 Index Futures", "ticker": "CN", "productType": "Equity Index"},
                ]
            },
        )
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_multi_word_query_prefers_the_real_index_future_over_the_etf(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("china a50 futures")
        assert result["matches"][0]["exchange"] == "SGX"
        assert result["matches"][0]["codes"] == ["CN"]

    def test_bare_code_query_still_resolves_the_etf(self, monkeypatch):
        # A single-token query has no "other plausible reading" to prefer
        # -- suppression must not apply, or a genuine bare-code lookup
        # (the audit's own explicit requirement) would stop working.
        self._patch(monkeypatch)
        result = ss.search_contracts("A50")
        assert result["matches"][0]["codes"] == ["A50"]

    def test_query_naming_the_variant_still_credits_the_matching_card(self, monkeypatch):
        # If the query DOES name the variant the card represents, the code
        # match is plausibly deliberate even for a multi-word query.
        self._patch(monkeypatch)
        result = ss.search_contracts("a50 etf")
        assert result["matches"][0]["codes"] == ["A50"]


class TestSearchContractsVariantPoolRestriction:
    """Regression: an explicit-variant query must not let the flagship
    monthly contract win purely on raw score -- live-confirmed "hsi
    dividend point futures" ranked the main HSI/MHI combo card ~150 points
    above the actual HSI Dividend Point Index Futures card, with the
    system prompt then telling the model to answer with whichever match
    is `main`."""

    def _patch(self, monkeypatch):
        monkeypatch.setattr(
            settlement, "fetch_hkex_fsp",
            lambda: {
                "rows": [
                    {"contract": "Hang Seng Index / Mini-Hang Seng Index Futures & Options",
                     "hkatsCode": "HSI/MHI", "productType": "Equity Index"},
                    {"contract": "HSI Dividend Point Index Futures", "hkatsCode": "DHS", "productType": "Equity Index"},
                    {"contract": "Hang Seng Index (Net Total Return Index) Futures",
                     "hkatsCode": "HNT", "productType": "Equity Index"},
                    {"contract": "Weekly Hang Seng Index Options", "hkatsCode": "HSI", "productType": "Equity Index"},
                ]
            },
        )
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_dividend_point_query_prefers_the_dividend_point_card(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("hsi dividend point futures")
        assert result["matches"][0]["codes"] == ["DHS"]

    def test_net_total_return_query_prefers_the_ntr_card(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("hsi net total return")
        assert result["matches"][0]["codes"] == ["HNT"]

    def test_plain_query_prefers_the_main_monthly_contract(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("hsi futures")
        assert set(result["matches"][0]["codes"]) == {"HSI", "MHI"}

    def test_weekly_options_query_prefers_the_weekly_card(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("hsi weekly options")
        assert result["matches"][0]["variant"] == "weekly options"

    def test_mini_still_reaches_the_combo_card_via_its_name(self, monkeypatch):
        # "mini" lives only in the combo card's NAME (not a distinct
        # variant string -- see _classify_hkex_variant), so the pool
        # restriction's name-matching arm must still keep it reachable.
        self._patch(monkeypatch)
        result = ss.search_contracts("mini hang seng")
        assert set(result["matches"][0]["codes"]) == {"HSI", "MHI"}


class TestSearchContractsPhraseBonus:
    """Regression: a natural-language query like "settlement price of the
    hang seng index" top-ranked an unrelated same-family index (Hang Seng
    BIOTECH Index) over the actual Hang Seng Index contract -- word-token
    overlap alone scores both almost identically since both names share
    most of the query's words; only a card whose name contains the
    query's content words CONTIGUOUSLY should get the extra credit that
    breaks the tie in the right direction."""

    _ROWS = [
        {"contract": "Hang Seng Index / Mini-Hang Seng Index Futures & Options", "hkatsCode": "HSI / MHI",
         "productType": "Equity Index"},
        {"contract": "Hang Seng Biotech Index Futures", "hkatsCode": "HBI", "productType": "Equity Index"},
    ]

    def _patch(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": self._ROWS})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_natural_phrase_prefers_the_named_index_over_a_same_family_decoy(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("settlement price of the hang seng index")
        assert result["matches"][0]["codes"] == ["HSI", "MHI"]

    def test_scattered_word_overlap_alone_does_not_win(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("settlement price of the hang seng index")
        by_code = {tuple(m["codes"]): m["score"] for m in result["matches"]}
        assert by_code[("HSI", "MHI")] > by_code[("HBI",)]


class TestSearchContractsHangSengReverseAlias:
    """Regression: "hang seng" has no entry in _HKEX_INDEX_ALIASES (keyed
    by abbreviation, not full wording) -- live-confirmed the bare query
    top-ranked Hang Seng BANK (a real, unrelated stock futures contract)
    over the Hang Seng INDEX, since the bank's short official name scores
    higher on precision than the index's long combo name."""

    _ROWS = [
        {"contract": "Hang Seng Index / Mini-Hang Seng Index Futures & Options", "hkatsCode": "HSI/MHI",
         "productType": "Equity Index"},
        {"contract": "Hang Seng Bank Ltd.", "hkatsCode": "HSB", "productType": "Stock Futures"},
        {"contract": "HSI Dividend Point Index Futures", "hkatsCode": "DHS", "productType": "Equity Index"},
        {"contract": "Hang Seng China Enterprises Index / Mini-Hang Seng China Enterprises Index Futures & Options",
         "hkatsCode": "HHI/MCH", "productType": "Equity Index"},
    ]

    def _patch(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": self._ROWS})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_bare_query_prefers_the_index_over_the_bank(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("hang seng")
        assert set(result["matches"][0]["codes"]) == {"HSI", "MHI"}

    def test_query_naming_the_bank_still_finds_the_bank(self, monkeypatch):
        # The block must not make the bank unreachable -- only stop it
        # from being SHADOWED by the index alias on a bare "hang seng".
        self._patch(monkeypatch)
        result = ss.search_contracts("hang seng bank")
        assert result["matches"][0]["codes"] == ["HSB"]

    def test_full_wording_reaches_a_card_named_with_the_bare_abbreviation(self, monkeypatch):
        # "HSI Dividend Point Index Futures" is named with the bare
        # abbreviation, not the full wording the alias table expands to --
        # the card-side augmentation is what lets a full-wording query
        # ("hang seng dividend point") still reach it.
        self._patch(monkeypatch)
        result = ss.search_contracts("hang seng dividend point")
        assert result["matches"][0]["codes"] == ["DHS"]

    def test_more_specific_phrase_is_not_shadowed_by_the_bare_fallback(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("hang seng china enterprises")
        assert set(result["matches"][0]["codes"]) == {"HHI", "MCH"}


class TestSearchContractsGuidanceNotes:
    """Regression: a query that legitimately yields zero usable tokens
    (an expiry with nothing else, or a non-Latin script) used to just
    return an empty match list with no hint -- indistinguishable from
    "genuinely no such contract exists"."""

    def _patch(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_expiry_only_query_gets_a_guidance_note(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("may26")
        assert result["matches"] == []
        assert result["parsedExpiry"] == "2026-05"
        assert "note" in result
        assert "expiry" in result["note"].lower()

    def test_non_latin_query_gets_a_different_guidance_note(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("恒生指数")
        assert result["matches"] == []
        assert result["parsedExpiry"] is None
        assert "note" in result
        assert "english" in result["note"].lower()

    def test_ordinary_no_match_query_gets_no_note(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("qwzxjk flibbergast noodleplex")
        assert result["matches"] == []
        assert "note" not in result


class TestSearchContractsSourcesFailedWarning:
    """Regression: a query answered from a partially-failed build (see
    TestBuildContractCardsFailureRetry) used to give the model no signal
    at all that the empty/thin match list might be an outage, not proof
    the contract doesn't exist -- sourcesFailed sat unused in the result
    and was never mentioned in the system prompt either."""

    def test_note_warns_when_a_source_failed(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(
            settlement, "fetch_eurex_products",
            lambda: (_ for _ in ()).throw(settlement.SettlementError("Eurex catalog down")),
        )
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})
        result = ss.search_contracts("anything")
        assert result["sourcesFailed"]
        assert "note" in result
        assert "unreachable" in result["note"].lower()
        assert "do not conclude" in result["note"].lower()

    def test_no_warning_when_every_source_succeeds(self, monkeypatch):
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_sgx_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})
        result = ss.search_contracts("anything")
        assert result["sourcesFailed"] == []
        assert "note" not in result


class TestSearchContractsParsedDateAndSgxRouting:
    """Regression: "what did the Nikkei settle at on 10 July 2026" used to
    have its one stated date consumed and re-purposed as a contract-month
    FILTER (parsedExpiry="2026-07"), rather than surfaced as the specific
    day the question was actually about -- and get_sgx_settlement_prices
    (the SGX card's own fetch.tool) can't answer a past-date question at
    all (current snapshot only)."""

    def _patch(self, monkeypatch):
        monkeypatch.setattr(
            settlement, "fetch_sgx_fsp",
            lambda: {"rows": [{"contract": "SGX Nikkei 225 Index Futures", "ticker": "NK", "productType": "Equity Index"}]},
        )
        monkeypatch.setattr(settlement, "fetch_hkex_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "fetch_eurex_products", lambda: [])
        monkeypatch.setattr(settlement, "fetch_eurex_msci_fsp", lambda: {"rows": []})
        monkeypatch.setattr(settlement, "_load_resolved_eurex_ids", lambda: {})

    def test_parsed_date_surfaced_in_result(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("nikkei settlement on 2026-07-10")
        assert result["parsedDate"] == "2026-07-10"
        assert result["parsedExpiry"] == "2026-07"

    def test_parsed_date_note_distinguishes_it_from_expiry_month(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("nikkei settlement on 2026-07-10")
        assert "note" in result
        assert "specific day" in result["note"].lower()
        assert "not" in result["note"].lower() and "contract_month" in result["note"]

    def test_sgx_match_with_parsed_date_gets_the_routing_note(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("nikkei settlement on 2026-07-10")
        assert "get_sgx_daily_settlement" in result["note"]
        assert "get_sgx_settlement_history" in result["note"]

    def test_no_routing_note_without_a_parsed_date(self, monkeypatch):
        self._patch(monkeypatch)
        result = ss.search_contracts("nikkei settlement")
        assert result["parsedDate"] is None
        assert "note" not in result
