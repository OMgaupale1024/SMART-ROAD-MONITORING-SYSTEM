"""The history store (roadsense.web.history) and its export (roadsense.web.history_export): what survives a restart,
how identities stay apart, what every failure does, retention, the dataset, and that the safety output is the same
with the store on and off.

Failures are real SQLite and filesystem errors where that is safe (a held lock, a read-only file, a path under a
regular file, a corrupt file, SQLite's own page limit for SQLITE_FULL, a SIGKILLed process); a failing writer thread
and a slow database open are injected by replacing a method or the connect function.
"""
import importlib.util
import json
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from roadsense.web import history, history_export, live

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bench_history", ROOT / "tools/bench_history.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def config(directory, **changes):
    """Tests publish far faster than 5 Hz: a queue that never fills, unless a test is about a full one."""
    return history.Config(enabled=True, directory=str(directory), flush_s=0.05, **dict({"queue_size": 100_000}, **changes))


def feed(recorder, frames, hub=None):
    """Publish frames through a hub whose listener is the recorder; returns the hub."""
    hub = hub or live.LiveStateHub(listeners=[recorder.observe])
    for frame in frames:
        hub.ingest_sensors(json.dumps(frame).encode())
    return hub


def record(directory, frames, **changes):
    recorder = history.HistoryRecorder(config(directory, **changes))
    feed(recorder, frames)
    recorder.close()
    return recorder


def db(directory):
    conn = sqlite3.connect(str(Path(directory) / history.DB_NAME))
    conn.row_factory = sqlite3.Row
    return conn


def rows(directory, sql, *args):
    with db(directory) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(history, "RETRY_S", (0.05, 0.2))
    monkeypatch.setattr(history, "BUSY_TIMEOUT_S", 0.2)


# --- configuration


def test_history_is_off_unless_enabled_and_never_writes_then(tmp_path):
    assert history.config_from_environment({}) == history.Config()
    recorder = history.HistoryRecorder(history.Config(directory=str(tmp_path / "h")))
    feed(recorder, bench.scenario(2))
    recorder.close()
    assert not (tmp_path / "h").exists()
    assert recorder.status()["state"] == "disabled" and recorder.status()["lost"]["inactive"] == 0


def test_configuration_from_the_environment_refuses_unusable_values(tmp_path):
    warnings = []
    c = history.config_from_environment({
        "ROADSENSE_HISTORY_ENABLE": "1", "ROADSENSE_HISTORY_DATASET": "yes", "ROADSENSE_HISTORY_DIR": str(tmp_path),
        "ROADSENSE_HISTORY_RETENTION_DAYS": "7", "ROADSENSE_HISTORY_MAX_MB": "0", "ROADSENSE_HISTORY_QUEUE": "x"},
        warnings.append)
    assert c.enabled and c.dataset and c.directory == str(tmp_path) and c.retention_days == 7
    assert c.max_mb == history.Config().max_mb and c.queue_size == history.Config().queue_size
    assert len(warnings) == 2
    relative = history.config_from_environment({"ROADSENSE_HISTORY_DIR": "data/history"}, warnings.append)
    assert relative.directory == "" and "absolute" in warnings[-1]  # never inside the checkout the service runs in


def test_default_directory_is_the_platforms_state_directory():
    home = Path.home()
    assert history.default_directory({}, "Linux") == home / ".local/state/roadsense/history"
    assert history.default_directory({"XDG_STATE_HOME": "/srv/state"}, "Linux") == Path("/srv/state/roadsense/history")
    assert history.default_directory({"XDG_STATE_HOME": "relative"}, "Linux") == home / ".local/state/roadsense/history"
    assert history.default_directory({}, "Darwin") == home / "Library/Application Support/RoadSense/history"


# --- the event policy


def _hazard(**changes):
    values = {"type": "pothole", "severity": "HIGH", "confidence": 0.9, "observations": 1, "status": "ACTIVE",
              "x_m": -400.0, "y_m": 7.0, "first_seen_s": 10.0, "last_seen_s": 10.0, "dimensions": None,
              "sensor": "s", "vehicle_id": "EGO"}
    values.update(changes)
    return values


def test_only_material_hazard_changes_make_rows():
    first = _hazard()
    assert history.hazard_change(None, first, 10.0) == "created"
    assert history.hazard_change(first, _hazard(confidence=0.92, observations=3, last_seen_s=10.4, x_m=-400.2), 10.4) is None
    assert history.hazard_change(first, _hazard(confidence=0.96), 10.6) == "confidence"
    assert history.hazard_change(first, _hazard(severity="MEDIUM", x_m=-400.6), 10.6) == "severity+position"
    assert history.hazard_change(first, _hazard(status="INACTIVE"), 10.6) == "status"
    passed = _hazard(observations=12, last_seen_s=12.0)
    assert history.hazard_change(first, passed, 16.9) is None
    assert history.hazard_change(first, passed, 17.0) == "settled"  # 5 s of simulation without a new observation
    assert history.hazard_change(first, _hazard(x_m=None), 11.0) == "position"


