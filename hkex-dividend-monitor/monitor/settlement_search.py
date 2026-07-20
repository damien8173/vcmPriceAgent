"""Lexical retrieval over the settlement-price contract universe.

monitor.settlement exposes deterministic fetch/filter functions per
exchange, but the chat assistant was still choosing WHICH code, contract
name, or exchange to call them with from its own memory -- the actual
source of the wrong-row incidents this module exists to close off (see
monitor.settlement's filter_hkex_rows docstring and monitor.chat's
settlement system-prompt bullet for the concrete cases).

This module builds a normalized, searchable index -- "contract cards" --
from each exchange's own live catalog (never from memory), and scores
free-text queries against it. The chat assistant is required to call
search_contracts first and then use exactly the returned fetch.tool /
fetch.params, rather than guessing a code or contract name itself. This is
retrieval-first grounding (the "R" in RAG) applied to structured catalog
data rather than prose documents -- no embeddings, no LLM involved, fully
deterministic and unit-testable.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Callable, Optional

from monitor import settlement
from monitor.config import HKT

_CARDS_TTL_SECONDS = 3600.0  # mirrors fetch_eurex_products' TTL, the slowest of the four sources

# Words that carry no retrieval signal in a settlement-price question --
# stripped from the query side only (never from a card's own name/codes)
# before recall/precision/phrase scoring, so a prose question doesn't get
# diluted or misdirected by its own scaffolding ("what did X settle at" has
# only one contentful word: X). If stripping removes every token (a query
# that's ENTIRELY filler, e.g. a bare code like "CAN"), the unfiltered
# token list is used instead -- see its use in search_contracts.
_FILLER_TOKENS = frozenset({
    "the", "a", "an", "of", "for", "at", "on", "in", "to", "is", "was", "were",
    "what", "when", "which", "how", "much", "did", "do", "does", "and", "or",
    "as", "by", "with", "from", "it", "its", "you", "me", "my", "your", "i",
    "we", "us", "please", "show", "give", "tell", "can", "could", "would",
    "should", "get", "need", "want", "have", "has", "had", "price", "prices",
    "settle", "settled", "settlement", "final", "level", "today", "latest",
    "current", "expiry", "all",
})

# Product/company codes that are ALSO common English words (Air Liquide=AIR,
# Allreal=ALL, Canal+=CAN, Forbo=FOR, Getinge=GET, Nemetschek=NET,
# Pernod-Ricard=PER, Sulzer=SUN, SGX USD/SGD=US, Givaudan=GIVE,
# Henkel=THEN, Lonza=LONG, ...). An exact-code match on one of these only
# counts when the user plausibly meant a code, not when ordinary prose
# happens to contain the word -- see the token-local check in
# search_contracts (_code_is_deliberate) for exactly what "plausibly
# meant" requires.
#
# Generation recipe (live-verified; re-run periodically as catalogs
# change): build_contract_cards(force=True), collect every card's codes
# uppercased, intersect with /usr/share/dict/words (macOS's bundled
# Webster's Second, ~236k entries) for length >= 2, then hand-filter the
# raw intersection down to words a user would plausibly type in ordinary
# lowercase settlement-question prose -- Webster's Second is exhaustive
# enough that the raw intersection also contains dozens of archaic/rare
# entries that are technically dictionary words but essentially never
# appear in real usage (e.g. "coix", "dowf", "yare"); including those
# would only make retrieval more conservative for no real protective
# benefit, so they're deliberately left out. The first two rows are the
# original hand-vetted set from before that live scan existed.
_AMBIGUOUS_CODE_WORDS = frozenset({
    "AIR", "ALL", "CAN", "FOR", "GET", "NET", "PER", "SUN",
    "NEW", "ONE", "TWO", "TOP", "LOW", "NOW", "DAY", "USE", "SEE", "BUY", "GAS", "OIL",
    "US", "GIVE", "THEN", "LONG", "MAN", "TAKE", "FREE", "HOT", "RED", "CAR",
    "CON", "NON", "NONE", "SOL", "VOL", "HALF", "FEED",
    "ACE", "ACT", "BAN", "BARE", "BARN", "BASE", "BAY", "BELL", "BOSS", "BUD",
    "BYE", "DIM", "DOC", "DUE", "ELSE", "ERA", "FEUD", "FINE", "FOAM", "GANG",
    "GIN", "HEN", "HEX", "HOLE", "HUB", "IMP", "INN", "KIN", "LEG", "LONE",
    "LORE", "MAC", "MAP", "MET", "MIN", "NAG", "NICK", "NOSE", "OMEN", "PAGE",
    "PEN", "PIC", "PORE", "PORK", "PRY", "REP", "SAD", "SAX", "SEC", "SIN",
    "SING", "SIR", "SONG", "SOON", "SOP", "TACK", "TAG", "TALC", "TAUT", "TEAM",
    "TENT", "TINT", "TORN", "TOTE", "TRIO", "VOW", "WEB", "ARC", "COG", "COT",
    "CARP", "CHI", "LISP", "SERF",
})

_MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
# Trailing "-DD" is now captured (not just consumed) so a full date like
# "2026-05-15" can feed parsedDate below, not just strip cleanly from the
# cleaned query.
_ISO_EXPIRY_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])(?:-(\d{1,2}))?\b")
_MONTH_YEAR_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")[\s\-]?(\d{2}|\d{4})\b",
    re.IGNORECASE,
)

# A settlement question is never about a contract expiring far in the past
# or the distant future -- this window catches a *day* number wrongly read
# as a 2-digit year ("June 30" -> year 30 -> 2030; "March 15" -> 2015),
# which would otherwise silently poison expiry_month/contract_month filters
# with a plausible-looking but wrong month. "dec 25" (-> 2025) stays
# inherently ambiguous between Christmas Day and December 2025 and is not
# rejected -- there's no signal in the text to disambiguate it. A 4-digit
# year gets a much wider backward-looking window than a 2-digit one: a
# 2-digit year is exactly the shape a misread day number takes (so it's
# kept narrow, tight around "now"), but a spelled-out 4-digit year is
# never a day number in disguise -- "august 2022 hsi" is unambiguously a
# real historical-expiry question this app's own tools (get_sgx_daily_
# settlement, get_sgx_settlement_history) can actually answer, and used to
# be silently rejected by the same narrow window a stray "March 15" needs.
_EXPIRY_YEAR_MIN_OFFSET_2DIGIT = -2
_EXPIRY_YEAR_MIN_OFFSET_4DIGIT = -10
_EXPIRY_YEAR_MAX_OFFSET = 3


def _year_is_plausible(year: int, *, four_digit: bool) -> bool:
    current = datetime.now(HKT).year
    min_offset = _EXPIRY_YEAR_MIN_OFFSET_4DIGIT if four_digit else _EXPIRY_YEAR_MIN_OFFSET_2DIGIT
    return (current + min_offset) <= year <= (current + _EXPIRY_YEAR_MAX_OFFSET)


def parse_expiry(query: str) -> tuple[str, Optional[str], Optional[str]]:
    """Extract an expiry month (and, when the query named a specific day,
    an observation date) from free text -- returns (query with the
    matched phrase removed, "YYYY-MM" contract-month or None, "YYYY-MM-DD"
    date or None). Recognizes "may26", "May 2026", "may-26", and a bare
    ISO "2026-05" (or a full "2026-05-15" date) -- the ways a user is
    likely to phrase an expiry or a specific day without requiring an
    exact format. Rejects an implausible year (see _year_is_plausible)
    rather than returning a wrong expiry/date; a day that doesn't form a
    real calendar date for its month (e.g. "2026-02-30") keeps the
    expiry but drops the date, same as if no day had been given at all.

    The two are DIFFERENT questions downstream, and matter for the
    caller: parsedExpiry is a contract's expiry MONTH (get_hkex_
    settlement_prices' expiry_month, get_sgx_settlement_prices'
    contract_month); parsedDate is a specific trading DAY someone asked
    about a price ON (get_sgx_daily_settlement's date, an Eurex busdate)
    -- conflating them turns "what settled on 10 July" into a filter for
    everything expiring in July, not the 10th specifically.
    """
    m = _ISO_EXPIRY_RE.search(query)
    if m:
        year = int(m.group(1))
        if _year_is_plausible(year, four_digit=True):
            cleaned = (query[: m.start()] + query[m.end():]).strip()
            expiry = f"{year:04d}-{m.group(2)}"
            parsed_date = None
            if m.group(3):
                try:
                    parsed_date = date(year, int(m.group(2)), int(m.group(3))).isoformat()
                except ValueError:
                    pass  # e.g. "2026-02-30" isn't a real date -- keep the expiry, drop the date
            return cleaned, expiry, parsed_date

    m = _MONTH_YEAR_RE.search(query)
    if m:
        month = _MONTH_NAMES[m.group(1).lower()]
        year_str = m.group(2)
        year = int(year_str) + (2000 if len(year_str) == 2 else 0)
        if _year_is_plausible(year, four_digit=len(year_str) == 4):
            cleaned = re.sub(r"\s{2,}", " ", query[: m.start()] + query[m.end():]).strip()
            return cleaned, f"{year:04d}-{month:02d}", None

    return query, None, None


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _classify_hkex_variant(name: str, product_type: str) -> tuple[str, bool]:
    """HKEX's "SEPARATE rows per variant" problem (see filter_hkex_rows)
    only occurs for Equity Index contracts -- but at least one variant (the
    ETF-tracking one) is filed under productType "Stock Futures" rather
    than "Equity Index", so the name patterns are checked first regardless
    of productType; only a name matching none of them falls back to
    "single contract, no variant siblings" using productType."""
    lname = name.lower()
    if "weekly" in lname:
        return "weekly options", False
    if "futures options" in lname and "futures & options" not in lname and "futures and options" not in lname:
        return "futures options", False
    if "net total return" in lname:
        return "net total return", False
    if "gross total return" in lname:
        return "gross total return", False
    if "dividend point" in lname:
        return "dividend point", False
    if lname.endswith(" etf"):
        return "etf", False
    if "futures & options" in lname or "futures and options" in lname:
        return "monthly futures & options", True
    return (product_type or "contract").lower(), True


def _hkex_cards() -> list[dict[str, Any]]:
    data = settlement.fetch_hkex_fsp()
    by_name: dict[str, dict[str, Any]] = {}
    for row in data["rows"]:
        name = row.get("contract")
        if not name:
            continue
        entry = by_name.setdefault(name, {"codes": set(), "productType": row.get("productType") or ""})
        entry["codes"].update(settlement._hkats_components(row))

    cards = []
    for name, info in by_name.items():
        variant, main = _classify_hkex_variant(name, info["productType"])
        codes = sorted(info["codes"])
        # Fetch by the exact official contract name, not the HKATS code:
        # a variant (e.g. weekly options) routinely SHARES its code with
        # the monthly contract (both "HSI"), so filtering by code alone
        # pulls sibling-variant rows into what's meant to be one specific
        # card's own fetch -- verified live: fetching "HSI" by code
        # returned 24 weekly + only 6 monthly rows. filter_hkex_rows'
        # contract filter is an exact-name substring match against this
        # same live table, so it can't drift from what this card represents.
        params: dict[str, Any] = {"contract": name}
        cards.append({
            "exchange": "HKEX",
            "source": "hkex_fsp",
            "name": name,
            "codes": codes,
            "productType": info["productType"],
            "variant": variant,
            "main": main,
            "fetch": {"tool": "get_hkex_settlement_prices", "params": params},
        })
    return cards


# SGX has the same "many related contracts share a family name" problem
# HKEX does (e.g. Nikkei 225's flagship futures vs. its ESG/climate,
# dividend-point, total-return, and micro-sized siblings, all separate
# rows) -- demonstrated live: a bare "Nikkei 225" query top-ranked the
# Climate PAB variant over the standard contract with no signal to prefer
# one over the other, and separately, an SGX "Weekly Options" sibling
# (e.g. NSE IFSC Nifty 50 Index Weekly Options) was left classified as
# `main`, tying its own monthly contract with no way to break the tie.
# These substring markers flag the non-standard siblings so `main` can
# break the tie the same way HKEX's classifier does.
_SGX_NON_MAIN_MARKERS = (
    " weekly", "total return", "dividend point", "climate", " esg", "esg-", " pab", "micro ", "mini ",
)


def _classify_sgx_variant(name: str) -> tuple[str, bool]:
    lname = f" {name.lower()} "
    for marker in _SGX_NON_MAIN_MARKERS:
        if marker in lname:
            return marker.strip(" -") or "variant", False
    return "standard", True


def _sgx_cards() -> list[dict[str, Any]]:
    data = settlement.fetch_sgx_fsp()
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in data["rows"]:
        name = row.get("contract")
        if not name:
            continue
        key = (name, row.get("ticker") or "")
        seen.setdefault(key, row)

    cards = []
    for (name, ticker), row in seen.items():
        # SGX combines related tickers into one compound field like
        # "NK/NKO" (same pattern as HKEX's compound HKATS codes) -- split
        # so an exact-code match on just "NK" still works.
        codes = [c.strip() for c in ticker.split("/") if c.strip()] if ticker else []
        variant, main = _classify_sgx_variant(name)
        cards.append({
            "exchange": "SGX",
            "source": "sgx_fsp",
            "name": name,
            "codes": codes,
            "productType": row.get("productType") or "",
            "variant": variant,
            "main": main,
            "fetch": {"tool": "get_sgx_settlement_prices", "params": {"search": ticker or name}},
        })
    return cards


def _classify_eurex_variant(group: str) -> tuple[str, bool]:
    """Eurex's general product catalog has no per-contract name pattern to
    sniff a variant from the way HKEX/SGX names do -- PRODUCT_GROUP is the
    only signal (live-observed groups include "INDEX FUTURES", "INDEX
    OPTIONS", "INDEX DIVIDEND FUTURES", "EQUITY TOTAL RETURN FUTURES",
    "SINGLE STOCK OPTIONS", ...). A query for the base product should
    default to the plain futures reading, not its options/dividend/
    total-return sibling -- same "main is the standard contract" policy
    HKEX/SGX already apply, just keyed off group instead of name."""
    ugroup = (group or "").upper()
    if "OPTION" in ugroup:
        return "options", False
    if "DIVIDEND" in ugroup:
        return "dividend", False
    if "TOTAL RETURN" in ugroup:
        return "total return", False
    return (group or "product").lower(), True


def _eurex_cards() -> list[dict[str, Any]]:
    products = settlement.fetch_eurex_products()
    # Read the resolved-id store once rather than once per product (up to
    # ~3000 file reads otherwise -- resolve_eurex_product_id re-reads it
    # every call, fine for a single lookup but not for a catalog scan).
    resolved_ids = settlement._load_resolved_eurex_ids()

    cards = []
    for p in products:
        code = p.get("code")
        if not code:
            continue
        resolved = code in settlement._EUREX_SEED_PRODUCT_IDS or code in resolved_ids
        group = p.get("group") or ""
        variant, main = _classify_eurex_variant(group)
        card: dict[str, Any] = {
            "exchange": "Eurex",
            "source": "eurex_products",
            "name": p.get("name") or code,
            "codes": [code],
            "productType": group,
            "variant": variant,
            "main": main,
            "resolved": resolved,
            "fetch": {"tool": "get_eurex_settlement_prices", "params": {"product_code": code}},
        }
        if not resolved:
            card["note"] = (
                f"'{code}' hasn't been resolved to a Eurex product id yet -- tell the user to "
                "open the Eurex tab, enter this code, and paste that product's Eurex page URL "
                "to resolve it (a one-time step) -- never guess a product id yourself."
            )
        cards.append(card)
    # The general catalog routinely lists the same company/index name more
    # than once across its futures/options/dividend/total-return siblings
    # (live-confirmed: 621 duplicate (exchange, name) pairs, e.g. "Ferrari"
    # x3 -- SINGLE STOCK OPTIONS / SINGLE STOCK FUTURES / EQUITY TOTAL
    # RETURN FUTURES) -- disambiguate the same way MSCI's cards already
    # are, keyed off productType (which reliably differs across those
    # siblings) rather than dividendReinvestment (an MSCI-only field).
    _disambiguate_duplicate_names(
        cards,
        suffix_sources=(
            lambda card: card.get("productType"),
            lambda card: card["codes"][0] if card["codes"] else None,
        ),
    )
    return cards


_DEFAULT_DISAMBIGUATION_SUFFIX_SOURCES: tuple[Callable[[dict[str, Any]], Optional[str]], ...] = (
    lambda card: card.get("dividendReinvestment"),
    lambda card: card["codes"][0] if card["codes"] else None,
)


def _disambiguate_duplicate_names(
    cards: list[dict[str, Any]],
    suffix_sources: tuple[Callable[[dict[str, Any]], Optional[str]], ...] = _DEFAULT_DISAMBIGUATION_SUFFIX_SOURCES,
) -> None:
    """Mutates `cards` in place: more than one distinct, differently-coded
    contract can share the same display name -- MSCI's own workbook does
    this after folding in currency (e.g. "MSCI World Futures (USD)" covers
    both a Net Total Return series, code FMWO, and a Price-return series,
    code FMWP -- verified live), and so does Eurex's general catalog (a
    company/index name repeated across its futures/options/dividend/
    total-return siblings, e.g. "Ferrari" x3 -- verified live, 621
    duplicate (exchange, name) pairs). `suffix_sources` tries each
    candidate field in order (MSCI's own dividendReinvestment column by
    default; _eurex_cards passes productType instead, which reliably
    differs across ITS siblings), falling back to the code itself in the
    rare case neither disambiguates, so a retrieval match is never
    ambiguous between two real contracts. Each source is only applied to a
    group if it gives every card in that group a distinct, non-empty
    value -- otherwise that group is left for the next source (or, if none
    work, stays duplicated rather than mislabeled).
    """
    def _append_suffix(card: dict[str, Any], suffix: str) -> None:
        if card["name"].endswith(")"):
            card["name"] = f"{card['name'][:-1]}, {suffix})"
        else:
            card["name"] = f"{card['name']} ({suffix})"

    for suffix_of in suffix_sources:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for card in cards:
            by_name.setdefault(card["name"], []).append(card)
        for group in by_name.values():
            if len(group) < 2:
                continue
            suffixes = [suffix_of(card) for card in group]
            if None in suffixes or len(set(suffixes)) != len(group):
                continue  # this suffix source doesn't uniquely distinguish every card yet
            for card, suffix in zip(group, suffixes):
                _append_suffix(card, suffix)


# Style/factor variants of a base MSCI index (e.g. "MSCI World SRI",
# "MSCI World Momentum") are never the standard/"main" reading of a bare
# query for the base index -- distinct products, not just a currency or
# return-type variant of the same one.
_MSCI_STYLE_WORDS = ("sri", "momentum", "quality", "midcap", "small", "esg", "climate")


def _eurex_msci_cards() -> list[dict[str, Any]]:
    data = settlement.fetch_eurex_msci_fsp()
    # Grouped by base index name (before currency folds into the display
    # name) so `main` can be picked per GROUP, not per row -- MSCI's
    # workbook has no field distinguishing a "standard" variant among a
    # group's currency/return-type siblings. Live-confirmed: every MSCI
    # card used to be hardcoded main=True, and the system prompt's "answer
    # using the one marked main" rule then let the model fabricate an
    # unfounded "the standard MSCI World contract is the EUR NTR variant"
    # for a query naming no currency/return-type at all.
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in data["rows"]:
        # Squash whitespace: at least one live indexName ("MSCI Emerging
        # Markets ") carries a trailing space that would otherwise leak
        # into the display name and tokenize into a stray empty token.
        name = re.sub(r"\s+", " ", (row.get("indexName") or "")).strip()
        # Belt-and-braces: monitor.settlement._parse_msci_workbook already
        # drops legend/footnote rows (no eurexCode and no settlement
        # figures) at the source -- this only guards against a
        # differently-shaped stray row slipping past that upstream filter.
        if not name or name.startswith("*") or " = " in name:
            continue
        groups.setdefault(name, []).append(row)

    cards = []
    for base_name, rows in groups.items():
        style_variant = any(w in base_name.lower() for w in _MSCI_STYLE_WORDS)
        main_row = None
        if not style_variant:
            if len(rows) == 1:
                main_row = rows[0]
            else:
                # Among a real multi-currency/return-type group, the Net
                # Total Return series (preferring USD, Eurex's own default
                # quoting currency) is the closest thing to a "standard"
                # reading -- but only when the group actually contains
                # one; a group with none (e.g. only Price-return variants)
                # is left with NO main at all rather than picking one
                # arbitrarily. This is exactly the "multiple/no main"
                # ambiguity the system prompt now tells the model to ask
                # the user about instead of silently choosing.
                ntr_rows = [r for r in rows if (r.get("dividendReinvestment") or "").strip().upper() == "NTR"]
                if ntr_rows:
                    usd_ntr = [r for r in ntr_rows if (r.get("currency") or "").strip().upper() == "USD"]
                    main_row = (usd_ntr or ntr_rows)[0]
        for row in rows:
            code = row.get("eurexCode")
            currency = row.get("currency")
            dividend = row.get("dividendReinvestment")
            # Several distinct, separately-coded contracts share one
            # indexName (e.g. "MSCI World" has EUR/GBP/USD variants, each
            # its own real Eurex code) -- fold currency into the display
            # name so a match is self-disambiguating instead of presenting
            # duplicates. "Futures" is appended because these ARE futures
            # contracts and a natural query ("MSCI World futures
            # settlement price") contains that word -- without it, this
            # card's name is too sparse to outscore the general Eurex
            # product catalog's entry for the same underlying index, which
            # needs an unnecessary manual product-id resolve step this
            # purpose-built card and its fetch tool don't.
            display_name = f"{base_name} Futures ({currency})" if currency else f"{base_name} Futures"
            variant = " ".join(p for p in ((dividend or "").strip(), (currency or "").strip()) if p).lower()
            cards.append({
                "exchange": "Eurex",
                "source": "eurex_msci",
                "name": display_name,
                "codes": [code] if code else [],
                "productType": row.get("indexType") or "",
                "dividendReinvestment": dividend,
                "variant": variant or "msci index",
                "main": row is main_row,
                "fetch": {"tool": "get_eurex_msci_fsp", "params": {"search": code or base_name}},
            })
    _disambiguate_duplicate_names(cards)
    return cards


_CARD_SOURCES = (
    ("HKEX", _hkex_cards),
    ("SGX", _sgx_cards),
    ("Eurex products", _eurex_cards),
    ("Eurex MSCI", _eurex_msci_cards),
)


def _build_contract_cards_impl() -> dict[str, Any]:
    cards: list[dict[str, Any]] = []
    failed: list[str] = []
    # The four sources are independent live fetches with no shared state
    # between them -- run them concurrently rather than serially (measured
    # live: ~20s cold serial, dominated by per-source network latency, not
    # CPU work, so threads are the right tool here). Submitted in
    # _CARD_SOURCES' own order and consumed in that same order (not
    # completion order) so the assembled card list -- and which source
    # lands in `failed` first -- stays deterministic regardless of which
    # network call happens to finish first; `.result()` on an
    # already-finished future returns immediately, so this costs nothing
    # over consuming in true completion order.
    with ThreadPoolExecutor(max_workers=len(_CARD_SOURCES)) as pool:
        futures = [(label, pool.submit(builder)) for label, builder in _CARD_SOURCES]
        for label, future in futures:
            try:
                cards.extend(future.result())
            except settlement.SettlementError as exc:
                failed.append(f"{label}: {exc}")
    return {"cards": cards, "sourcesFailed": failed}


# A build where one or more sources failed (e.g. a transient network
# blip) would otherwise sit cached for the FULL healthy TTL -- live-
# confirmed twice, independently, that this poisons retrieval for up to
# an hour: a wrong-exchange match sits in `matches` with a normal-looking
# positive score, indistinguishable from a healthy one. A failed build is
# retried much sooner instead; still-failing simply re-caches under this
# same short window rather than the full hour.
_FAILED_BUILD_RETRY_SECONDS = 120.0


def build_contract_cards(force: bool = False) -> dict[str, Any]:
    """Normalized, searchable index of every contract/product across HKEX,
    SGX, and Eurex (daily + MSCI) -- assembled from each exchange's own
    live catalog, never from memory. Each source is fetched independently:
    one failing (e.g. Eurex's catalog endpoint down) doesn't blank the
    other two -- see sourcesFailed. Cached ~1h when healthy, much sooner
    when the cached build itself has a sourcesFailed (see
    _FAILED_BUILD_RETRY_SECONDS); pass force=True to rebuild immediately
    regardless."""
    if not force:
        peeked = settlement._cache_peek("contract_cards")
        if peeked is not None:
            age, value = peeked
            if value.get("sourcesFailed") and age > _FAILED_BUILD_RETRY_SECONDS:
                force = True
    return settlement._cached_fetch("contract_cards", force, _CARDS_TTL_SECONDS, _build_contract_cards_impl)


def _code_is_deliberate(code: str, raw_tokens: list[str], all_caps: bool, content_tokens: list[str]) -> bool:
    """Whether an exact-code match on an AMBIGUOUS `code` (one that's also
    a common English word -- see _AMBIGUOUS_CODE_WORDS) plausibly reflects
    the user MEANING the code, not ordinary prose incidentally containing
    the same word. Only ever consulted for ambiguous codes -- a
    non-ambiguous code (not a real English word, e.g. "TCH"/"NK") always
    counts regardless of case, unchanged from before.

    Two ways a mention counts as deliberate:
      1. The code appears in the RAW query as an exact-case uppercase
         token, AND the query as a whole isn't itself typed in all caps.
         That second condition is what a bare case check misses: a query
         typed ENTIRELY IN CAPS makes every ambiguous word satisfy
         "appears in matching case" by coincidence of shouting-case, not
         because the user meant any of them as a code -- live-confirmed:
         "ALL NIKKEI SETTLEMENT PRICES" used to top-rank Allreal Holding
         over the actual Nikkei card.
      2. The filler-stripped query is a SINGLE token equal to the code
         case-insensitively -- covers a user typing just the bare code in
         lowercase (e.g. "sun" for Sunac's own HKATS code), which a
         case-sensitive-only check could never satisfy since a stored code
         is always compared uppercase.
    """
    if code in raw_tokens and not all_caps:
        return True
    if len(content_tokens) == 1 and content_tokens[0].upper() == code:
        return True
    return False


# Query words that name a specific HKEX/SGX variant classification (see
# _classify_hkex_variant / _classify_sgx_variant) -- when one appears in a
# query, only cards whose OWN variant matches it (or whose name contains
# it, for variants like "mini" that live in the name but not a distinct
# variant string) are scored at all. Matched longest-first against the
# cleaned query so "net total return" doesn't get shadowed by "total
# return" (both would otherwise match) before it's tried.
_VARIANT_QUERY_MARKERS = (
    "net total return", "gross total return", "total return", "ntr", "gtr",
    "dividend point", "weekly", "etf", "micro", "mini", "esg", "climate",
)


def search_contracts(query: str, limit: int = 8) -> dict[str, Any]:
    """Lexical retrieval over build_contract_cards(): resolves free text
    (e.g. "HSCEI may26", "DAX futures", "Nikkei 225", a company name) to
    the specific contract(s) the chat should fetch prices for, instead of
    leaving the model to guess a code, contract name, or exchange from
    memory. Ranking, highest first: exact code match (guarded against a
    code that's also a common English word -- see _AMBIGUOUS_CODE_WORDS),
    then a known index-abbreviation alias (HSI/HSCEI/HSTECH) found in the
    contract name, then a contiguous query-phrase match, then word-token
    overlap with the name/product type; `main` (the standard contract,
    e.g. monthly futures & options rather than weekly options) breaks ties
    among equally-scored variants of the same underlying index/product.
    """
    cards_data = build_contract_cards()
    cleaned, expiry_month, parsed_date = parse_expiry(query)
    tokens = _tokenize(cleaned)
    token_set_upper = {t.upper() for t in tokens}
    # Filler-stripped for recall/precision/phrase scoring only -- exact-code
    # matching below still considers every token, since a code that's also
    # a filler word (e.g. Eurex's "FOR"/"CAN"/"GET") must still resolve
    # when the user plainly meant the code (guarded by case, not presence).
    # Falls back to the unfiltered list if stripping empties it, so a bare
    # query that's entirely filler-shaped (e.g. just a code, "CAN") still
    # has something to score against.
    content_tokens = [t for t in tokens if t not in _FILLER_TOKENS] or tokens
    # Aliases are matched per-token, not against the whole cleaned string --
    # a real query is prose ("HSCEI may26 expiry level"), not a bare code,
    # so only "HSCEI" should be looked up, not "HSCEI EXPIRY LEVEL".
    aliases_hit = {
        settlement._HKEX_INDEX_ALIASES[t] for t in token_set_upper if t in settlement._HKEX_INDEX_ALIASES
    }
    content_phrase = " ".join(content_tokens)
    # Reverse direction: the user typed the full wording, not the
    # abbreviation -- see _HKEX_REVERSE_PHRASE_ALIASES's own comment for
    # why this is matched longest-phrase-first and what the None entries
    # (deliberate blocks, e.g. "hang seng bank") mean.
    for phrase, abbr in settlement._HKEX_REVERSE_PHRASE_ALIASES:
        if phrase in content_phrase:
            if abbr:
                aliases_hit.add(settlement._HKEX_INDEX_ALIASES[abbr])
            break
    # Case-preserving (not lowercased) tokens, plus whether the query as a
    # whole is typed in all caps -- both feed _code_is_deliberate below;
    # see that function's docstring for why "all caps" matters on its own.
    raw_tokens = [t for t in re.split(r"[^A-Za-z0-9]+", query) if t]
    alpha_tokens = [t for t in raw_tokens if any(c.isalpha() for c in t)]
    all_caps = bool(alpha_tokens) and all(t == t.upper() for t in alpha_tokens)

    # An explicit-variant query ("hsi dividend point futures", "hsi weekly
    # options") must not let the flagship monthly contract win on raw
    # score alone -- live-confirmed: "hsi dividend point futures" ranked
    # the main HSI/MHI combo card ~150 points above the actual HSI
    # Dividend Point Index Futures card, with the system prompt then
    # telling the model to answer with whichever match is `main`. When the
    # query names a marker AND at least one card's own classification (or,
    # for markers like "mini" that live only in the name, its name)
    # matches it, scoring is restricted to that subset; matched longest-
    # phrase-first so "net total return" isn't shadowed by "total return".
    active_cards = cards_data["cards"]
    for marker in _VARIANT_QUERY_MARKERS:
        if marker in content_phrase:
            marker_cards = [
                c for c in active_cards
                if marker in (c.get("variant") or "").lower() or marker in (c.get("name") or "").lower()
            ]
            if marker_cards:
                active_cards = marker_cards
                break

    scored: list[tuple[float, dict[str, Any]]] = []
    for card in active_cards:
        score = 0.0
        codes_upper = {c.upper() for c in (card.get("codes") or []) if c}
        matched_codes = codes_upper & token_set_upper
        if matched_codes and any(
            code not in _AMBIGUOUS_CODE_WORDS or _code_is_deliberate(code, raw_tokens, all_caps, content_tokens)
            for code in matched_codes
        ):
            # A card whose own code happens to exact-match a query token
            # can still be the WRONG contract for a multi-word query if the
            # card is a non-main variant with nothing else connecting it to
            # what was actually asked -- live-confirmed: "china a50" exact-
            # code-matched an HKEX ETF-futures card (code "A50") over the
            # real SGX A50 index future, by a ~6x score margin. Withheld
            # only when there's a real alternative reading (>=2 content
            # tokens) and nothing in the query names this specific variant
            # -- a bare code query ("A50" alone) or one that DOES name the
            # variant ("HHI weekly options") still credits normally.
            variant_text = (card.get("variant") or "").lower()
            suppress_code_bonus = (
                card.get("main") is False
                and len(content_tokens) >= 2
                and not any(t in variant_text for t in content_tokens)
            )
            if not suppress_code_bonus:
                score += 100.0
        name_tokens = set(_tokenize(card.get("name") or ""))
        haystack_tokens = name_tokens | set(_tokenize(card.get("productType") or ""))
        haystack = f"{card.get('name') or ''} {card.get('productType') or ''}".lower()
        # A card can be named with the bare ABBREVIATION itself (e.g. "HSI
        # Dividend Point Index Futures") rather than the full wording the
        # alias table expands to -- augment the haystack with each
        # abbreviation's own expansion whenever that abbreviation appears
        # as a token in the card's name, so a full-wording query (typed
        # directly, or resolved via the reverse phrase map above) still
        # reaches a card that only ever spells out the short form.
        for abbr, expansion in settlement._HKEX_INDEX_ALIASES.items():
            if abbr.lower() in name_tokens:
                haystack += " " + expansion.lower()
        if any(alias.lower() in haystack for alias in aliases_hit):
            score += 60.0
        # Contiguous-phrase bonus: rewards a card whose name contains the
        # query's content words in the SAME ORDER, adjacent to each other
        # (e.g. "hang seng index" inside "Hang Seng Index / Mini-... Futures
        # & Options") over a card that merely contains the same words
        # scattered (e.g. "Hang Seng Biotech Index Futures", where "biotech"
        # splits "seng" from "index") -- word-token overlap alone scores
        # both cards almost identically, which is what let an unrelated
        # index outrank the one actually named in a natural-language query.
        if len(content_tokens) >= 2:
            name_phrase = " ".join(_tokenize(card.get("name") or ""))
            if content_phrase in name_phrase:
                score += 15.0
        if content_tokens:
            hits = sum(1 for t in content_tokens if t in haystack_tokens)
            if hits:
                score += 10.0 * hits / len(content_tokens)  # recall: how much of the query this covers
                if hits == len(content_tokens):
                    score += 5.0
                # precision, measured against the NAME alone (not productType, which
                # would otherwise dilute it): without this, a short exact name (e.g.
                # "DAX Futures") ties an unrelated long name that happens to contain the
                # same words (e.g. "iShares Core DAX(R) UCITS (DE) Futures"), tie-breaking
                # arbitrarily by catalog order instead of picking the closer match.
                if name_tokens:
                    name_hits = sum(1 for t in content_tokens if t in name_tokens)
                    score += 5.0 * name_hits / len(name_tokens)
        # The `main` bonus is a tie-breaker among genuine matches, never a
        # standalone signal -- gating it behind score > 0 stops every
        # main-flagged card in the whole catalog from scoring >0 (and thus
        # being returned as a plausible-looking match) for a query that
        # matched nothing at all.
        if score > 0 and card.get("main"):
            score += 2.0
        if score > 0 and card.get("resolved") is False:
            # An unresolved Eurex catalog product needs a manual one-time
            # URL-paste step before it can be fetched at all -- if the same
            # underlying index/product is also reachable through a card
            # that needs no such step (e.g. get_eurex_msci_fsp's own
            # ready-to-use card for an index also listed in the general
            # product catalog), that one should win on a near-tie instead
            # of losing to incidental extra word overlap in the catalog
            # entry's official name.
            score -= 3.0
        if score > 0:
            scored.append((score, card))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    matches = []
    for score, card in scored[:limit]:
        m = dict(card)
        m["score"] = round(score, 2)
        matches.append(m)

    # Guidance for the two ways a query can legitimately yield zero usable
    # tokens to search on, so the model narrows/asks instead of silently
    # accepting an empty or (worse) a stale-looking match list.
    notes: list[str] = []
    if not tokens:
        if expiry_month:
            notes.append(
                "The query contained only an expiry, no contract/index name or code -- "
                "call again including what to look up."
            )
        elif query.strip():
            notes.append(
                "The query has no searchable contract name, index name, or code (e.g. it may "
                "be in a non-Latin script) -- ask the user for it in English."
            )

    if parsed_date:
        # parsedExpiry (a contract MONTH) and parsedDate (a specific
        # trading DAY) answer different questions downstream -- conflating
        # them turns "what settled on 10 July" into a filter for
        # everything expiring in July, not the 10th specifically. Flagged
        # explicitly rather than trusting the field name alone to carry
        # the distinction across to the model.
        notes.append(
            f"parsedDate ({parsed_date}) means the query named a SPECIFIC DAY, not a contract "
            "expiry month -- use it as get_sgx_daily_settlement's date or an Eurex busdate "
            "(YYYYMMDD), never as a contract_month/expiry_month filter."
        )
        if any(m.get("exchange") == "SGX" for m in matches):
            # get_sgx_settlement_prices only ever shows SGX's current
            # snapshot -- never a specific past date -- so a query that
            # both matched SGX and named a day is exactly the shape that
            # risks the wrong tool: the current-snapshot one, for a
            # question that isn't about the current snapshot at all.
            notes.append(
                "This query matched an SGX contract and named a specific day: "
                "get_sgx_settlement_prices cannot answer it (current snapshot only) -- use "
                "get_sgx_daily_settlement(ticker=..., date=parsedDate) for an ongoing contract's "
                "daily mark, or get_sgx_settlement_history for a final/expiry settlement."
            )

    sources_failed = cards_data.get("sourcesFailed", [])
    if sources_failed:
        # A thin or empty match list right now can mean "this contract
        # doesn't exist" OR "the catalog that would have had it is
        # unreachable this build" -- these read identically to a model
        # unless told otherwise, which is exactly the failure mode a
        # cards-build outage produces (see _FAILED_BUILD_RETRY_SECONDS).
        notes.append(
            f"WARNING: catalog(s) unreachable this build: {'; '.join(sources_failed)} -- matches may "
            "be missing entire exchanges/products right now. Do NOT conclude a contract doesn't "
            "exist based on a thin or empty match list while this warning is present -- say the "
            "catalog is temporarily unreachable and suggest retrying shortly instead."
        )

    result: dict[str, Any] = {"parsedExpiry": expiry_month, "parsedDate": parsed_date, "sourcesFailed": sources_failed}
    if notes:
        result["note"] = " ".join(notes)
    result["matches"] = matches
    return result
