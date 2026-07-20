import json

import pytest

import monitor.config as config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config.SETTINGS_FILE at a fresh tmp file and reset get_config()'s
    module-level cache, so precedence tests never touch the real
    data/settings.json or leak cached state between tests."""
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    config._cached_config = None
    config._cached_settings_mtime = None
    yield settings_file
    config._cached_config = None
    config._cached_settings_mtime = None


def _write_settings(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestPrecedence:
    def test_default_used_when_nothing_else_set(self, isolated_config, monkeypatch):
        monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
        cfg = config.get_config()
        assert cfg.poll_interval_seconds == 180  # dataclass default

    def test_settings_file_overrides_default(self, isolated_config, monkeypatch):
        monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
        _write_settings(isolated_config, {"poll_interval_seconds": 60})
        cfg = config.get_config()
        assert cfg.poll_interval_seconds == 60

    def test_env_var_overrides_settings_file(self, isolated_config, monkeypatch):
        _write_settings(isolated_config, {"poll_interval_seconds": 60})
        monkeypatch.setenv("POLL_INTERVAL_SECONDS", "45")
        cfg = config.get_config()
        assert cfg.poll_interval_seconds == 45

    def test_blank_env_var_does_not_shadow_settings_file(self, isolated_config, monkeypatch):
        """The auto-created .env template ships blank lines like
        DEEPSEEK_API_KEY= for every optional setting -- these must count as
        unset, not override a real value saved via the web Settings tab."""
        _write_settings(isolated_config, {"deepseek_api_key": "sk-real-key-123"})
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        cfg = config.get_config()
        assert cfg.deepseek_api_key == "sk-real-key-123"

    def test_invalid_int_in_settings_file_falls_back_to_default(self, isolated_config, monkeypatch):
        monkeypatch.delenv("MAX_EXTRACTION_RETRIES", raising=False)
        _write_settings(isolated_config, {"max_extraction_retries": "not-a-number"})
        cfg = config.get_config()
        assert cfg.max_extraction_retries == 3  # dataclass default


class TestCaching:
    def test_get_config_is_cached_until_settings_file_mtime_changes(self, isolated_config, monkeypatch):
        monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
        _write_settings(isolated_config, {"poll_interval_seconds": 60})
        first = config.get_config()
        # Rewriting with the same content but not touching the file at all
        # should still return the *same* cached object (no mtime change).
        assert config.get_config() is first

        # Simulate the Settings tab saving a change: touch mtime forward.
        import os
        import time

        time.sleep(0.01)
        _write_settings(isolated_config, {"poll_interval_seconds": 90})
        os.utime(isolated_config, None)
        second = config.get_config()
        assert second.poll_interval_seconds == 90
        assert second is not first


class TestSaveSettings:
    def test_save_settings_ignores_unrecognized_keys(self, isolated_config, monkeypatch):
        monkeypatch.delenv("SURREAL_PASSWORD", raising=False)
        config.save_settings({"surreal_password": "should-not-be-writable", "poll_interval_seconds": 42})
        saved = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert "surreal_password" not in saved
        assert saved["poll_interval_seconds"] == 42

    def test_save_settings_merges_rather_than_replaces(self, isolated_config):
        config.save_settings({"poll_interval_seconds": 42})
        config.save_settings({"scrape_lookback_days": 7})
        saved = json.loads(isolated_config.read_text(encoding="utf-8"))
        assert saved["poll_interval_seconds"] == 42
        assert saved["scrape_lookback_days"] == 7


class TestMaskedSettings:
    def test_masked_settings_never_exposes_raw_secret(self, isolated_config, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        _write_settings(isolated_config, {"deepseek_api_key": "sk-abcdefghijklmnop"})
        masked = config.masked_settings()
        assert "sk-abcdefghijklmnop" not in masked["deepseek_api_key_masked"]
        assert masked["deepseek_api_key_set"] is True
        assert "deepseek_api_key" not in masked  # raw key never included at all

    def test_masked_settings_reports_unset_secret(self, isolated_config, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        masked = config.masked_settings()
        assert masked["deepseek_api_key_masked"] == ""
        assert masked["deepseek_api_key_set"] is False


class TestDeepSeekModelTiers:
    """The fast (deepseek_model) and reasoning-escalation (deepseek_reasoning_model)
    tiers monitor.extractor/monitor.announcement_extractor choose between --
    see their own module docstrings."""

    def test_default_models_are_the_current_fast_and_reasoning_tiers(self, isolated_config, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
        monkeypatch.delenv("DEEPSEEK_REASONING_MODEL", raising=False)
        cfg = config.get_config()
        assert cfg.deepseek_model == "deepseek-v4-flash"
        assert cfg.deepseek_reasoning_model == "deepseek-v4-pro"

    def test_reasoning_model_configurable_via_settings_file(self, isolated_config, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_REASONING_MODEL", raising=False)
        _write_settings(isolated_config, {"deepseek_reasoning_model": "deepseek-v4-pro-custom"})
        cfg = config.get_config()
        assert cfg.deepseek_reasoning_model == "deepseek-v4-pro-custom"

    def test_reasoning_model_configurable_via_env_var(self, isolated_config, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_REASONING_MODEL", "env-pro-model")
        cfg = config.get_config()
        assert cfg.deepseek_reasoning_model == "env-pro-model"

    def test_reasoning_model_in_masked_settings(self, isolated_config, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_REASONING_MODEL", raising=False)
        masked = config.masked_settings()
        assert masked["deepseek_reasoning_model"] == "deepseek-v4-pro"


class TestNewRaceAndChatSettings:
    """New settings added alongside race-mode outage alerting and the chat
    daily cap -- should behave like every other configurable int field."""

    def test_race_alert_fields_have_sane_defaults(self, isolated_config, monkeypatch):
        for var in ("RACE_ALERT_FAILURE_THRESHOLD", "RACE_ALERT_COOLDOWN_SECONDS", "CHAT_DAILY_MESSAGE_LIMIT"):
            monkeypatch.delenv(var, raising=False)
        cfg = config.get_config()
        assert cfg.race_alert_failure_threshold == 3
        assert cfg.race_alert_cooldown_seconds == 1800
        assert cfg.chat_daily_message_limit == 200

    def test_race_alert_fields_configurable_via_settings_file(self, isolated_config, monkeypatch):
        for var in ("RACE_ALERT_FAILURE_THRESHOLD", "RACE_ALERT_COOLDOWN_SECONDS", "CHAT_DAILY_MESSAGE_LIMIT"):
            monkeypatch.delenv(var, raising=False)
        _write_settings(
            isolated_config,
            {
                "race_alert_failure_threshold": 5,
                "race_alert_cooldown_seconds": 600,
                "chat_daily_message_limit": 0,
            },
        )
        cfg = config.get_config()
        assert cfg.race_alert_failure_threshold == 5
        assert cfg.race_alert_cooldown_seconds == 600
        assert cfg.chat_daily_message_limit == 0
