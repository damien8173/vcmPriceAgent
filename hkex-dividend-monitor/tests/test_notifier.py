from datetime import datetime, timezone

import monitor.notifier as notifier


class TestAlertPayload:
    def test_render_fills_all_fields(self):
        payload = notifier.AlertPayload(
            ticker="00700",
            company_name="Tencent Holdings Limited",
            payout_amount="HKD 3.40 per share",
            ex_dividend_date="2026-08-20",
            payment_date="2026-09-10",
            source_url="https://example.com/filing.pdf",
        )
        rendered = payload.render()
        assert "00700" in rendered
        assert "Tencent Holdings Limited" in rendered
        assert "HKD 3.40 per share" in rendered
        assert "2026-08-20" in rendered
        assert "2026-09-10" in rendered
        assert "https://example.com/filing.pdf" in rendered
        assert "Bloomberg" not in rendered

    def test_render_uses_na_fallback_for_missing_fields(self):
        payload = notifier.AlertPayload(
            ticker="00700",
            company_name=None,
            payout_amount=None,
            ex_dividend_date=None,
            payment_date=None,
            source_url=None,
        )
        rendered = payload.render()
        assert rendered.count("N/A") == 5  # company, payout, ex-div, payment, source_url

    def test_render_appends_bloomberg_fields_when_present(self):
        payload = notifier.AlertPayload(
            ticker="00700",
            company_name="Tencent",
            payout_amount="HKD 3.40",
            ex_dividend_date="2026-08-20",
            payment_date="2026-09-10",
            source_url="https://example.com/filing.pdf",
            bloomberg_fields={"DVD_SH_LAST": "3.40", "DVD_EX_DT": "2026-08-20"},
        )
        rendered = payload.render()
        assert "Bloomberg dividend data" in rendered
        assert "Last Dividend / Share" in rendered

    def test_render_skips_empty_bloomberg_field_values(self):
        payload = notifier.AlertPayload(
            ticker="00700",
            company_name="Tencent",
            payout_amount="HKD 3.40",
            ex_dividend_date="2026-08-20",
            payment_date="2026-09-10",
            source_url="https://example.com/filing.pdf",
            bloomberg_fields={"DVD_SH_LAST": None, "DVD_EX_DT": ""},
        )
        rendered = payload.render()
        assert "Bloomberg" not in rendered  # every field was empty -> section omitted


class TestFilingPing:
    def test_render_fills_all_fields(self):
        ping = notifier.FilingPing(
            ticker="00700",
            stock_name="Tencent Holdings",
            title="Final Dividend Announcement",
            document_url="https://example.com/filing.pdf",
            detected_at=datetime(2026, 8, 15, 16, 45, tzinfo=timezone.utc),
        )
        rendered = ping.render()
        assert "00700" in rendered
        assert "Tencent Holdings" in rendered
        assert "Final Dividend Announcement" in rendered
        assert "2026-08-15 16:45:00" in rendered
        assert "https://example.com/filing.pdf" in rendered


class TestRaceOutageAlert:
    def test_unreachable_render_includes_failure_details(self):
        alert = notifier.RaceOutageAlert(ticker="00700", recovered=False, failures=5, error="timeout")
        rendered = alert.render()
        assert "UNREACHABLE" in rendered
        assert "00700" in rendered
        assert "5" in rendered
        assert "timeout" in rendered

    def test_unreachable_render_defaults_error_to_na(self):
        alert = notifier.RaceOutageAlert(ticker="00700", recovered=False, failures=3)
        rendered = alert.render()
        assert "N/A" in rendered

    def test_recovered_render_is_distinct_from_unreachable(self):
        alert = notifier.RaceOutageAlert(ticker="00700", recovered=True)
        rendered = alert.render()
        assert "RECOVERED" in rendered
        assert "UNREACHABLE" not in rendered
        assert "00700" in rendered


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class TestSendTelegram:
    def _cfg(self):
        import monitor.config as config

        return config.Config(telegram_bot_token="tok", telegram_chat_id="42")

    def test_markdown_parse_failure_retries_as_plain_text(self, monkeypatch):
        monkeypatch.setattr(notifier, "get_config", lambda: self._cfg())
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json)
            if json.get("parse_mode"):
                return _FakeResponse(400, "Bad Request: can't parse entities")
            return _FakeResponse(200)

        monkeypatch.setattr(notifier.requests, "post", fake_post)
        assert notifier._send_telegram("title_with_underscores") is True
        assert len(calls) == 2
        assert calls[0].get("parse_mode") == "Markdown"
        assert "parse_mode" not in calls[1]

    def test_non_400_failure_does_not_retry(self, monkeypatch):
        monkeypatch.setattr(notifier, "get_config", lambda: self._cfg())
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(json)
            return _FakeResponse(500, "server error")

        monkeypatch.setattr(notifier.requests, "post", fake_post)
        assert notifier._send_telegram("message") is False
        assert len(calls) == 1


class TestDispatchText:
    def test_all_channels_attempted_and_results_collected(self, monkeypatch):
        monkeypatch.setattr(notifier, "_send_slack", lambda m: True)
        monkeypatch.setattr(notifier, "_send_discord", lambda m: False)
        monkeypatch.setattr(notifier, "_send_telegram", lambda m: True)
        monkeypatch.setattr(notifier, "configured_channels", lambda: [])
        results = notifier.dispatch_text("hello")
        assert results == {"slack": True, "discord": False, "telegram": True}

    def test_sends_run_concurrently_not_sequentially(self, monkeypatch):
        """Race mode's ping latency is max(channel latency), not the sum --
        three sends that each sleep 0.15s must finish in well under 0.45s."""
        import time

        def slow_send(m):
            time.sleep(0.15)
            return True

        monkeypatch.setattr(notifier, "_send_slack", slow_send)
        monkeypatch.setattr(notifier, "_send_discord", slow_send)
        monkeypatch.setattr(notifier, "_send_telegram", slow_send)
        monkeypatch.setattr(notifier, "configured_channels", lambda: [])
        started = time.monotonic()
        notifier.dispatch_text("hello")
        assert time.monotonic() - started < 0.4


class TestConfiguredChannels:
    def test_reflects_which_channels_have_credentials(self, monkeypatch):
        import monitor.config as config

        fake_cfg = config.Config(
            slack_webhook_url="https://hooks.slack.com/x",
            discord_webhook_url="",
            telegram_bot_token="",
            telegram_chat_id="",
        )
        monkeypatch.setattr(notifier, "get_config", lambda: fake_cfg)
        assert notifier.configured_channels() == ["slack"]

    def test_telegram_requires_both_token_and_chat_id(self, monkeypatch):
        import monitor.config as config

        fake_cfg = config.Config(telegram_bot_token="token-only", telegram_chat_id="")
        monkeypatch.setattr(notifier, "get_config", lambda: fake_cfg)
        assert notifier.configured_channels() == []

    def test_no_channels_configured(self, monkeypatch):
        import monitor.config as config

        monkeypatch.setattr(notifier, "get_config", lambda: config.Config())
        assert notifier.configured_channels() == []