# --- persistence and identity


def test_a_new_database_is_versioned_and_uses_write_ahead_logging(tmp_path):
    record(tmp_path, bench.scenario(4))
    with db(tmp_path) as conn:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == history.APPLICATION_ID
        assert conn.execute("PRAGMA user_version").fetchone()[0] == history.SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert dict(conn.execute("SELECT key, value FROM meta").fetchall())["schema"] == history.SCHEMA
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_history_survives_a_restart_and_two_lifetimes_of_one_run_never_share_a_hazard(tmp_path):
    frames = list(bench.scenario(40, "run-x", end=False))
    first = record(tmp_path, frames[:120])  # ends at sequence 120
    second = record(tmp_path, frames[130:])  # the same producer run, a new processor: hazard ids restart
    processors = rows(tmp_path, "SELECT * FROM processors ORDER BY started_utc")
    assert [p["processor_id"] for p in processors] == [first.processor_id, second.processor_id]
    assert all(p["end_reason"] == "clean" and p["ended_utc"] for p in processors)
    segments = rows(tmp_path, "SELECT * FROM segments ORDER BY first_utc")
    assert [s["run_id"] for s in segments] == ["run-x", "run-x"]
    assert [s["processor_id"] for s in segments] == [first.processor_id, second.processor_id]
    assert (segments[0]["first_sequence"], segments[0]["last_sequence"]) == (1, 120)
    created = rows(tmp_path, "SELECT segment_id, local_id, x_m FROM hazard_updates WHERE change = 'created' "
                             "ORDER BY segment_id, local_id")
    by_segment = {}
    for r in created:
        by_segment.setdefault(r["segment_id"], {})[r["local_id"]] = r["x_m"]
    one, two = (by_segment[s["segment_id"]] for s in segments)
    assert "PH_001" in one and "PH_001" in two and abs(one["PH_001"] - two["PH_001"]) > 100  # different potholes
    resumed, = rows(tmp_path, "SELECT * FROM discontinuities WHERE kind = 'run_resumed'")
    assert resumed["basis"] == "inferred" and resumed["segment_id"] == segments[1]["segment_id"]
    assert (resumed["from_sequence"], resumed["to_sequence"], resumed["missing_sequences"]) == (120, 131, 10)
    assert json.loads(resumed["detail"])["previous_processor_id"] == first.processor_id


def test_one_processor_gives_each_new_pipeline_its_own_segment(tmp_path):
    """Run a, run b, run a again, then a precomputed state of a, then edge again: the hub restarts the pipeline each
    time, so local ids restart, and each time is a segment of its own."""
    recorder = history.HistoryRecorder(config(tmp_path))
    hub = feed(recorder, bench.scenario(20, "a", end=False))
    feed(recorder, bench.scenario(20, "b", end=False), hub)
    feed(recorder, bench.scenario(20, "a", start_sequence=200, end=False), hub)
    precomputed = json.loads(hub.latest_json)
    precomputed["simulation"]["sequence"] = 400
    hub.ingest(json.dumps(precomputed).encode())
    feed(recorder, bench.scenario(20, "a", start_sequence=500, end=False), hub)
    recorder.close()
    segments = rows(tmp_path, "SELECT run_id, processing, first_sequence FROM segments ORDER BY segment_id")
    assert [(s["run_id"], s["processing"], s["first_sequence"]) for s in segments] == [
        ("a", "edge", 1), ("b", "edge", 1), ("a", "edge", 200), ("a", "precomputed", 400), ("a", "edge", 500)]
    kinds = [r["kind"] for r in rows(tmp_path, "SELECT kind FROM discontinuities ORDER BY id")]
    assert kinds.count("run_change") == 4 and kinds.count("run_reentered") == 3
    created = rows(tmp_path, "SELECT segment_id FROM hazard_updates WHERE local_id = 'PH_001' AND change = 'created'")
    assert len({r["segment_id"] for r in created}) == 5  # PH_001 of five pipelines: five historical hazards


def test_a_genuine_duplicate_is_one_event_and_a_different_observation_is_never_overwritten(tmp_path):
    recorder = history.HistoryRecorder(config(tmp_path))
    hub = feed(recorder, bench.scenario(4, end=False))
    state = json.loads(hub.latest_json)
    recorder.observe(state)  # the same state again
    recorder.close()
    decisions = rows(tmp_path, "SELECT segment_id, sequence, trigger FROM decisions ORDER BY sequence")
    assert len({(d["segment_id"], d["sequence"]) for d in decisions}) == len(decisions)
    with db(tmp_path) as conn:
        before = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
        d = decisions[0]
        conn.execute(history._INSERTS["decisions"], (d["segment_id"], d["sequence"], 0.0, 0.0, "change")
                     + ("CRITICAL",) + (None,) * (len(history.DECISION_FIELDS) - 1))
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == before
        assert conn.execute("SELECT risk FROM decisions WHERE sequence = ?", (d["sequence"],)).fetchone()[0] != "CRITICAL"


