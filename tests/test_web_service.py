"""Web telemetry manager: sources run only on request, and recording stays consistent while
packets and requests race.

Interleavings are forced with a Gate: the first call of a repository method parks its thread
inside the call (inside the manager's locks) until released, while another thread races it.
"""
import asyncio
import os
import sqlite3
import threading
import time

import pytest

from roadsense.database import Database
from roadsense.repositories import SessionRepository, TelemetryRepository
from roadsense.web import service
from roadsense.web.service import WebTelemetryManager

T = "T,100,1200,300,25.0,NORMAL"
E = "E,100,-6800,18000,42.0,POTHOLE"
# Lets the racing thread reach the lock before the gate opens. Only the old bugs need this
# window; the fixed manager is correct for every interleaving, so the pause can't flake.
RACE_WINDOW_S = 0.05


@pytest.fixture
def mgr():
    m = WebTelemetryManager(db=Database(":memory:", check_same_thread=False))
    yield m
    m.stop_simulator()
    m.disconnect_serial()


class Gate:
    """Park the first caller of obj.<name> inside the call until release()."""

    def __init__(self, monkeypatch, obj, name):
        self.entered, self._open = threading.Event(), threading.Event()
        real = getattr(obj, name)

        def gated(*args, **kwargs):
            if not self.entered.is_set():
                self.entered.set()
                self._open.wait(5)
            return real(*args, **kwargs)

        monkeypatch.setattr(obj, name, gated)

    def release(self):
        self._open.set()


def spawn(fn, *args):
    """Run fn on a daemon thread (a deadlocked one can't hang pytest); collect its exceptions."""
    errors = []

    def run():
        try:
            fn(*args)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def finish(*threads):
    for thread in threads:
        thread.join(5)
    assert not any(t.is_alive() for t in threads), "deadlock: a thread is still blocked after 5 s"


def feed(mgr, line, times=1):
    for _ in range(times):
        mgr._handle_incoming_raw_line(line)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


def total_telemetry_rows(mgr):
    return mgr.db.conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]


def worker_threads():
    names = ("RoadSenseSimWorker", "RoadSenseSerialWorker")
    return [t.name for t in threading.enumerate() if t.name in names and t.is_alive()]


def test_starts_with_no_source_and_no_telemetry(mgr):
    status = mgr.get_status()
    assert (status["source"], status["is_simulator"], status["connection_status"]) == (
        "NONE", False, "Disconnected")
    assert status["total_packets_received"] == 0
    assert worker_threads() == []


def test_simulator_runs_only_between_explicit_start_and_stop(mgr, monkeypatch):
    monkeypatch.setattr(service, "SIM_PERIOD_S", 0.005)
    mgr.start_simulator("normal")
    status = mgr.get_status()
    assert (status["source"], status["is_simulator"], status["active_port"]) == ("SIMULATOR", True, None)
    wait_for(lambda: mgr.total_packets_received >= 3)

    mgr.start_simulator("bumpy")  # restart: the previous run must end, not keep streaming
    assert worker_threads() == ["RoadSenseSimWorker"]

    mgr.stop_simulator()
    status = mgr.get_status()
    assert (status["source"], status["connection_status"]) == ("NONE", "Disconnected")
    assert worker_threads() == []  # joined, so no packet can follow the stop


def test_failed_serial_connect_reports_an_error_and_no_source(mgr):
    assert mgr.connect_serial("/dev/roadsense-test-no-such-port")["status"] == "error"
    status = mgr.get_status()
    assert (status["source"], status["connection_status"], status["active_port"]) == ("NONE", "Error", None)
    assert worker_threads() == []


@pytest.fixture
def serial_device():
    """A pseudo-terminal standing in for the Arduino's USB serial port: (port path, writer fd)."""
    if not hasattr(os, "openpty"):
        pytest.skip("needs a POSIX pseudo-terminal")
    controller, device = os.openpty()
    yield os.ttyname(device), controller
    os.close(controller)
    os.close(device)


def test_serial_source_streams_until_disconnected(mgr, serial_device):
    port, arduino = serial_device
    assert mgr.connect_serial(port)["status"] == "ok"
    status = mgr.get_status()
    assert (status["source"], status["is_simulator"], status["active_port"]) == ("ARDUINO", False, port)
    os.write(arduino, b"HELLO,ROADSENSE,1\nT,10,100,5,12.0,NORMAL\n")
    wait_for(lambda: mgr.total_packets_received == 1)

    mgr.disconnect_serial()
    status = mgr.get_status()
    assert (status["source"], status["connection_status"], status["active_port"]) == (
        "NONE", "Disconnected", None)
    assert worker_threads() == []


def test_starting_the_simulator_replaces_the_serial_source(mgr, serial_device):
    port, _ = serial_device
    mgr.connect_serial(port)
    mgr.start_simulator()
    status = mgr.get_status()
    assert (status["source"], status["active_port"]) == ("SIMULATOR", None)  # never "Arduino"
    assert worker_threads() == ["RoadSenseSimWorker"]


