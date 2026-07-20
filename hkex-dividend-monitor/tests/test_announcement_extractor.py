import json

import pytest

import monitor.announcement_extractor as ann
import monitor.config as config
import monitor.extractor as extractor_module
from monitor.extractor import ExtractionError


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """extract_announcement's escalation path reads get_config().deepseek_
    reasoning_model directly, so tests asserting on it must never resolve
    against the real data/settings.json -- same isolation as
    tests/test_config.py's fixture of the same name."""
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
    Every call's kwargs are recorded in .calls for assertions."""

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


class TestClassifyTitle:
    def test_board_meeting_keyword(self):
        assert ann.classify_title("Notice of Board Meeting") == "board_meeting"

    def test_results_keyword(self):
        assert ann.classify_title("Announcement of Annual Results") == "results"

    def test_dividend_keyword(self):
        assert ann.classify_title("Declaration of Interim Dividend") == "dividend"

    def test_unrelated_title_is_other(self):
        assert ann.classify_title("Change of Company Secretary") == "other"

    def test_empty_title_is_other(self):
        assert ann.classify_title("") == "other"


class TestExtractAnnouncement:
    def test_empty_document_raises_without_calling_llm(self):
        with pytest.raises(ExtractionError):
            ann.extract_announcement("fid", "Board Meeting Notice", "   ")

    def test_valid_response_parses_into_model(self, monkeypatch):
        payload = {
            "event_kind": "board_meeting",
            "company_name": "Example Holdings",
            "board_meeting_date": "2026-07-20",
            "board_meeting_purpose_approves_results": True,
            "board_meeting_purpose_considers_dividend": True,
            "board_meeting_purpose_raw": "to approve results and consider dividend",
            "results_period": "annual",
            "dividend_type": None,
            "dividend_amount": None,
            "declared_date": None,
            "ex_date": None,
            "record_date": None,
            "payment_date": None,
        }
        monkeypatch.setattr(ann, "_client", lambda: _FakeClient(content=json.dumps(payload)))
        result = ann.extract_announcement("fid", "Notice of Board Meeting", "some filing text")
        assert result.event_kind == "board_meeting"
        assert result.board_meeting_date == "2026-07-20"
        assert result.board_meeting_purpose_approves_results is True

    def test_malformed_json_raises_extraction_error(self, monkeypatch):
        monkeypatch.setattr(ann, "_client", lambda: _FakeClient(content="not json"))
        with pytest.raises(ExtractionError):
            ann.extract_announcement("fid", "title", "text")

    def test_schema_violation_raises_extraction_error(self, monkeypatch):
        # event_kind should be a string; a nested object fails Pydantic validation.
        monkeypatch.setattr(ann, "_client", lambda: _FakeClient(content=json.dumps({"event_kind": {"bad": 1}})))
        with pytest.raises(ExtractionError):
            ann.extract_announcement("fid", "title", "text")

    def test_api_error_raises_extraction_error(self, monkeypatch):
        monkeypatch.setattr(ann, "_client", lambda: _FakeClient(exc=RuntimeError("network down")))
        with pytest.raises(ExtractionError):
            ann.extract_announcement("fid", "title", "text")

    def test_missing_fields_default_safely(self, monkeypatch):
        monkeypatch.setattr(ann, "_client", lambda: _FakeClient(content=json.dumps({"event_kind": "other"})))
        result = ann.extract_announcement("fid", "title", "text")
        assert result.event_kind == "other"
        assert result.board_meeting_date is None
        assert result.board_meeting_purpose_approves_results is False


_BASE_ANN_PAYLOAD = {
    "event_kind": "dividend",
    "company_name": "Example Holdings",
    "board_meeting_date": None,
    "board_meeting_purpose_approves_results": False,
    "board_meeting_purpose_considers_dividend": False,
    "board_meeting_purpose_raw": None,
    "results_period": None,
    "dividend_type": "final",
    "dividend_amount": "HKD 0.45",
    "declared_date": "2026-07-20",
    "ex_date": "2026-08-01",
    "record_date": "2026-08-05",
    "payment_date": "2026-08-15",
}


def _ann_payload(**overrides):
    return {**_BASE_ANN_PAYLOAD, **overrides}


class TestExtractAnnouncementEscalation:
    """extract_announcement's own fast-tier call uses the `_client` name
    imported into monitor.announcement_extractor's namespace, but the
    escalation attempt (monitor.extractor._escalate) looks `_client` up in
    monitor.extractor's own namespace instead -- a plain `from ... import
    _client` copies the reference at import time, it doesn't alias the two
    modules' names together. Both must be patched to control both calls."""

    def test_unambiguous_result_does_not_escalate(self, isolated_config, monkeypatch):
        client = _FakeClient(content=json.dumps(_ann_payload(ambiguous=False)))
        monkeypatch.setattr(ann, "_client", lambda *a, **k: client)
        result = ann.extract_announcement("fid", "title", "some filing text")
        assert result.ambiguous is False
        assert len(client.chat.completions.calls) == 1

    def test_ambiguous_result_escalates_to_reasoning_model(self, isolated_config, monkeypatch):
        _write_settings(isolated_config, {"deepseek_reasoning_model": "custom-pro-model"})
        fast = _ann_payload(ambiguous=True, dividend_amount="unclear")
        reasoned = _ann_payload(ambiguous=False, dividend_amount="HKD 0.45")
        client = _FakeClient(responses=[json.dumps(fast), json.dumps(reasoned)])
        monkeypatch.setattr(ann, "_client", lambda *a, **k: client)
        monkeypatch.setattr(extractor_module, "_client", lambda *a, **k: client)

        result = ann.extract_announcement("fid", "title", "some filing text")

        assert result.dividend_amount == "HKD 0.45"  # the escalated, resolved answer
        assert len(client.chat.completions.calls) == 2
        fast_call, escalated_call = client.chat.completions.calls
        assert escalated_call["model"] == "custom-pro-model"
        assert escalated_call["reasoning_effort"] == "high"
        assert "temperature" not in escalated_call
        assert fast_call["model"] != escalated_call["model"]

    def test_failed_escalation_falls_back_to_fast_result(self, isolated_config, monkeypatch):
        fast = _ann_payload(ambiguous=True)
        client = _FakeClient(responses=[json.dumps(fast)], exc=RuntimeError("reasoning tier down"))
        monkeypatch.setattr(ann, "_client", lambda *a, **k: client)
        monkeypatch.setattr(extractor_module, "_client", lambda *a, **k: client)

        result = ann.extract_announcement("fid", "title", "text")

        assert result.ambiguous is True  # the original fast-tier result, unchanged
        assert result.dividend_amount == _BASE_ANN_PAYLOAD["dividend_amount"]
        assert len(client.chat.completions.calls) == 2  # escalation was attempted


class TestExplain:
    def test_empty_reasons_returns_none_without_calling_llm(self):
        assert ann.explain("Example Co", "00700", []) is None

    def test_returns_stripped_content_on_success(self, monkeypatch):
        monkeypatch.setattr(ann, "_client", lambda: _FakeClient(content="  Board meets today to approve results.  "))
        text = ann.explain("Example Co", "00700", [{"signal": "x", "label": "Official", "evidence": "y"}])
        assert text == "Board meets today to approve results."

    def test_failure_returns_none_not_raise(self, monkeypatch):
        monkeypatch.setattr(ann, "_client", lambda: _FakeClient(exc=RuntimeError("boom")))
        result = ann.explain("Example Co", "00700", [{"signal": "x", "label": "Official", "evidence": "y"}])
        assert result is None
