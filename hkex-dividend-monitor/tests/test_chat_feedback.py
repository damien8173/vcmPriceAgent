import json

import pytest

import monitor.chat_feedback as chat_feedback

# CHAT_FEEDBACK_FILE is already redirected to tmp_path for every test by
# the autouse _isolate_diagnostics_log fixture in conftest.py.


def _read_lines():
    with open(chat_feedback.CHAT_FEEDBACK_FILE, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class TestRecordFeedback:
    def test_appends_one_self_contained_json_line(self):
        chat_feedback.record_feedback(
            "When is X's next board meeting?",
            "It is on 2026-08-12.",
            note="wrong date",
            tool_activity=[{"tool": "get_upcoming_board_meetings", "args": {}, "result": {"count": 1}}],
            prior_transcript=[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
        )
        entries = _read_lines()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["userMessage"] == "When is X's next board meeting?"
        assert entry["reply"] == "It is on 2026-08-12."
        assert entry["note"] == "wrong date"
        assert entry["toolActivity"][0]["tool"] == "get_upcoming_board_meetings"
        assert entry["priorTranscript"][0] == {"role": "user", "text": "hi"}
        assert entry["flaggedAt"].endswith("+08:00")  # HKT, not UTC/server-local

    def test_optional_fields_omitted_when_absent(self):
        chat_feedback.record_feedback("q", "a")
        entry = _read_lines()[0]
        assert set(entry) == {"flaggedAt", "userMessage", "reply"}

    def test_entries_accumulate_in_order(self):
        chat_feedback.record_feedback("first", "a1")
        chat_feedback.record_feedback("second", "a2")
        assert [e["userMessage"] for e in _read_lines()] == ["first", "second"]

    def test_implausibly_large_entry_is_rejected_not_written(self):
        with pytest.raises(ValueError, match="large"):
            chat_feedback.record_feedback("q", "x" * (chat_feedback.MAX_ENTRY_BYTES + 100))
        assert not chat_feedback.CHAT_FEEDBACK_FILE.exists()


class TestFeedbackStats:
    def test_missing_file_is_zero_not_an_error(self):
        assert chat_feedback.feedback_stats() == {"count": 0, "bytes": 0}

    def test_counts_entries_and_bytes(self):
        chat_feedback.record_feedback("q1", "a1")
        chat_feedback.record_feedback("q2", "a2")
        stats = chat_feedback.feedback_stats()
        assert stats["count"] == 2
        assert stats["bytes"] == chat_feedback.CHAT_FEEDBACK_FILE.stat().st_size


class TestClearFeedback:
    def test_removes_file_and_reports_discarded_count(self):
        chat_feedback.record_feedback("q1", "a1")
        chat_feedback.record_feedback("q2", "a2")
        assert chat_feedback.clear_feedback() == 2
        assert not chat_feedback.CHAT_FEEDBACK_FILE.exists()
        assert chat_feedback.feedback_stats() == {"count": 0, "bytes": 0}

    def test_already_missing_is_zero_not_an_error(self):
        assert chat_feedback.clear_feedback() == 0
