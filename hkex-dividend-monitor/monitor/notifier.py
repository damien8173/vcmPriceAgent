"""Multi-device webhook dispatch: Slack, Discord, Telegram.

Each channel is enabled iff its required env var(s) are set. A filing
is only marked "notified" (see registry.NotifiedCache) after at least
one channel succeeds; per-channel failures are logged to diagnostics
but never raised.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import requests

from monitor.bloomberg import FIELD_LABELS
from monitor.config import get_config
from monitor.diagnostics import log_error
from monitor.registry import ChannelHealth

ALERT_TEMPLATE = """🚨 **NEW HKEX DIVIDEND ALERT** 🚨
• **Stock Code:** {ticker}
• **Company Name:** {company_name}
• **Payout Amount:** {payout_amount}
• **Ex-Dividend Date:** {ex_dividend_date}
• **Payment Date:** {payment_date}
🔗 **Source Document:** {source_url}"""

PING_TEMPLATE = """⚡ **NEW HKEX FILING DETECTED** ⚡
• **Stock Code:** {ticker}
• **Company Name:** {stock_name}
• **Title:** {title}
• **Detected:** {detected_at} HKT
🔗 **Document:** {document_url}

_Checking whether this is a dividend announcement -- details follow shortly if so._"""

RACE_UNREACHABLE_TEMPLATE = """🔴 **HKEX RACE MODE UNREACHABLE** 🔴
• **Stock Code:** {ticker}
• **Consecutive failures:** {failures}
• **Last error:** {error}
Still retrying with backoff. The normal full-market scan runs in parallel as a backup, but it \
may hit the same issue if HKEX itself is down."""

RACE_RECOVERED_TEMPLATE = """✅ **HKEX RACE MODE RECOVERED** ✅
• **Stock Code:** {ticker}
HKEX search is reachable again; race mode has resumed normal polling."""


@dataclass(frozen=True)
class AlertPayload:
    ticker: str
    company_name: Optional[str]
    payout_amount: Optional[str]
    ex_dividend_date: Optional[str]
    payment_date: Optional[str]
    source_url: Optional[str]
    bloomberg_fields: Optional[dict] = None

    def render(self) -> str:
        def _fmt(v: Optional[str]) -> str:
            return v if v else "N/A"

        message = ALERT_TEMPLATE.format(
            ticker=self.ticker,
            company_name=_fmt(self.company_name),
            payout_amount=_fmt(self.payout_amount),
            ex_dividend_date=_fmt(self.ex_dividend_date),
            payment_date=_fmt(self.payment_date),
            source_url=_fmt(self.source_url),
        )

        if self.bloomberg_fields:
            lines = []
            for field, label in FIELD_LABELS.items():
                value = self.bloomberg_fields.get(field)
                if value is None or value == "":
                    continue
                lines.append(f"• **{label}:** {value}")
            if lines:
                message += "\n\n📊 **Bloomberg dividend data**\n" + "\n".join(lines)

        return message


@dataclass(frozen=True)
class FilingPing:
    """Race mode's stage-1 alert: fired the moment a new filing from a
    racing ticker is detected, before extraction/classification -- pure
    speed, at the cost of not yet knowing if it's a dividend at all."""

    ticker: str
    stock_name: Optional[str]
    title: Optional[str]
    document_url: Optional[str]
    detected_at: datetime

    def render(self) -> str:
        def _fmt(v: Optional[str]) -> str:
            return v if v else "N/A"

        return PING_TEMPLATE.format(
            ticker=self.ticker,
            stock_name=_fmt(self.stock_name),
            title=_fmt(self.title),
            detected_at=self.detected_at.strftime("%Y-%m-%d %H:%M:%S"),
            document_url=_fmt(self.document_url),
        )


@dataclass(frozen=True)
class RaceOutageAlert:
    """Race mode's HKEX-unreachable / recovered notifications -- see
    monitor.daemon's _alert_race_unreachable/_alert_race_recovered, which
    decide *when* to send one of these (failure threshold, cooldown)."""

    ticker: str
    recovered: bool
    failures: int = 0
    error: str = ""

    def render(self) -> str:
        if self.recovered:
            return RACE_RECOVERED_TEMPLATE.format(ticker=self.ticker)
        return RACE_UNREACHABLE_TEMPLATE.format(
            ticker=self.ticker, failures=self.failures, error=self.error or "N/A"
        )


