"""Environment + settings.json driven configuration for the HKEX dividend monitor.

Precedence (highest wins): OS environment variables > data/settings.json
(written by the web Settings page) > built-in defaults.

Call `get_config()` wherever the old code called `CONFIG` directly -- it's
cheap and only rebuilds when data/settings.json's mtime has changed, so
settings saved in the web UI take effect on the daemon's *next* poll
cycle without a container restart.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# This app is used from Hong Kong and its two primary exchanges (HKEX, SGX)
# both trade on this calendar -- every date/time the app shows or reasons
# with (chat's "today", asOf fields, "yesterday" resolution, log timestamps)
# is HKT, never UTC or server-local time. Defined here (not daemon.py) so
# every module -- including settlement.py, chat.py, settlement_search.py --
# can import it without reaching into daemon and risking a cycle.
HKT = ZoneInfo("Asia/Hong_Kong")

# All app state lives under DATA_DIR so it survives container rebuilds via a bind mount.
DATA_DIR = Path(os.environ.get("MONITOR_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
TARGETS_FILE = DATA_DIR / "hkex_targets.json"
NOTIFIED_FILE = DATA_DIR / "notified_filings.json"
ALERT_HISTORY_FILE = DATA_DIR / "alert_history.json"
DIVIDENDS_FILE = DATA_DIR / "dividends.json"
DIAGNOSTICS_FILE = DATA_DIR / "diagnostics.log"
ACTIVITY_FILE = DATA_DIR / "activity.log"
HEARTBEAT_FILE = DATA_DIR / "heartbeat"
SETTINGS_FILE = DATA_DIR / "settings.json"
CHANNEL_HEALTH_FILE = DATA_DIR / "channel_health.json"
WATCHLIST_TICKERS_FILE = DATA_DIR / "watchlist_tickers.json"
EUREX_PRODUCT_IDS_FILE = DATA_DIR / "eurex_product_ids.json"
SGX_DAILY_KEYS_FILE = DATA_DIR / "sgx_daily_keys.json"
CHAT_FEEDBACK_FILE = DATA_DIR / "chat_feedback.log"

# Fields the web Settings page is allowed to write to settings.json.
# (SurrealDB connection fields are intentionally excluded -- those are
# infra, set via .env / docker-compose, not something a user edits live.)
_SETTINGS_KEYS = (
    "deepseek_api_key",
    "deepseek_base_url",
    "deepseek_model",
    "deepseek_reasoning_model",
    "slack_webhook_url",
    "discord_webhook_url",
    "telegram_bot_token",
    "telegram_chat_id",
    "poll_interval_seconds",
    "scrape_lookback_days",
    "max_extraction_retries",
    "race_poll_interval_seconds",
    "race_start_hour",
    "race_end_hour",
    "race_alert_failure_threshold",
    "race_alert_cooldown_seconds",
    "bloomberg_enabled",
    "bloomberg_bridge_url",
    "bloomberg_token",
    "chat_daily_message_limit",
    "watchlist_horizon_days",
    "watchlist_notice_lookback_days",
    "watchlist_history_lookback_days",
    "watchlist_max_candidates",
)

_ENV_VAR_NAMES = {
    "surreal_endpoint": "SURREAL_ENDPOINT",
    "surreal_namespace": "SURREAL_NAMESPACE",
    "surreal_database": "SURREAL_DATABASE",
    "surreal_username": "SURREAL_USERNAME",
    "surreal_password": "SURREAL_PASSWORD",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "deepseek_base_url": "DEEPSEEK_BASE_URL",
    "deepseek_model": "DEEPSEEK_MODEL",
    "deepseek_reasoning_model": "DEEPSEEK_REASONING_MODEL",
    "slack_webhook_url": "SLACK_WEBHOOK_URL",
    "discord_webhook_url": "DISCORD_WEBHOOK_URL",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "poll_interval_seconds": "POLL_INTERVAL_SECONDS",
    "scrape_lookback_days": "SCRAPE_LOOKBACK_DAYS",
    "max_extraction_retries": "MAX_EXTRACTION_RETRIES",
    "race_poll_interval_seconds": "RACE_POLL_INTERVAL_SECONDS",
    "race_start_hour": "RACE_START_HOUR",
    "race_end_hour": "RACE_END_HOUR",
    "race_alert_failure_threshold": "RACE_ALERT_FAILURE_THRESHOLD",
    "race_alert_cooldown_seconds": "RACE_ALERT_COOLDOWN_SECONDS",
    "bloomberg_enabled": "BLOOMBERG_ENABLED",
    "bloomberg_bridge_url": "BLOOMBERG_BRIDGE_URL",
    "bloomberg_token": "BLOOMBERG_TOKEN",
    "chat_daily_message_limit": "CHAT_DAILY_MESSAGE_LIMIT",
    "watchlist_horizon_days": "WATCHLIST_HORIZON_DAYS",
    "watchlist_notice_lookback_days": "WATCHLIST_NOTICE_LOOKBACK_DAYS",
    "watchlist_history_lookback_days": "WATCHLIST_HISTORY_LOOKBACK_DAYS",
    "watchlist_max_candidates": "WATCHLIST_MAX_CANDIDATES",
}

_INT_FIELDS = {
    "poll_interval_seconds",
    "scrape_lookback_days",
    "max_extraction_retries",
    "race_poll_interval_seconds",
    "race_start_hour",
    "race_end_hour",
    "race_alert_failure_threshold",
    "race_alert_cooldown_seconds",
    "bloomberg_enabled",
    "chat_daily_message_limit",
    "watchlist_horizon_days",
    "watchlist_notice_lookback_days",
    "watchlist_history_lookback_days",
    "watchlist_max_candidates",
}


@dataclass(frozen=True)
class Config:
    # SurrealDB
    surreal_endpoint: str = "http://localhost:8000"
    surreal_namespace: str = "hkex"
    surreal_database: str = "hkex"
    surreal_username: str = "root"
    surreal_password: str = ""

    # LLM (DeepSeek, OpenAI-compatible)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # "deepseek-chat"/"deepseek-reasoner" are deprecated 2026-07-24 -- deepseek_model
    # is the fast, non-thinking tier used for routine classify/extract/JSON calls
    # (monitor/extractor.py, monitor/announcement_extractor.py) and the chat
    # assistant. deepseek_reasoning_model is a separate, more expensive escalation
    # tier those two extraction modules call ONLY when a fast-tier result flags
    # itself `ambiguous` -- see their own module docstrings.
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_reasoning_model: str = "deepseek-v4-pro"

    # Notification webhooks
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Scheduler
    poll_interval_seconds: int = 180
    scrape_lookback_days: int = 14
    max_extraction_retries: int = 3

    # Race mode -- tight per-ticker polling on a target's exact date (see
    # monitor/daemon.py's racing_targets/run_race_tick). Defaults cover the
    # entire day at a conservative cadence; both are user-tunable live via
    # the Settings tab without a restart.
    race_poll_interval_seconds: int = 30
    race_start_hour: int = 0
    race_end_hour: int = 24

    # Consecutive HKEX search failures for one racing ticker before the user
    # is alerted directly (push notification, not just diagnostics.log), and
    # the minimum gap between repeat alerts while an outage continues -- see
    # monitor/daemon.py's _alert_race_unreachable.
    race_alert_failure_threshold: int = 3
    race_alert_cooldown_seconds: int = 1800

    # Bloomberg integration -- OFF by default. A native bridge process
    # (bloomberg_bridge.py) runs on the Bloomberg Terminal machine and
    # re-serves Bloomberg Desktop API data over plain HTTP, so this app
    # only ever needs to be an HTTP client and never imports blpapi itself.
    bloomberg_enabled: int = 0
    bloomberg_bridge_url: str = "http://host.docker.internal:8195"
    bloomberg_token: str = ""

    # Soft guardrail against runaway DeepSeek spend from the chat assistant
    # (a UI bug, a very chatty user, etc.) -- 0 means unlimited. Counted
    # in-memory over a rolling 24h window (monitor/chat.py), so it resets on
    # a process restart rather than needing its own persisted state file.
    chat_daily_message_limit: int = 200

    # Today's HKEX Dividend Watchlist -- deterministic ranking of the
    # user's chosen tickers by how likely they are to release a
    # dividend-related announcement soon (see monitor/watchlist.py).
    # Deliberately per-ticker (never market-wide): scope/cost guardrails on
    # the HKEX searches and LLM extraction calls each generation makes.
    watchlist_horizon_days: int = 3
    watchlist_notice_lookback_days: int = 14
    watchlist_history_lookback_days: int = 1095
    watchlist_max_candidates: int = 40

    def ensure_data_dir(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)


_DEFAULTS = Config()


def _read_settings_file() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _build_config() -> Config:
    settings = _read_settings_file()
    values: dict[str, Any] = {}

    for f in fields(Config):
        name = f.name
        env_val = os.environ.get(_ENV_VAR_NAMES[name])
        default = getattr(_DEFAULTS, name)

        # Empty-string env vars count as *unset*: the auto-created .env (and
        # docker-compose env_file) contains blank lines like DEEPSEEK_API_KEY=
        # for every optional setting, and those must not shadow values saved
        # via the web Settings tab.
        if env_val:
            raw: Any = env_val
        elif name in settings and settings[name] not in (None, ""):
            raw = settings[name]
        else:
            raw = default

        if name in _INT_FIELDS:
            try:
                values[name] = int(raw)
            except (TypeError, ValueError):
                values[name] = default
        else:
            values[name] = str(raw)

    return Config(**values)


_lock = threading.Lock()
_cached_config: Config | None = None
_cached_settings_mtime: float | None = None


def _settings_mtime() -> float | None:
    try:
        return SETTINGS_FILE.stat().st_mtime
    except OSError:
        return None


def get_config() -> Config:
    """Return the current effective Config, rebuilding it only if
    data/settings.json changed since the last call."""
    global _cached_config, _cached_settings_mtime
    current_mtime = _settings_mtime()
    with _lock:
        if _cached_config is None or current_mtime != _cached_settings_mtime:
            _cached_config = _build_config()
            _cached_settings_mtime = current_mtime
        return _cached_config


def save_settings(updates: dict[str, Any]) -> None:
    """Merge `updates` into data/settings.json (only recognized keys).

    Writes atomically; the next get_config() call picks up the change
    because the file's mtime will have moved.
    """
    from monitor.jsonutil import atomic_write_json

    current = _read_settings_file()
    for key, value in updates.items():
        if key not in _SETTINGS_KEYS:
            continue
        current[key] = value
    atomic_write_json(SETTINGS_FILE, current)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def masked_settings() -> dict[str, Any]:
    """Effective configuration for display in the UI, with secrets masked.
    Never returns a usable raw key/token value."""
    cfg = get_config()
    return {
        "deepseek_api_key_masked": _mask(cfg.deepseek_api_key),
        "deepseek_api_key_set": bool(cfg.deepseek_api_key),
        "deepseek_base_url": cfg.deepseek_base_url,
        "deepseek_model": cfg.deepseek_model,
        "deepseek_reasoning_model": cfg.deepseek_reasoning_model,
        "slack_webhook_url_masked": _mask(cfg.slack_webhook_url),
        "slack_webhook_set": bool(cfg.slack_webhook_url),
        "discord_webhook_url_masked": _mask(cfg.discord_webhook_url),
        "discord_webhook_set": bool(cfg.discord_webhook_url),
        "telegram_bot_token_masked": _mask(cfg.telegram_bot_token),
        "telegram_bot_token_set": bool(cfg.telegram_bot_token),
        "telegram_chat_id": cfg.telegram_chat_id,  # not secret
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "scrape_lookback_days": cfg.scrape_lookback_days,
        "max_extraction_retries": cfg.max_extraction_retries,
        "race_poll_interval_seconds": cfg.race_poll_interval_seconds,
        "race_start_hour": cfg.race_start_hour,
        "race_end_hour": cfg.race_end_hour,
        "race_alert_failure_threshold": cfg.race_alert_failure_threshold,
        "race_alert_cooldown_seconds": cfg.race_alert_cooldown_seconds,
        "bloomberg_enabled": cfg.bloomberg_enabled,
        "bloomberg_bridge_url": cfg.bloomberg_bridge_url,
        "bloomberg_token_masked": _mask(cfg.bloomberg_token),
        "bloomberg_token_set": bool(cfg.bloomberg_token),
        "chat_daily_message_limit": cfg.chat_daily_message_limit,
        "watchlist_horizon_days": cfg.watchlist_horizon_days,
        "watchlist_notice_lookback_days": cfg.watchlist_notice_lookback_days,
        "watchlist_history_lookback_days": cfg.watchlist_history_lookback_days,
        "watchlist_max_candidates": cfg.watchlist_max_candidates,
    }
