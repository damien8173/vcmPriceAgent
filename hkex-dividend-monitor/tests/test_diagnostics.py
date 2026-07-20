import json

import monitor.activity as activity
import monitor.diagnostics as diagnostics


def test_log_error_writes_json_line(tmp_path, monkeypatch):
    log_file = tmp_path / "diagnostics.log"
    monkeypatch.setattr(diagnostics, "DIAGNOSTICS_FILE", log_file)
    diagnostics.log_error("test.source", "something went wrong")
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["source"] == "test.source"
    assert entry["message"] == "something went wrong"
    assert "exception" not in entry


def test_log_error_includes_traceback_when_exc_given(tmp_path, monkeypatch):
    log_file = tmp_path / "diagnostics.log"
    monkeypatch.setattr(diagnostics, "DIAGNOSTICS_FILE", log_file)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        diagnostics.log_error("test.source", "failed", exc)
    entry = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert "ValueError" in entry["exception"]
    assert "boom" in entry["exception"]


def test_rotation_keeps_file_size_bounded(tmp_path, monkeypatch):
    log_file = tmp_path / "diagnostics.log"
    monkeypatch.setattr(diagnostics, "DIAGNOSTICS_FILE", log_file)
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTICS_BYTES", 2000)
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTICS_LINES", 10)

    for i in range(200):
        diagnostics.log_error("test.source", f"message number {i} " + "x" * 20)

    size = log_file.stat().st_size
    # Without rotation this would be ~200 * 60 bytes =~ 12000 bytes; rotation
    # should keep it oscillating near the byte cap instead.
    assert size < diagnostics.MAX_DIAGNOSTICS_BYTES * 2

    lines = log_file.read_text(encoding="utf-8").splitlines()
    last_entry = json.loads(lines[-1])
    assert "message number 199" in last_entry["message"]


def test_rotation_never_raises_if_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "DIAGNOSTICS_FILE", tmp_path / "does_not_exist.log")
    diagnostics._rotate_if_needed()  # must not raise


def test_log_error_mirrors_an_error_event_into_activity_log(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "DIAGNOSTICS_FILE", tmp_path / "diagnostics.log")
    activity_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", activity_file)

    diagnostics.log_error("test.source", "something went wrong")

    lines = activity_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["source"] == "test.source"
    assert entry["message"] == "something went wrong"
    assert entry["level"] == "error"
    assert entry["kind"] == "error"
