"""HTTP client to a native `bloomberg_bridge.py` process for dividend data.

Bloomberg's Desktop API (blpapi) only accepts connections from localhost on
the machine running the Bloomberg Terminal, so it can never be reached
directly from inside this app's Docker container. Instead, a user who wants
this integration runs a small standalone bridge process -- bloomberg_bridge.py
-- on the same machine as their Terminal; that bridge talks to blpapi locally
and re-serves the data it needs over plain HTTP.

This module is only ever an HTTP client to that bridge (via `requests`). It
must never import `blpapi` -- that dependency lives solely in the bridge
script, which is a separate, standalone program.
"""
from __future__ import annotations

from typing import Any

import requests

from monitor.config import get_config
from monitor.registry import normalize_ticker

DIVIDEND_FIELDS = (
    "DVD_SH_LAST",
    "BDVD_NEXT_PROJECTED_DVD",
    "DVD_EX_DT",
    "DVD_DECLARED_DT",
    "BDVD_NEXT_EST_EX_DT",
)

FIELD_LABELS = {
    "DVD_SH_LAST": "Last Dividend / Share",
    "BDVD_NEXT_PROJECTED_DVD": "Next Projected Dividend",
    "DVD_EX_DT": "Ex-Dividend Date",
    "DVD_DECLARED_DT": "Declared Date",
    "BDVD_NEXT_EST_EX_DT": "Next Est. Ex-Date",
}


class BloombergError(RuntimeError):
    pass


def bloomberg_configured() -> bool:
    """True only when the Bloomberg integration is turned on and a bridge
    URL is set. Every other Bloomberg-touching code path must gate on this
    before doing anything, so the feature stays fully inert by default."""
    cfg = get_config()
    return bool(cfg.bloomberg_enabled) and bool(cfg.bloomberg_bridge_url.strip())


def to_bloomberg_ticker(ticker: str) -> str:
    """Convert an HKEX stock code to Bloomberg's security-name convention,
    e.g. 700 / 00700 -> "700 HK Equity", 5 / 00005 -> "5 HK Equity"."""
    code = normalize_ticker(ticker)
    stripped = code.lstrip("0") or "0"
    return f"{stripped} HK Equity"


def fetch_dividend_data(tickers: list[str], timeout: float = 20.0) -> list[dict[str, Any]]:
    """Fetch DIVIDEND_FIELDS for each HKEX ticker via the bridge's /dividends
    endpoint.

    Returns a list of {"ticker": <bloomberg ticker>, "code": <normalized
    HKEX code>, "fields": {<field name>: <value>, ...}}, one per security,
    in the order the bridge returned them.

    Callers are expected to check bloomberg_configured() first; this
    function still defends against being called without a configured
    bridge URL.
    """
    if not tickers:
        return []

    cfg = get_config()
    bridge_url = cfg.bloomberg_bridge_url.strip()
    if not bridge_url:
        raise BloombergError("Bloomberg is not configured (no bridge URL set)")

    ticker_to_code = {}
    securities = []
    seen_securities = set()
    for t in tickers:
        code = normalize_ticker(t)
        bloomberg_ticker = to_bloomberg_ticker(t)
        ticker_to_code[bloomberg_ticker] = code
        # De-duplicate: the bridge sends one Bloomberg request per distinct
        # security, and matches rows back to requested slots by name -- a
        # repeated name in the same request is an edge case some Bloomberg
        # setups may not echo back one-for-one, so avoid it entirely here.
        if bloomberg_ticker not in seen_securities:
            seen_securities.add(bloomberg_ticker)
            securities.append(bloomberg_ticker)

    url = f"{bridge_url.rstrip('/')}/dividends"
    headers = {"Content-Type": "application/json"}
    if cfg.bloomberg_token:
        headers["X-Bloomberg-Token"] = cfg.bloomberg_token

    try:
        resp = requests.post(
            url,
            json={"securities": securities, "fields": list(DIVIDEND_FIELDS)},
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BloombergError(
            f"Bloomberg bridge is not reachable at {bridge_url} -- is "
            f"bloomberg_bridge.py running on the Terminal PC? ({exc})"
        ) from exc

    if resp.status_code == 401:
        raise BloombergError("Bloomberg bridge rejected the request: bad or missing token")
    if resp.status_code >= 400:
        raise BloombergError(
            f"Bloomberg bridge returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise BloombergError(f"Unparseable Bloomberg bridge response: {resp.text[:500]}") from exc

    # Defend against a misconfigured bridge URL / unexpected response shape
    # (e.g. it happens to hit some other JSON HTTP endpoint) -- callers only
    # catch BloombergError, so a malformed body must raise that, not a bare
    # AttributeError from blindly calling .get() on the wrong type.
    if not isinstance(payload, dict):
        raise BloombergError(
            f"Unexpected Bloomberg bridge response (expected a JSON object): {resp.text[:500]}"
        )

    raw_securities = payload.get("securities", [])
    if not isinstance(raw_securities, list):
        raise BloombergError(
            f"Unexpected Bloomberg bridge response ('securities' is not a list): {resp.text[:500]}"
        )

    results = []
    for entry in raw_securities:
        if not isinstance(entry, dict):
            raise BloombergError(
                f"Unexpected Bloomberg bridge response (a security entry is not an object): {resp.text[:500]}"
            )
        bloomberg_ticker = entry.get("ticker", "")
        code = ticker_to_code.get(bloomberg_ticker, bloomberg_ticker)
        results.append(
            {
                "ticker": bloomberg_ticker,
                "code": code,
                "fields": entry.get("fields") or {},
            }
        )
    return results
