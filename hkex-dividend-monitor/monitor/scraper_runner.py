"""Subprocess wrapper around the `hkex-scraper` console script.

We shell out to the installed CLI rather than importing internals so
this stays decoupled from the upstream project's Python API (which
isn't guaranteed stable). Works identically on Windows/macOS/Linux:
subprocess resolves `hkex-scraper` (or `hkex-scraper.exe` on Windows)
via PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date, datetime, timedelta

from monitor.config import HKT, get_config


class ScraperError(RuntimeError):
    pass


def _fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def scraper_available() -> bool:
    return shutil.which("hkex-scraper") is not None


def run_scrape(
    from_date: date, to_date: date, timeout: int = 1800, metadata_only: bool = False
) -> subprocess.CompletedProcess:
    """Run `hkex-scraper --from-date ... --to-date ...` synchronously.

    With `metadata_only=True`, passes `--metadata-only`: the scraper's Phase 1
    (HKEX JSON API metadata scan) runs and Phase 2 (sequential PDF/HTML
    download + text extraction, the slow part -- roughly one filing per
    minute) is skipped entirely. Metadata-only records get a full row
    (ticker, date, title, documentUrl, etc.) with `documentText`/
    `documentStatus` left unset; use monitor.document_extractor to fill
    those in for specific filings that actually matter, instead of paying
    for full-text extraction on every filing in the date range.

    Raises ScraperError on non-zero exit or if the binary can't be found.
    Idempotent: the scraper dedups on filingId internally, so re-scraping
    an overlapping window is safe and just skips already-ingested filings.
    """
    if not scraper_available():
        raise ScraperError(
            "hkex-scraper executable not found on PATH. "
            "Install it with: pip install \"hkex-filing-scraper[all]\" "
            "(or `git clone` + `pip install .[all]` from source)."
        )

    cfg = get_config()
    env = os.environ.copy()
    env.setdefault("SURREAL_ENDPOINT", cfg.surreal_endpoint)
    env.setdefault("SURREAL_NAMESPACE", cfg.surreal_namespace)
    env.setdefault("SURREAL_DATABASE", cfg.surreal_database)
    env.setdefault("SURREAL_USERNAME", cfg.surreal_username)
    env.setdefault("SURREAL_PASSWORD", cfg.surreal_password)

    cmd = [
        "hkex-scraper",
        "--from-date", _fmt(from_date),
        "--to-date", _fmt(to_date),
    ]
    if metadata_only:
        cmd.append("--metadata-only")

    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScraperError(f"hkex-scraper timed out after {timeout}s") from exc
    except OSError as exc:
        raise ScraperError(f"Failed to launch hkex-scraper: {exc}") from exc

    if proc.returncode != 0:
        raise ScraperError(
            f"hkex-scraper exited with code {proc.returncode}.\n"
            f"stdout (tail): {proc.stdout[-2000:]}\n"
            f"stderr (tail): {proc.stderr[-2000:]}"
        )
    return proc


def compute_scrape_window(target_dates: list[date], lookback_days: int) -> tuple[date, date] | None:
    """Compute the [from, to] window to scrape based on active targets.

    Returns None if there is nothing worth scraping (no target date falls
    within the lookback window and today).

    "today" is HKT's calendar date, not the host machine's local date --
    target dates and HKEX filing dates are both HKT concepts (see
    monitor.db.filing_hkt_date), so on a machine whose system timezone
    isn't HKT (e.g. a personal Windows install outside Hong Kong), using
    the naive local date here can produce an inverted (from_date >
    to_date) window that silently scrapes nothing for a target dated
    "today" in HKT while it's still "yesterday" locally.
    """
    if not target_dates:
        return None

    today = datetime.now(HKT).date()
    earliest_cap = today - timedelta(days=lookback_days)

    relevant = [d for d in target_dates if earliest_cap <= d <= today + timedelta(days=1)]
    if not relevant:
        return None

    from_date = max(min(relevant), earliest_cap)
    to_date = today
    return from_date, to_date