def test_a_published_state_is_never_mutated_by_later_frames():
    # The recorder queues references, not copies: this is what makes that safe.
    import copy
    held = []
    hub = live.LiveStateHub(listeners=[lambda state: held.append((state, copy.deepcopy(state)))])
    for frame in bench.scenario(60):
        hub.ingest_sensors(json.dumps(frame).encode())
    assert len(held) == 301  # every frame, and the end marker's state
    assert all(state == snapshot for state, snapshot in held)


def test_decisions_are_kept_on_change_in_order_with_their_threat(tmp_path):
    record(tmp_path, bench.scenario(60))
    decisions = rows(tmp_path, "SELECT * FROM decisions ORDER BY id")
    assert [d["sequence"] for d in decisions] == sorted(d["sequence"] for d in decisions)
    assert decisions[0]["trigger"] == "first" and decisions[-1]["trigger"] == "final"
    actions = {d["action"] for d in decisions}
    assert {"MAINTAIN", "BRAKE", "EMERGENCY_BRAKE"} <= actions
    braking = [d for d in decisions if d["action"] in ("BRAKE", "EMERGENCY_BRAKE")]
    vehicles = [d for d in braking if d["threat_type"] == "vehicle"]
    potholes = [d for d in braking if d["threat_type"] == "road_hazard"]
    assert vehicles and potholes and len(vehicles) + len(potholes) == len(braking)
    assert all(d["threat_ttc_s"] is not None and d["threat_rel_lon_mps"] < 0 for d in vehicles)
    assert all(d["threat_id"].startswith("PH_") and d["threat_distance_m"] > 0 and d["threat_ttc_s"] is None
               for d in potholes)
    assert len(decisions) < 60  # transitions, not 300 frames
    ended, = rows(tmp_path, "SELECT * FROM discontinuities WHERE kind = 'run_ended'")
    assert rows(tmp_path, "SELECT ended FROM segments")[0]["ended"] == 1 and ended["to_sequence"] == 301


def test_sequence_gaps_and_stalls_are_recorded_as_what_was_observed(tmp_path):
    frames = list(bench.scenario(10, end=False))
    recorder = history.HistoryRecorder(config(tmp_path, dataset=True))
    clock = [1000.0]
    recorder._clock = lambda: clock[0]  # the processor's monotonic clock, advanced by hand
    hub = live.LiveStateHub(listeners=[recorder.observe])
    for frame in frames[:10] + frames[15:20] + frames[20:]:
        clock[0] += 3.0 if frame is frames[20] else 0.2
        hub.ingest_sensors(json.dumps(frame).encode())
    recorder._clock = time.monotonic
    recorder.close()
    gap, = rows(tmp_path, "SELECT * FROM discontinuities WHERE kind = 'sequence_gap'")
    assert (gap["from_sequence"], gap["to_sequence"], gap["missing_sequences"], gap["basis"]) == (10, 16, 5, "observed")
    stall, = rows(tmp_path, "SELECT * FROM discontinuities WHERE kind = 'stream_stall'")
    assert stall["mono_gap_s"] == 3.0 and stall["sim_gap_s"] == 0.2  # two clocks, each reported on its own
    sample = rows(tmp_path, "SELECT features FROM samples WHERE sequence = 16")[0]
    f = json.loads(sample["features"])
    assert f["sequence_gap_before"] == 5 and f["history_dropped_before"] == 0 and f["sim_dt_s"] == 1.2


# --- failures


def test_a_full_queue_drops_and_counts_without_blocking(tmp_path):
    gate = threading.Event()

    def slow_connect(*args, **kwargs):
        gate.wait(10)
        return sqlite3.connect(*args, **kwargs)
    recorder = history.HistoryRecorder(config(tmp_path, queue_size=16), connect=slow_connect)
    hub = live.LiveStateHub(listeners=[recorder.observe])
    frames = list(bench.scenario(20, end=False))
    started = time.perf_counter()
    feed(recorder, frames, hub)
    elapsed = time.perf_counter() - started
    assert recorder.status()["lost"]["queue_full"] == len(frames) - 16
    assert json.loads(hub.latest_json)["simulation"]["sequence"] == len(frames)  # every frame was published
    assert elapsed < 5.0
    gate.set()
    recorder.close()
    status = recorder.status()
    assert status["states"]["absorbed"] == 16 and status["queue"]["high_water"] == 16
    loss = rows(tmp_path, "SELECT sum(count) AS n FROM discontinuities WHERE kind = 'history_loss'")[0]["n"]
    assert loss == len(frames) - 16


