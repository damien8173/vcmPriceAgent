import json

import monitor.activity as activity


def test_log_event_writes_json_line(tmp_path, monkeypatch):
    log_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log_file)

    activity.log_event("daemon.race", "hkex.refresh", "HKEX refresh 00700: 1 filing today")

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["source"] == "daemon.race"
    assert entry["kind"] == "hkex.refresh"
    assert entry["message"] == "HKEX refresh 00700: 1 filing today"
    assert entry["level"] == "info"
    assert "ticker" not in entry
    assert "meta" not in entry


def test_log_event_includes_ticker_and_meta_when_given(tmp_path, monkeypatch):
    log_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log_file)

    activity.log_event(
        "daemon.race", "hkex.refresh", "HKEX refresh 00700: 1 filing today",
        level="debug", ticker="00700", meta={"count": 1, "duration_ms": 42},
    )

    entry = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert entry["level"] == "debug"
    assert entry["ticker"] == "00700"
    assert entry["meta"] == {"count": 1, "duration_ms": 42}


def test_log_event_never_raises_on_unwritable_path(tmp_path, monkeypatch):
    # A path whose parent is a file (not a directory) can never be created.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(activity, "ACTIVITY_FILE", blocker / "activity.log")

    activity.log_event("test.source", "kind", "message")  # must not raise


def test_rotation_keeps_file_size_bounded(tmp_path, monkeypatch):
    log_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log_file)
    monkeypatch.setattr(activity, "MAX_ACTIVITY_BYTES", 2000)
    monkeypatch.setattr(activity, "MAX_ACTIVITY_LINES", 10)

    for i in range(200):
        activity.log_event("test.source", "kind", f"message number {i} " + "x" * 20)

    size = log_file.stat().st_size
    assert size < activity.MAX_ACTIVITY_BYTES * 2

    lines = log_file.read_text(encoding="utf-8").splitlines()
    last_entry = json.loads(lines[-1])
    assert "message number 199" in last_entry["message"]


def test_rotation_never_raises_if_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "does_not_exist.log")
    activity._rotate_if_needed()  # must not raise


def test_read_recent_returns_empty_list_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "does_not_exist.log")
    assert activity.read_recent() == []


def test_read_recent_returns_newest_first(tmp_path, monkeypatch):
    log_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log_file)

    for i in range(5):
        activity.log_event("test.source", "kind", f"message {i}")

    events = activity.read_recent()
    assert [e["message"] for e in events] == [
        "message 4", "message 3", "message 2", "message 1", "message 0",
    ]


def test_read_recent_respects_limit(tmp_path, monkeypatch):
    log_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log_file)

    for i in range(20):
        activity.log_event("test.source", "kind", f"message {i}")

    events = activity.read_recent(limit=3)
    assert len(events) == 3
    assert events[0]["message"] == "message 19"


def test_read_recent_filters_by_min_level(tmp_path, monkeypatch):
    log_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log_file)

    activity.log_event("s", "k", "a debug event", level="debug")
    activity.log_event("s", "k", "an info event", level="info")
    activity.log_event("s", "k", "a warn event", level="warn")
    activity.log_event("s", "k", "an error event", level="error")

    events = activity.read_recent(min_level="warn")
    assert [e["message"] for e in events] == ["an error event", "a warn event"]


def test_read_recent_skips_malformed_lines(tmp_path, monkeypatch):
    log_file = tmp_path / "activity.log"
    monkeypatch.setattr(activity, "ACTIVITY_FILE", log_file)

    activity.log_event("s", "k", "good event one")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("not valid json\n")
    activity.log_event("s", "k", "good event two")

    events = activity.read_recent()
    assert [e["message"] for e in events] == ["good event two", "good event one"]
