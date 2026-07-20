import sys

import pytest

import monitor._ssl_bootstrap as ssl_bootstrap


@pytest.fixture(autouse=True)
def _reset_status():
    """enable_system_trust_store() caches its result process-wide (and the
    package import already ran it once); reset so each test starts clean."""
    saved = ssl_bootstrap._STATUS
    ssl_bootstrap._STATUS = None
    yield
    ssl_bootstrap._STATUS = saved


class TestEnableSystemTrustStore:
    def test_opt_out_via_env_reports_disabled(self, monkeypatch):
        monkeypatch.setenv("MONITOR_DISABLE_TRUSTSTORE", "1")
        assert ssl_bootstrap.enable_system_trust_store() == "disabled"

    @pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
    def test_opt_out_accepts_truthy_variants(self, monkeypatch, value):
        ssl_bootstrap._STATUS = None  # autouse reset already ran; be explicit per-param
        monkeypatch.setenv("MONITOR_DISABLE_TRUSTSTORE", value)
        assert ssl_bootstrap.enable_system_trust_store() == "disabled"

    def test_injects_when_truststore_available(self, monkeypatch):
        monkeypatch.delenv("MONITOR_DISABLE_TRUSTSTORE", raising=False)
        # truststore is a declared dependency; in this env it imports fine.
        assert ssl_bootstrap.enable_system_trust_store() == "injected"

    def test_missing_truststore_falls_back_gracefully(self, monkeypatch):
        monkeypatch.delenv("MONITOR_DISABLE_TRUSTSTORE", raising=False)
        # sys.modules[name] = None makes `import truststore` raise ImportError.
        monkeypatch.setitem(sys.modules, "truststore", None)
        assert ssl_bootstrap.enable_system_trust_store() == "unavailable"

    def test_idempotent_caches_first_decision(self, monkeypatch):
        monkeypatch.delenv("MONITOR_DISABLE_TRUSTSTORE", raising=False)
        first = ssl_bootstrap.enable_system_trust_store()
        # A later env change must NOT re-decide -- injection happens once.
        monkeypatch.setenv("MONITOR_DISABLE_TRUSTSTORE", "1")
        assert ssl_bootstrap.enable_system_trust_store() == first

    def test_status_helper_reports_not_run_before_call(self):
        assert ssl_bootstrap.trust_store_status() == "not-run"
