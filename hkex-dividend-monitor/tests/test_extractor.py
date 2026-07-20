import json

import pytest

import monitor.config as config
import monitor.extractor as extractor
from monitor.extractor import ExtractionError


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """extract_dividend_info/_escalate read get_config().deepseek_model and
    .deepseek_reasoning_model directly (not just inside the faked _client()),
    so every test here must never resolve against the real data/settings.json
    -- same isolation as tests/test_config.py's fixture of the same name."""
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


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """content: fixed content returned for every call. responses: a queue
    consumed one per call; once exhausted, raises `exc` if given (lets a
    test make the fast-tier call succeed and the escalation call fail).
    Every call's kwargs are recorded in .calls, so a test can assert which
    model/params each attempt used."""

    def __init__(self, content=None, exc=None, responses=None):
        self._content = content
        self._exc = exc
        self._responses = list(responses) if responses is not None else None
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._responses:
            return _FakeResponse(self._responses.pop(0))
        if self._exc:
            raise self._exc
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, content=None, exc=None, responses=None):
        self.chat = _FakeChat(_FakeCompletions(content=content, exc=exc, responses=responses))


_BASE_PAYLOAD = {
    "is_dividend_announcement": True,
    "company_name": "Example Holdings",
    "payout_amount": "HKD 0.45",
    "ex_dividend_date": "2026-08-01",
    "payment_date": "2026-08-15",
}


def _payload(**overrides):
    return {**_BASE_PAYLOAD, **overrides}


class TestExtractDividendInfo:
    def test_empty_document_raises_without_calling_llm(self, isolated_config):
        with pytest.raises(ExtractionError):
            extractor.extract_dividend_info("fid", "   ")

    def test_valid_response_parses_into_model(self, isolated_config, monkeypatch):
        client = _FakeClient(content=json.dumps(_payload()))
        monkeypatch.setattr(extractor, "_client", lambda *a, **k: client)
        result = extractor.extract_dividend_info("fid", "some filing text")
        assert result.is_dividend_announcement is True
        assert result.payout_amount == "HKD 0.45"
        assert result.ambiguous is False  # defaults false when the model omits it

    def test_malformed_json_raises_extraction_error(self, isolated_config, monkeypatch):
        monkeypatch.setattr(extractor, "_client", lambda *a, **k: _FakeClient(content="not json"))
        with pytest.raises(ExtractionError):
            extractor.extract_dividend_info("fid", "text")

    def test_schema_violation_raises_extraction_error(self, isolated_config, monkeypatch):
        monkeypatch.setattr(
            extractor, "_client", lambda *a, **k: _FakeClient(content=json.dumps({"is_dividend_announcement": "not-a-bool"}))
        )
        with pytest.raises(ExtractionError):
            extractor.extract_dividend_info("fid", "text")

    def test_api_error_raises_extraction_error(self, isolated_config, monkeypatch):
        monkeypatch.setattr(extractor, "_client", lambda *a, **k: _FakeClient(exc=RuntimeError("network down")))
        with pytest.raises(ExtractionError):
            extractor.extract_dividend_info("fid", "text")


class TestEscalation:
    def test_unambiguous_result_does_not_escalate(self, isolated_config, monkeypatch):
        client = _FakeClient(content=json.dumps(_payload(ambiguous=False)))
        monkeypatch.setattr(extractor, "_client", lambda *a, **k: client)
        result = extractor.extract_dividend_info("fid", "text")
        assert result.ambiguous is False
        assert len(client.chat.completions.calls) == 1

    def test_ambiguous_result_escalates_to_reasoning_model(self, isolated_config, monkeypatch):
        _write_settings(isolated_config, {"deepseek_reasoning_model": "custom-pro-model"})
        fast = _payload(ambiguous=True, payout_amount="unclear")
        reasoned = _payload(ambiguous=False, payout_amount="HKD 0.45")
        client = _FakeClient(responses=[json.dumps(fast), json.dumps(reasoned)])
        monkeypatch.setattr(extractor, "_client", lambda *a, **k: client)

        result = extractor.extract_dividend_info("fid", "text")

        assert result.payout_amount == "HKD 0.45"  # the escalated, resolved answer
        assert len(client.chat.completions.calls) == 2
        fast_call, escalated_call = client.chat.completions.calls
        assert escalated_call["model"] == "custom-pro-model"
        assert escalated_call["reasoning_effort"] == "high"
        assert escalated_call["extra_body"] == {"thinking": {"type": "enabled"}}
        assert "temperature" not in escalated_call  # thinking mode ignores it -- omitted, not just 0
        assert fast_call["model"] != escalated_call["model"]

    def test_failed_escalation_falls_back_to_fast_result(self, isolated_config, monkeypatch):
        fast = _payload(ambiguous=True)
        client = _FakeClient(responses=[json.dumps(fast)], exc=RuntimeError("reasoning tier unreachable"))
        monkeypatch.setattr(extractor, "_client", lambda *a, **k: client)

        result = extractor.extract_dividend_info("fid", "text")

        assert result.ambiguous is True  # the original fast-tier result, unchanged
        assert result.payout_amount == _BASE_PAYLOAD["payout_amount"]
        assert len(client.chat.completions.calls) == 2  # escalation was attempted