def _send_slack(message: str, timeout: float = 10.0) -> bool:
    cfg = get_config()
    if not cfg.slack_webhook_url:
        return False
    try:
        resp = requests.post(cfg.slack_webhook_url, json={"text": message}, timeout=timeout)
        if resp.status_code >= 300:
            log_error("notifier.slack", f"Slack webhook returned {resp.status_code}: {resp.text[:500]}")
            return False
        return True
    except requests.RequestException as exc:
        log_error("notifier.slack", f"Slack webhook request failed: {exc}")
        return False


def _send_discord(message: str, timeout: float = 10.0) -> bool:
    cfg = get_config()
    if not cfg.discord_webhook_url:
        return False
    try:
        resp = requests.post(cfg.discord_webhook_url, json={"content": message}, timeout=timeout)
        if resp.status_code >= 300:
            log_error("notifier.discord", f"Discord webhook returned {resp.status_code}: {resp.text[:500]}")
            return False
        return True
    except requests.RequestException as exc:
        log_error("notifier.discord", f"Discord webhook request failed: {exc}")
        return False


def _send_telegram(message: str, timeout: float = 10.0) -> bool:
    cfg = get_config()
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return False
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"

    def _post(parse_mode: Optional[str]) -> requests.Response:
        body: dict[str, Any] = {
            "chat_id": cfg.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": False,
        }
        if parse_mode:
            body["parse_mode"] = parse_mode
        return requests.post(url, json=body, timeout=timeout)

    try:
        resp = _post("Markdown")
        if resp.status_code == 400:
            # Telegram rejects the ENTIRE message on a Markdown parse error --
            # filing titles routinely contain '_'/'*' that read as unclosed
            # markup. An unformatted alert beats a silently lost one, so
            # retry as plain text.
            log_error(
                "notifier.telegram",
                f"Telegram rejected Markdown formatting ({resp.text[:200]}); retrying as plain text",
            )
            resp = _post(None)
        if resp.status_code >= 300:
            log_error("notifier.telegram", f"Telegram API returned {resp.status_code}: {resp.text[:500]}")
            return False
        return True
    except requests.RequestException as exc:
        log_error("notifier.telegram", f"Telegram API request failed: {exc}")
        return False


def configured_channels() -> list[str]:
    cfg = get_config()
    channels = []
    if cfg.slack_webhook_url:
        channels.append("slack")
    if cfg.discord_webhook_url:
        channels.append("discord")
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        channels.append("telegram")
    return channels


def dispatch_text(message: str) -> dict[str, bool]:
    """Send a pre-rendered message to every configured channel. Returns
    per-channel success map. Shared by dispatch_alert (dividend details)
    and race mode's stage-1 FilingPing.

    Records each *configured* channel's outcome into ChannelHealth so the
    Dashboard can show actual delivery health ("last delivered 2 min ago" /
    "last 3 attempts failed") instead of just "a webhook URL is set" --
    unconfigured channels are deliberately excluded so they don't show up
    as a false "failure".

    The three sends run in parallel: race mode's instant ping is the
    latency-critical path this app exists for, and sequential sends would
    make total delivery time the SUM of three webhook round-trips (up to
    ~30s worst case with one slow channel) instead of the slowest one.
    """
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "slack": pool.submit(_send_slack, message),
            "discord": pool.submit(_send_discord, message),
            "telegram": pool.submit(_send_telegram, message),
        }
        results = {channel: future.result() for channel, future in futures.items()}
    configured = configured_channels()
    if configured:
        health = ChannelHealth()
        for channel in configured:
            health.record(channel, results[channel])
    return results


def dispatch_alert(payload: AlertPayload) -> dict[str, bool]:
    """Send the alert to every configured channel. Returns per-channel success map."""
    return dispatch_text(payload.render())


def any_succeeded(results: dict[str, bool]) -> bool:
    return any(results.values())