def test_only_packets_inside_a_recording_are_persisted(mgr):
    assert mgr.stop_recording()["status"] == "noop"  # nothing to stop or flush: no crash
    feed(mgr, T)  # before any recording
    first = mgr.start_recording("first")["session_id"]
    feed(mgr, T, 2)
    feed(mgr, E)
    stopped = mgr.stop_recording()
    feed(mgr, T, 4)  # between recordings
    second = mgr.start_recording("second")["session_id"]
    feed(mgr, T)
    mgr.stop_recording()

    assert (stopped["telemetry_count"], stopped["event_count"]) == (2, 1)
    assert (mgr.telemetry.count(first), mgr.events.count(first)) == (2, 1)
    assert (mgr.telemetry.count(second), mgr.events.count(second)) == (1, 0)
    assert total_telemetry_rows(mgr) == 3


def test_telemetry_arriving_while_recording_starts_is_saved_as_counted(mgr, monkeypatch):
    gate = Gate(monkeypatch, mgr.sessions, "create")
    started = {}
    starter, start_errors = spawn(lambda: started.update(mgr.start_recording("a")))
    assert gate.entered.wait(5)
    feeder, feed_errors = spawn(feed, mgr, T, 5)
    time.sleep(RACE_WINDOW_S)
    gate.release()
    finish(starter, feeder)
    assert start_errors == feed_errors == []

    stopped = mgr.stop_recording()
    assert mgr.telemetry.count(started["session_id"]) == stopped["telemetry_count"]


def test_event_racing_a_recording_stop_does_not_deadlock(mgr, monkeypatch):
    sid = mgr.start_recording("a")["session_id"]
    gate = Gate(monkeypatch, mgr.sessions, "list")
    reader, read_errors = spawn(mgr.list_sessions)  # parks inside the database lock
    assert gate.entered.wait(5)
    stopped = {}
    stopper, stop_errors = spawn(lambda: stopped.update(mgr.stop_recording()))
    time.sleep(RACE_WINDOW_S)  # stop queues for the database first...
    feeder, feed_errors = spawn(feed, mgr, E)
    time.sleep(RACE_WINDOW_S)  # ...then the event (old code: holding the buffer lock)
    gate.release()
    finish(reader, stopper, feeder)
    assert read_errors == stop_errors == feed_errors == []

    persisted = (mgr.telemetry.count(sid), mgr.events.count(sid))
    assert persisted == (stopped["telemetry_count"], stopped["event_count"])


def test_telemetry_arriving_while_recording_stops_neither_crashes_nor_leaks(mgr, monkeypatch):
    sid = mgr.start_recording("a")["session_id"]
    feed(mgr, T, 3)
    gate = Gate(monkeypatch, mgr.telemetry, "add_many")
    stopped = {}
    stopper, stop_errors = spawn(lambda: stopped.update(mgr.stop_recording()))
    assert gate.entered.wait(5)
    feeder, feed_errors = spawn(feed, mgr, T, 25)  # crosses the 20-row batch: the feeder flushes too
    time.sleep(RACE_WINDOW_S)
    gate.release()
    finish(stopper, feeder)
    assert stop_errors == feed_errors == []

    assert mgr.telemetry.count(sid) == stopped["telemetry_count"]
    assert total_telemetry_rows(mgr) == stopped["telemetry_count"]


def test_rows_buffered_for_one_recording_never_land_in_the_next(mgr, monkeypatch):
    first = mgr.start_recording("first")["session_id"]
    feed(mgr, T, 3)
    gate = Gate(monkeypatch, mgr.telemetry, "add_many")
    result = {}

    def stop_then_start():
        result["stopped"] = mgr.stop_recording()
        result["second"] = mgr.start_recording("second")["session_id"]

    switcher, switch_errors = spawn(stop_then_start)
    assert gate.entered.wait(5)
    feeder, feed_errors = spawn(feed, mgr, T, 25)
    time.sleep(RACE_WINDOW_S)
    gate.release()
    finish(switcher, feeder)
    assert switch_errors == feed_errors == []

    stopped_second = mgr.stop_recording()
    assert mgr.telemetry.count(first) == result["stopped"]["telemetry_count"]
    assert mgr.telemetry.count(result["second"]) == stopped_second["telemetry_count"]


def test_rapid_start_stop_under_live_telemetry_keeps_every_session_consistent(mgr):
    done = threading.Event()
    results = []

    def live_source():
        while not done.is_set():
            feed(mgr, T)
            feed(mgr, E)

    def toggle_recording():
        for i in range(30):
            sid = mgr.start_recording(f"s{i}")["session_id"]
            results.append((sid, mgr.stop_recording()))

    source, source_errors = spawn(live_source)
    toggler, toggle_errors = spawn(toggle_recording)
    toggler.join(10)
    done.set()
    finish(toggler, source)
    assert source_errors == toggle_errors == []

    assert len(results) == 30
    for sid, stopped in results:
        persisted = (mgr.telemetry.count(sid), mgr.events.count(sid))
        assert persisted == (stopped["telemetry_count"], stopped["event_count"])


