import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_diagnostics_log(tmp_path, monkeypatch):
    """Prevent any test, anywhere in the suite, from ever writing to the
    real data/diagnostics.log, data/activity.log, or data/chat_feedback.log.
    Plenty of tests induce a failure in code under test as a side effect of
    what they're actually testing (e.g. "a search failure is logged and
    swallowed"), and monitor.diagnostics.log_error /
    monitor.activity.log_event / monitor.chat_feedback.record_feedback
    would otherwise write those synthetic events into the real,
    Docker-bind-mounted data files -- indistinguishable later from genuine
    production activity."""
    import monitor.activity as activity
    import monitor.chat_feedback as chat_feedback
    import monitor.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "DIAGNOSTICS_FILE", tmp_path / "diagnostics.log")
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.log")
    monkeypatch.setattr(chat_feedback, "CHAT_FEEDBACK_FILE", tmp_path / "chat_feedback.log")
