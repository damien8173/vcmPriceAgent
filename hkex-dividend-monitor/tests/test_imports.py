"""Smoke test: every module in the package should at least import cleanly.

Catches syntax errors or import-time crashes in modules (web.py, cli.py,
scraper_runner.py, etc.) that aren't otherwise exercised by the more
targeted unit tests in this directory.
"""
import importlib

import pytest

MODULES = [
    "monitor.announcement_extractor",
    "monitor.bloomberg",
    "monitor.board_meetings",
    "monitor.chat",
    "monitor.chat_feedback",
    "monitor.cli",
    "monitor.config",
    "monitor.daemon",
    "monitor.db",
    "monitor.diagnostics",
    "monitor.document_extractor",
    "monitor.extractor",
    "monitor.features",
    "monitor.hkex_search",
    "monitor.history",
    "monitor.jsonutil",
    "monitor.notifier",
    "monitor.registry",
    "monitor.scoring",
    "monitor.scraper_runner",
    "monitor.settlement",
    "monitor.settlement_history",
    "monitor.settlement_search",
    "monitor.sgx_daily",
    "monitor.watchlist",
    "monitor.web",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports_cleanly(module_name):
    importlib.import_module(module_name)