def test_failed_recording_write_does_not_stop_live_telemetry(mgr, monkeypatch):
    mgr.start_recording("a")

    def disk_error(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(mgr.events, "add", disk_error)
    feed(mgr, E)  # the event write fails: logged, never raised into the source thread
    feed(mgr, T)
    assert mgr.total_packets_received == 2
    assert mgr.latest_packet["kind"] == "T"


def test_simulator_keeps_streaming_after_recording_stops(mgr):
    mgr.start_simulator()
    sid = mgr.start_recording("drive")["session_id"]
    wait_for(lambda: mgr.get_status()["session_telemetry_count"] >= 2)
    stopped = mgr.stop_recording()
    seen = mgr.total_packets_received
    wait_for(lambda: mgr.total_packets_received >= seen + 3)  # source thread still alive
    assert mgr.telemetry.count(sid) == stopped["telemetry_count"]


def test_shutdown_saves_buffered_telemetry_and_ends_the_session(tmp_path):
    path = tmp_path / "roadsense.db"
    mgr = WebTelemetryManager(db=Database(path, check_same_thread=False))
    sid = mgr.start_recording("drive")["session_id"]
    feed(mgr, T, 3)  # below the batch size: still only in memory
    mgr.shutdown()

    with Database(path) as db:
        assert TelemetryRepository(db.conn).count(sid) == 3
        assert SessionRepository(db.conn).get(sid).ended_at is not None


@pytest.fixture
def loop():
    """An event loop collecting the errors it would otherwise only log (e.g. QueueFull)."""
    loop = asyncio.new_event_loop()
    loop.errors = []
    loop.set_exception_handler(lambda _, context: loop.errors.append(context))
    yield loop
    if not loop.is_running():  # a failed test may leave it running on its thread
        loop.close()


def drain(queue):
    return [queue.get_nowait() for _ in range(queue.qsize())]


def test_slow_websocket_client_gets_newest_telemetry_and_every_status(mgr, loop):
    mgr.set_event_loop(loop)
    slow, fast = asyncio.Queue(maxsize=5), asyncio.Queue(maxsize=5)
    mgr.register_subscriber(slow)
    mgr.register_subscriber(fast)
    fast_seen = []

    def pump():  # run the queued deliveries; the fast client reads, the slow one never does
        loop.run_until_complete(asyncio.sleep(0))
        fast_seen.extend(drain(fast))
        assert slow.qsize() <= 5

    sid = mgr.start_recording("drive")["session_id"]
    for i in range(40):
        feed(mgr, f"T,{i},1200,300,25.0,NORMAL")
        if i == 20:
            feed(mgr, "HELLO,ROADSENSE,1")
        pump()
    mgr.stop_recording()
    pump()

    assert loop.errors == []  # no QueueFull escaped into the loop
    assert [m["type"] for m in fast_seen] == ["status"] + ["packet"] * 21 + ["hello"] + ["packet"] * 19 + ["status"]
    assert mgr.telemetry.count(sid) == 40  # recording kept every packet

    backlog = drain(slow)
    assert [m["type"] for m in backlog if m["type"] != "packet"] == ["status", "hello", "status"]
    assert backlog[0]["status"]["is_recording"] and not backlog[-1]["status"]["is_recording"]
    assert [m for m in backlog if m["type"] == "packet"][-1]["packet"]["arduino_time_ms"] == 39
    assert mgr.get_status()["ws_dropped_messages"] > 0

    feed(mgr, "T,99,1200,300,25.0,NORMAL")  # drained: live telemetry reaches it again
    pump()
    assert drain(slow)[0]["packet"]["arduino_time_ms"] == 99

    for i in range(100, 106):  # overflow a queue of packets only: all stale ones go at once
        feed(mgr, f"T,{i},1200,300,25.0,NORMAL")
    pump()
    assert [m["packet"]["arduino_time_ms"] for m in drain(slow)] == [105]

    for _ in range(6):  # no packets to drop: the oldest status snapshot makes room
        mgr._broadcast_status()
    pump()
    assert [m["type"] for m in drain(slow)] == ["status"] * 5
    assert loop.errors == []


def test_slow_websocket_client_does_not_disturb_the_source_or_recording(mgr, loop, monkeypatch):
    monkeypatch.setattr(service, "SIM_PERIOD_S", 0.001)
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    mgr.set_event_loop(loop)
    slow = asyncio.Queue(maxsize=3)
    mgr.register_subscriber(slow)  # never read, like a stalled browser tab

    sid = mgr.start_recording("drive")["session_id"]
    mgr.start_simulator()
    wait_for(lambda: mgr.total_packets_received >= 100)
    assert worker_threads() == ["RoadSenseSimWorker"]
    assert mgr.get_status()["source"] == "SIMULATOR"

    seen = mgr.total_packets_received
    wait_for(lambda: mgr.total_packets_received >= seen + 100)  # still streaming
    stopped = mgr.stop_recording()
    assert stopped["telemetry_count"] + stopped["event_count"] >= 200  # T rows + E events
    assert mgr.telemetry.count(sid) == stopped["telemetry_count"]

    mgr.shutdown()
    loop.call_soon_threadsafe(loop.stop)
    finish(loop_thread)
    assert worker_threads() == []
    assert loop.errors == []
    assert slow.qsize() <= 3