def test_disk_full_is_reported_and_the_pipeline_goes_on(tmp_path, fast):
    def small(*args, **kwargs):
        conn = sqlite3.connect(*args, **kwargs)
        conn.execute("PRAGMA max_page_count = 40")  # SQLite's own limit: a real SQLITE_FULL, no real disk filled
        return conn
    recorder = history.HistoryRecorder(config(tmp_path, dataset=True), connect=small)
    hub = feed(recorder, bench.scenario(60, end=False))
    wait_for(lambda: recorder.status()["batches"]["failed"] > 0)
    status = recorder.status()
    assert status["last_error"]["kind"] == "disk_full" and status["losses_observed"]
    assert status["lost"]["uncommitted_rows"] > 0
    assert json.loads(hub.latest_json)["simulation"]["sequence"] == 300
    recorder.close()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file permissions that apply")
def test_a_read_only_database_or_directory_is_reported_not_raised(tmp_path, fast):
    record(tmp_path, bench.scenario(2))
    path = tmp_path / history.DB_NAME
    original = path.read_bytes()
    path.chmod(0o444)
    try:
        recorder = history.HistoryRecorder(config(tmp_path))
        feed(recorder, bench.scenario(2, "other"))
        wait_for(lambda: recorder.status()["last_error"] is not None)
        assert recorder.status()["last_error"]["kind"] == "read_only" and recorder.status()["state"] == "unavailable"
        recorder.close()
        assert path.read_bytes() == original
    finally:
        path.chmod(0o644)
    sub = tmp_path / "locked-dir"
    sub.mkdir()
    sub.chmod(0o555)
    try:
        recorder = history.HistoryRecorder(config(sub / "history"))
        wait_for(lambda: recorder.status()["last_error"] is not None)
        assert recorder.status()["last_error"]["kind"] == "cannot_open"
        recorder.close()
    finally:
        sub.chmod(0o755)


def test_an_invalid_directory_is_reported_and_retried(tmp_path, fast):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    recorder = history.HistoryRecorder(config(blocker / "history"))
    feed(recorder, bench.scenario(4))
    wait_for(lambda: recorder.status()["lost"]["unavailable"] > 0)
    status = recorder.status()
    assert status["state"] == "unavailable" and status["last_error"]["kind"] == "cannot_open"
    blocker.unlink()  # the operator fixes it: the next attempt succeeds
    wait_for(lambda: recorder.status()["state"] == "recording")
    recorder.close()


def test_a_held_lock_fails_a_batch_whole_and_the_writer_resynchronises_after(tmp_path, fast):
    recorder = history.HistoryRecorder(config(tmp_path))
    frames = list(bench.scenario(40, end=False))
    hub = feed(recorder, frames[:50])
    wait_for(lambda: recorder.status()["rows"]["decisions"] > 0)
    blocker = sqlite3.connect(str(tmp_path / history.DB_NAME), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        feed(recorder, frames[50:120], hub)  # includes the lead car's braking: new decisions to write
        wait_for(lambda: recorder.status()["batches"]["failed"] > 0)
        assert recorder.status()["last_error"]["kind"] == "locked"
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    wait_for(lambda: recorder.status()["state"] == "recording")
    feed(recorder, frames[120:], hub)
    recorder.close()
    kinds = [r["kind"] for r in rows(tmp_path, "SELECT kind FROM discontinuities")]
    assert "history_resumed" in kinds
    triggers = [r["trigger"] for r in rows(tmp_path, "SELECT trigger FROM decisions ORDER BY sequence")]
    assert "resync" in triggers  # what reached the database was unknown, so the current decision was written again
    assert rows(tmp_path, "SELECT count(*) AS n FROM hazard_updates WHERE change = 'resync'")[0]["n"] > 0
    # (a failed batch is rolled back whole: every decision row is a consistent transition or resync)
    assert recorder.status()["lost"]["uncommitted_rows"] > 0


def test_a_second_process_on_the_same_directory_is_refused(tmp_path, fast):
    first = history.HistoryRecorder(config(tmp_path))
    wait_for(lambda: first.status()["state"] == "recording")
    second = history.HistoryRecorder(config(tmp_path))
    wait_for(lambda: second.status()["last_error"] is not None)
    assert second.status()["last_error"]["kind"] == "in_use"
    second.close()
    first.close()


def test_an_unexpected_writer_error_is_counted_and_the_writer_recovers(tmp_path, fast):
    recorder = history.HistoryRecorder(config(tmp_path))
    real_flush = recorder._flush
    calls = []

    def flush_once_broken():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("injected writer fault")
        real_flush()
    recorder._flush = flush_once_broken
    frames = list(bench.scenario(20))
    hub = feed(recorder, frames[:50])
    wait_for(lambda: len(calls) >= 1 and recorder.status()["state"] == "recording")
    feed(recorder, frames[50:], hub)
    recorder.close()
    assert recorder.status()["batches"]["failed"] == 0 and recorder.status()["last_error"]["kind"] == "error"
    assert "history_resumed" in [r["kind"] for r in rows(tmp_path, "SELECT kind FROM discontinuities")]


def test_a_dead_writer_thread_stops_the_store_not_the_pipeline(tmp_path):
    recorder = history.HistoryRecorder(config(tmp_path))
    wait_for(lambda: recorder.status()["state"] == "recording")

    def die():
        raise KeyboardInterrupt  # not an Exception: nothing inside the writer catches it
    recorder._step = die
    recorder._queue.put_nowait(history._WAKE)
    wait_for(lambda: not recorder._thread.is_alive())
    hub = feed(recorder, bench.scenario(4))
    status = recorder.status()
    assert status["state"] == "stopped" and "writer thread ended" in status["reason"] and status["degraded"]
    assert status["lost"]["inactive"] == 21
    assert json.loads(hub.latest_json)["simulation"]["active"] is False  # published to the end
    recorder.close()


@pytest.mark.parametrize("damage", ["garbage", "newer", "foreign"])
def test_an_unreadable_or_incompatible_file_is_left_alone(tmp_path, damage, fast):
    path = tmp_path / history.DB_NAME
    if damage == "garbage":
        path.write_bytes(b"this is not an SQLite database" * 200)
    else:
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE notes (text)")
        if damage == "newer":
            conn.execute("PRAGMA application_id = %d" % history.APPLICATION_ID)
            conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
    original = path.read_bytes()
    recorder = history.HistoryRecorder(config(tmp_path))
    wait_for(lambda: recorder.status()["state"] == "incompatible")
    feed(recorder, bench.scenario(2))
    time.sleep(0.3)  # (no retry happens: a retry would be visible as a changed error time)
    status = recorder.status()
    assert status["lost"]["inactive"] == 11 and "left untouched" in status["reason"]
    recorder.close()
    assert path.read_bytes() == original


def test_shutdown_is_bounded_when_the_database_cannot_be_written(tmp_path, fast):
    recorder = history.HistoryRecorder(config(tmp_path, close_timeout_s=1.0))
    feed(recorder, bench.scenario(4, end=False))
    wait_for(lambda: recorder.status()["rows"]["decisions"] > 0)
    blocker = sqlite3.connect(str(tmp_path / history.DB_NAME), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        feed(recorder, bench.scenario(10, start_sequence=100, end=False),
             live.LiveStateHub(listeners=[recorder.observe]))
        started = time.monotonic()
        recorder.close()
        assert time.monotonic() - started < 2.5
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert rows(tmp_path, "SELECT end_reason FROM processors")[0]["end_reason"] is None  # no clean end recorded


def writer_steps(recorder, seconds):
    """How many times the writer's loop ran in this many seconds."""
    count = [0]
    step = recorder._step

    def counted():
        count[0] += 1
        step()
    recorder._step = counted
    time.sleep(seconds)
    del recorder._step
    return count[0]


@pytest.mark.parametrize("condition", ["idle_after_records", "never_fed", "unavailable", "incompatible", "paused"])
def test_a_writer_with_nothing_to_do_sleeps_and_stops_at_once(tmp_path, monkeypatch, condition):
    """Phase 7A.3: with nothing to write, the heartbeat deadline (or, with no database, the maintenance one) stayed
    in the past, so queue.get(timeout=0) spun a core until the next state: 100% of a Pi core for hours."""
    monkeypatch.setattr(history, "HEARTBEAT_S", 0.1)
    monkeypatch.setattr(history, "RETRY_S", (0.2, 0.2))
    directory, disk = tmp_path, shutil.disk_usage
    if condition == "unavailable":
        (tmp_path / "file").write_text("not a directory")
        directory = tmp_path / "file" / "history"
    elif condition == "incompatible":
        (tmp_path / history.DB_NAME).write_bytes(b"this is not an SQLite database" * 200)
    elif condition == "paused":
        disk = lambda path: SimpleNamespace(free=0)  # noqa: E731 (below min_free_mb: writing pauses)
    recorder = history.HistoryRecorder(config(directory), disk_usage=disk)
    if condition == "idle_after_records":
        feed(recorder, bench.scenario(2, end=False))
        wait_for(lambda: recorder.status()["states"]["absorbed"] == 10)
    state = {"unavailable": "unavailable", "incompatible": "incompatible", "paused": "paused"}.get(condition, "recording")
    wait_for(lambda: recorder.status()["state"] == state and not recorder._has_rows() and not recorder._dirty)
    time.sleep(0.3)  # past the heartbeat, with nothing left to write
    if condition != "unavailable":  # (a retry falls due every 0.2 s there)
        now = time.monotonic()
        assert recorder._wake_at(now) > now
    assert writer_steps(recorder, 0.6) <= 6  # about one wake-up a second, one per retry; spinning was > 10,000
    started = time.monotonic()
    recorder.close()
    assert time.monotonic() - started < 0.5 and not recorder._thread.is_alive()
    assert recorder.status()["state"] == ("incompatible" if condition == "incompatible" else "stopped")


def test_states_after_an_idle_spell_are_written_and_the_end_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HEARTBEAT_S", 0.1)
    recorder = history.HistoryRecorder(config(tmp_path, dataset=True))
    frames = list(bench.scenario(4, end=False))
    hub = feed(recorder, frames[:10])
    wait_for(lambda: recorder.status()["rows"]["samples"] == 10 and not recorder._dirty)
    time.sleep(0.3)  # idle, past the heartbeat
    assert writer_steps(recorder, 0.3) <= 3
    started = time.monotonic()
    feed(recorder, frames[10:], hub)
    wait_for(lambda: recorder.status()["rows"]["samples"] == 20, timeout=2.0)
    assert time.monotonic() - started < 0.5  # written within the flush period, not a heartbeat later
    recorder.close()
    status = recorder.status()
    assert status["state"] == "stopped" and not status["losses_observed"]
    processor, = rows(tmp_path, "SELECT end_reason, ended_utc FROM processors")
    assert processor["end_reason"] == "clean" and processor["ended_utc"]
    assert rows(tmp_path, "SELECT max(last_sequence) AS last FROM segments")[0]["last"] == 20
    with db(tmp_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


CRASH = """
import json, os, signal, sys, time
sys.path.insert(0, sys.argv[2])
import bench_history
from roadsense.web import history, live
recorder = history.HistoryRecorder(history.Config(enabled=True, directory=sys.argv[1], flush_s=0.05))
hub = live.LiveStateHub(listeners=[recorder.observe])
for frame in bench_history.scenario(30, "crash-run", end=False):
    hub.ingest_sensors(json.dumps(frame).encode())
while recorder.status()["states"]["absorbed"] < 150 or recorder._has_rows():
    time.sleep(0.01)
time.sleep(0.2)
print(recorder.processor_id, flush=True)
os.kill(os.getpid(), signal.SIGKILL)
"""


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="POSIX only")
def test_after_an_abrupt_termination_the_next_lifetime_records_the_unobserved_end(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    done = subprocess.run([sys.executable, "-c", CRASH, str(tmp_path), str(ROOT / "tools")], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == -signal.SIGKILL, done.stderr
    crashed = done.stdout.strip()
    before = rows(tmp_path, "SELECT count(*) AS n FROM decisions")[0]["n"]
    assert before > 0
    recorder = record(tmp_path, bench.scenario(10, "crash-run", start_sequence=400, end=False))
    with db(tmp_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    p = rows(tmp_path, "SELECT * FROM processors WHERE processor_id = ?", crashed)[0]
    assert p["end_reason"] == "not_observed" and p["ended_utc"] is None and p["last_commit_utc"]
    unseen, = rows(tmp_path, "SELECT * FROM discontinuities WHERE kind = 'processor_end_not_observed'")
    assert unseen["processor_id"] == crashed and unseen["basis"] == "inferred"
    start = rows(tmp_path, "SELECT detail FROM discontinuities WHERE kind = 'processor_start' AND processor_id = ?",
                 recorder.processor_id)[0]
    assert json.loads(start["detail"])["previous_end"] == "not_observed"
    resumed, = rows(tmp_path, "SELECT * FROM discontinuities WHERE kind = 'run_resumed'")
    assert resumed["to_sequence"] == 400 and resumed["from_sequence"] <= 150  # what was committed, not processed
    assert rows(tmp_path, "SELECT count(*) AS n FROM decisions WHERE segment_id LIKE ?", crashed + "%")[0]["n"] == before


# --- retention and size


def test_rows_older_than_the_retention_period_are_deleted_in_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DELETE_CHUNK", 7)
    old = time.time() - 40 * 86400
    clock = [old]
    recorder = history.HistoryRecorder(config(tmp_path, dataset=True), wall=lambda: clock[0])
    feed(recorder, bench.scenario(10))
    recorder.close()
    stored = rows(tmp_path, "SELECT (SELECT count(*) FROM samples) + (SELECT count(*) FROM decisions) + "
                            "(SELECT count(*) FROM hazard_updates) + (SELECT count(*) FROM discontinuities) AS n")[0]["n"]
    fresh = history.HistoryRecorder(config(tmp_path, retention_days=30))
    wait_for(lambda: fresh.status()["retention"]["deleted_by_age"] >= stored)
    fresh.close()
    assert fresh.status()["retention"]["deleted_by_age"] == stored
    assert [p["processor_id"] for p in rows(tmp_path, "SELECT processor_id FROM processors")] == [fresh.processor_id]
    assert rows(tmp_path, "SELECT count(*) AS n FROM segments")[0]["n"] == 0


def test_the_size_limit_deletes_samples_first(tmp_path):
    limit_mb = 0.5
    recorder = history.HistoryRecorder(config(tmp_path, dataset=True, max_mb=limit_mb, maintenance_s=0.05))
    frames, hub = list(bench.scenario(240)), None
    for start in range(0, len(frames), 50):  # a steady stream, slower than maintenance: as on the road
        hub = feed(recorder, frames[start:start + 50], hub)
        wait_for(lambda: recorder.status()["states"]["absorbed"] == min(len(frames), start + 50))
        time.sleep(0.12)
    assert recorder.status()["retention"]["deleted_by_size"] > 0
    recorder.close()
    with db(tmp_path) as conn:
        pages = conn.execute("PRAGMA page_count").fetchone()[0] - conn.execute("PRAGMA freelist_count").fetchone()[0]
        used = pages * conn.execute("PRAGMA page_size").fetchone()[0]
    assert used <= limit_mb * 1048576 * 1.15  # the limit, plus what arrived after the last maintenance pass
    assert rows(tmp_path, "SELECT min(sequence) AS s FROM samples")[0]["s"] > 1  # the oldest samples went
    assert rows(tmp_path, "SELECT min(sequence) AS s FROM decisions")[0]["s"] == 1  # decisions were kept
    assert rows(tmp_path, "SELECT count(*) AS n FROM samples")[0]["n"] > 150  # only the excess went


def test_a_reader_sees_a_consistent_snapshot_and_never_blocks_the_writer(tmp_path):
    recorder = history.HistoryRecorder(config(tmp_path, dataset=True))
    frames = list(bench.scenario(40, end=False))
    hub = feed(recorder, frames[:50])
    wait_for(lambda: recorder.status()["rows"]["samples"] == 50)
    reader = sqlite3.connect(str(tmp_path / history.DB_NAME), isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM samples").fetchone()[0] == 50
    feed(recorder, frames[50:], hub)
    wait_for(lambda: recorder.status()["rows"]["samples"] == len(frames))
    assert reader.execute("SELECT count(*) FROM samples").fetchone()[0] == 50  # its snapshot
    reader.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM samples").fetchone()[0] == len(frames)
    reader.close()
    recorder.close()
    assert recorder.status()["batches"]["failed"] == 0


# --- the dataset


@pytest.fixture
def recorded(tmp_path):
    """Three runs with samples, the last with a gap; the database directory."""
    directory = tmp_path / "history"
    recorder = history.HistoryRecorder(config(directory, dataset=True))
    hub = None
    for run in ("r1", "r2"):
        hub = feed(recorder, bench.scenario(30, run), hub)
    frames = list(bench.scenario(10, "r3"))
    feed(recorder, frames[:20] + frames[25:], hub)
    recorder.close()
    return directory


def test_the_export_has_provenance_definitions_and_no_geography(recorded, tmp_path):
    with history_export.connect(recorded) as conn:
        manifest = history_export.export(conn, tmp_path / "out")
    out = tmp_path / "out"
    assert manifest["schema"] == "roadsense.dataset.v1" and manifest["feature_version"] == history.FEATURES
    assert manifest["label_provenance"]["kind"] == "heuristic" and manifest["label_provenance"]["ground_truth"] is False
    assert manifest["data"] == {"simulated": True, "producer": "webots", "coordinate_frame": "webots_world",
                                "geo": None, "note": manifest["data"]["note"]}
    assert set(manifest["files"]) == {"processors.jsonl", "segments.jsonl", "discontinuities.jsonl", "decisions.jsonl",
                                      "hazard_updates.jsonl", "hazards.jsonl", "samples.jsonl"}
    for name, info in manifest["files"].items():
        assert len((out / name).read_text().splitlines()) == info["rows"]
    hazards = [json.loads(line) for line in (out / "hazards.jsonl").read_text().splitlines()]
    assert hazards and all(h["position"]["geo"] is None and h["position"]["frame"] == "webots_world"
                           and h["provenance"]["simulated"] is True for h in hazards)
    assert len({h["historical_id"] for h in hazards}) == len(hazards)
    samples = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines()]
    names = [n for n, _, _ in history.FEATURE_COLUMNS]
    assert all(set(s["features"]) == set(names) for s in samples)  # every feature in every row (the CSV keeps the order)
    first = samples[0]["features"]
    assert first["ego_acceleration_mps2"] is None and first["sequence_gap_before"] is None  # missing, not zero
    absent = next(s["features"] for s in samples if s["features"]["lead_present"] == 0)
    assert all(absent["lead_" + k] is None for k in ("lon_m", "v_lon_mps", "age_s"))
    assert any(s["features"]["sequence_gap_before"] == 5 for s in samples if s["run_id"] == "r3")
    assert {s["labels"]["recommended_action"] for s in samples} >= {"BRAKE", "EMERGENCY_BRAKE", "MAINTAIN"}
    assert "near-identical" in " ".join(manifest["warnings"])


def test_the_export_is_deterministic_and_never_overwrites(recorded, tmp_path):
    with history_export.connect(recorded) as conn:
        history_export.export(conn, tmp_path / "a")
        history_export.export(conn, tmp_path / "b", fmt="csv")
        history_export.export(conn, tmp_path / "c", fmt="csv")
        with pytest.raises(FileExistsError):
            history_export.export(conn, tmp_path / "a")
    for name in os.listdir(tmp_path / "c"):
        assert (tmp_path / "b" / name).read_bytes() == (tmp_path / "c" / name).read_bytes()
    header, *lines = (tmp_path / "b" / "samples.csv").read_text().splitlines()
    assert header.split(",")[:3] == ["split", "group", "run_id"] and "label_recommended_action" in header
    assert len(lines) == json.loads((tmp_path / "a" / "manifest.json").read_text())["files"]["samples.jsonl"]["rows"]
    assert ",," in lines[0]  # nulls are empty cells


def test_splits_are_by_run_or_by_group_and_never_split_a_run(recorded, tmp_path):
    with history_export.connect(recorded) as conn:
        by_run = history_export.export(conn, tmp_path / "runs", seed="s1")
        grouped = history_export.export(conn, tmp_path / "groups", seed="s1",
                                        group_map={"r1": "family-a", "r2": "family-a", "r3": "family-b"})
    for out, manifest in ((tmp_path / "runs", by_run), (tmp_path / "groups", grouped)):
        splits = {}
        for line in (out / "samples.jsonl").read_text().splitlines():
            s = json.loads(line)
            splits.setdefault(s["group"], set()).add(s["split"])
        assert all(len(v) == 1 for v in splits.values())
        assert {g: v.pop() for g, v in splits.items()} == manifest["split"]["assignment"]
    assert grouped["split"]["assignment"].keys() == {"family-a", "family-b"}
    # a group's split depends on the seed and the group only, not on the other groups
    assert all(history_export.assign(g, "s1", (0.7, 0.15, 0.15)) == s for g, s in by_run["split"]["assignment"].items())
    many = [history_export.assign("run-%d" % i, "s1", (0.7, 0.15, 0.15)) for i in range(2000)]
    assert 0.65 < many.count("train") / 2000 < 0.75 and many.count("test") > 200


def test_the_command_line_summarises_and_exports(recorded, tmp_path, capsys):
    assert history_export.main(["--dir", str(recorded), "summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"]["samples"] > 0 and len(summary["processors"]) == 1
    assert history_export.main(["--dir", str(recorded), "export", str(tmp_path / "x"), "--split", "50,50,0"]) == 0
    assert json.loads((tmp_path / "x" / "manifest.json").read_text())["split"]["fractions"]["test"] == 0
    assert history_export.main(["--dir", str(tmp_path / "nothing"), "summary"]) == 1


# --- the safety output is the same with the store on and off


def test_the_server_publishes_identical_decisions_with_history_on_and_off(tmp_path, monkeypatch):
    pytest.importorskip("httpx2")
    from fastapi.testclient import TestClient

    from roadsense.web import server
    import test_web_live

    lane_change = test_web_live.sensor_frame(1, 0.2, "lane-run")
    lane_change["objects"] = [[[48, 0, 0], "pothole severity=HIGH length=1.2 width=0.9 depth=0.08"]]
    frames = list(bench.scenario(60, "replay")) + [lane_change]

    def replay(env):
        monkeypatch.delenv("ROADSENSE_AI_EXPLAIN", raising=False)
        for key in [k for k in os.environ if k.startswith("ROADSENSE_HISTORY_")]:
            monkeypatch.delenv(key)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        states = []
        with TestClient(server.app) as client:
            for frame in frames:
                assert client.post("/api/sensor-frame", json=frame).status_code == 200
                states.append(bench.comparable(client.get("/api/live-state").text))
            status = client.get("/api/system").json()["history"]
        return states, status

    off, off_status = replay({})
    on, on_status = replay({"ROADSENSE_HISTORY_ENABLE": "1", "ROADSENSE_HISTORY_DIR": str(tmp_path),
                            "ROADSENSE_HISTORY_DATASET": "1"})
    assert on == off
    actions = {s["unified_safety"]["recommended_action"] for s in off}
    assert {"BRAKE", "EMERGENCY_BRAKE", "CONSIDER_LANE_CHANGE", "MONITOR", "MAINTAIN"} <= actions
    assert off_status["enabled"] is False and off_status["state"] == "disabled"
    assert on_status["enabled"] is True and on_status["state"] == "recording" and not on_status["degraded"]
    assert not on_status["losses_observed"]
    # one sample per frame, except the end marker: it repeats the last frame's sensor time, it is no new observation
    assert rows(tmp_path, "SELECT count(*) AS n FROM samples")[0]["n"] == len(frames) - 1
    assert rows(tmp_path, "SELECT end_reason FROM processors")[0]["end_reason"] == "clean"
