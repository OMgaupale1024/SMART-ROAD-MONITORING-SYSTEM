"""Web server: the live RoadSense state stream (POST and GET /api/live-state, WebSocket /ws/live)."""
import json
import math
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # web extra not installed
pytest.importorskip("httpx2")  # dev extra not installed (FastAPI's TestClient needs it)

from fastapi.testclient import TestClient

from roadsense.web import live, server

_CONTROLLER = str(Path(__file__).resolve().parents[1] / "simulation/webots/controllers/roadsense_ego")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADSENSE_DATA_DIR", str(tmp_path))  # the app opens its database at startup
    with TestClient(server.app) as c:
        yield c


def snapshot(sequence=1, run_id="run-a"):
    """A small but complete roadsense.live.v1 snapshot."""
    return {
        "schema": "roadsense.live.v1",
        "timestamp": round(0.2 * sequence, 3),
        "ego": {"vehicle_id": "EGO_ROADSENSE", "timestamp": round(0.2 * sequence, 3), "speed_mps": 22.2,
                "acceleration_mps2": None},
        "road": {"lane_width_m": 3.75, "heading_deg": 0.0,
                 "lanes": [{"relation": "ego_lane", "center_lateral_m": 0.08, "driving": True}]},
        "tracks": [{"track_id": "TRACK_001", "relative_position": {"longitudinal_m": 30.0, "lateral_m": 0.1},
                    "collision": {"ttc_s": None, "risk": "SAFE"}}],
        "hazards": [{"hazard_id": "PH_001", "relative": {"longitudinal_m": 60.0, "direction": "ahead"}}],
        "unified_safety": {"overall_risk": "SAFE", "recommended_action": "MAINTAIN"},
        "simulation": {"source": "webots", "active": True, "run_id": run_id, "sequence": sequence,
                       "generated_at_unix_s": 1791500000.0},
    }


def post(client, state):
    body = state if isinstance(state, bytes) else json.dumps(state).encode()
    return client.post("/api/live-state", content=body, headers={"Content-Type": "application/json"})


def recv(ws, timeout=5.0):
    """ws.receive_json() with a timeout, so a missing message fails the test instead of hanging it."""
    box = []
    reader = threading.Thread(target=lambda: box.append(ws.receive_json()), daemon=True)
    reader.start()
    reader.join(timeout)
    assert box, "no WebSocket message within %s s" % timeout
    return box[0]


def assert_silent(ws, wait=0.3):
    """No message within `wait`. Returns the reader still waiting and the box it fills with the next message."""
    box = []
    reader = threading.Thread(target=lambda: box.append(ws.receive_json()), daemon=True)
    reader.start()
    reader.join(wait)
    assert not box, "unexpected WebSocket message: %r" % (box,)
    return reader, box


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


# --- ingest and latest state


def test_no_live_state_until_the_simulation_sends_one(client):
    response = client.get("/api/live-state")
    assert (response.status_code, response.json()) == (404, {"detail": "No live state received yet"})


def test_posted_snapshot_becomes_the_latest_state(client):
    response = post(client, snapshot(1))
    assert (response.status_code, response.json()) == (200, {"accepted": True, "run_id": "run-a", "sequence": 1})
    assert client.get("/api/live-state").json() == snapshot(1)
    assert post(client, snapshot(2)).status_code == 200
    assert client.get("/api/live-state").json() == snapshot(2)


def _bad(**changes):
    state = snapshot(1)
    for path, value in changes.items():
        *parents, key = path.split("__")
        target = state
        for parent in parents:
            target = target[parent]
        if value is _DROP:
            del target[key]
        else:
            target[key] = value
    return json.dumps(state).encode()


_DROP = object()


@pytest.mark.parametrize("body", [
    b"not json",
    b"[]",
    json.dumps(snapshot(1)).encode().replace(b'"speed_mps": 22.2', b'"speed_mps": NaN'),
    json.dumps(snapshot(1)).encode().replace(b'"speed_mps": 22.2', b'"speed_mps": Infinity'),
    json.dumps(snapshot(1)).encode().replace(b'"speed_mps": 22.2', b'"speed_mps": 1e999'),  # overflows to inf
    _bad(schema="roadsense.live.v2"),
    _bad(timestamp=-1.0),
    _bad(timestamp="42"),
    _bad(ego=_DROP),
    _bad(tracks={"TRACK_001": {}}),
    _bad(tracks=[{"relative_position": {}}]),
    _bad(hazards=[{"hazard_id": 7}]),
    _bad(unified_safety="SAFE"),
    _bad(simulation__run_id=""),
    _bad(simulation__sequence=0),
    _bad(simulation__sequence=True),
    _bad(simulation__active="yes"),
])
def test_malformed_snapshot_is_rejected_and_changes_nothing(client, body):
    assert post(client, snapshot(1)).status_code == 200
    response = post(client, body)
    assert response.status_code == 400 and response.json()["detail"]
    assert client.get("/api/live-state").json() == snapshot(1)


def test_oversized_snapshot_is_rejected(client, monkeypatch):
    monkeypatch.setattr(live, "MAX_BYTES", 1000)
    assert post(client, snapshot(1)).status_code == 200
    big = snapshot(2)
    big["ego"]["padding"] = "x" * 2000
    assert post(client, big).status_code == 413
    assert client.get("/api/live-state").json()["simulation"]["sequence"] == 1


def test_older_snapshot_of_the_same_run_is_refused(client):
    assert post(client, snapshot(5)).status_code == 200
    for stale in (snapshot(5), snapshot(4)):  # a repeat, then an older one
        response = post(client, stale)
        assert response.status_code == 409 and "sequence" in response.json()["detail"]
    assert client.get("/api/live-state").json() == snapshot(5)


def test_new_run_replaces_the_previous_runs_state(client):
    assert post(client, snapshot(500, run_id="run-a")).status_code == 200
    assert post(client, snapshot(1, run_id="run-b")).status_code == 200  # the simulation restarted
    assert client.get("/api/live-state").json() == snapshot(1, run_id="run-b")


# --- WebSocket stream


def test_new_client_gets_the_latest_state_at_once_then_each_update(client):
    post(client, snapshot(1))
    with client.websocket_connect("/ws/live") as ws:
        assert recv(ws) == snapshot(1)
        post(client, snapshot(2))
        assert recv(ws) == snapshot(2)
        post(client, snapshot(3))
        assert recv(ws) == snapshot(3)


def test_client_connected_before_any_state_waits_for_the_first(client):
    with client.websocket_connect("/ws/live") as ws:
        reader, box = assert_silent(ws)
        post(client, snapshot(1))
        reader.join(5)
        assert box == [snapshot(1)]
        post(client, snapshot(2))
        assert recv(ws) == snapshot(2)


def test_every_client_gets_every_update_and_a_leaving_client_affects_no_other(client):
    with client.websocket_connect("/ws/live") as first, client.websocket_connect("/ws/live") as second:
        post(client, snapshot(1))
        assert recv(first) == snapshot(1) and recv(second) == snapshot(1)
        with client.websocket_connect("/ws/live") as third:
            assert recv(third) == snapshot(1)
        wait_for(lambda: server.live_hub.client_count == 2)
        post(client, snapshot(2))
        assert recv(first) == snapshot(2) and recv(second) == snapshot(2)
    wait_for(lambda: server.live_hub.client_count == 0)


def test_malformed_input_reaches_no_client(client):
    with client.websocket_connect("/ws/live") as ws:
        post(client, snapshot(1))
        assert recv(ws) == snapshot(1)
        assert post(client, b'{"schema": "roadsense.live.v1"}').status_code == 400
        assert post(client, snapshot(1)).status_code == 409
        post(client, snapshot(2))
        assert recv(ws) == snapshot(2)  # the rejected posts sent nothing


def test_slow_client_keeps_only_the_newest_state():
    hub = live.LiveStateHub()
    queue = hub.subscribe()
    for k in (1, 2, 3):  # three snapshots arrive before the client reads any
        hub.ingest(json.dumps(snapshot(k)).encode())
    assert queue.qsize() == 1 and json.loads(queue.get_nowait()) == snapshot(3)
    assert hub.dropped == 2
    hub.unsubscribe(queue)
    assert hub.client_count == 0


# --- the simulation's own snapshots, end to end


def _controller_modules():
    sys.path.insert(0, _CONTROLLER)
    try:
        import live_publisher
        import live_state
    finally:
        sys.path.remove(_CONTROLLER)
    return live_state, live_publisher


def test_backend_and_simulation_agree_on_the_schema():
    live_state, _ = _controller_modules()
    assert live.SCHEMA == live_state.SCHEMA == "roadsense.live.v1"


def test_simulation_publisher_feeds_websocket_clients_through_a_real_server(tmp_path, monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    from websockets.sync.client import connect

    live_state, live_publisher = _controller_modules()
    monkeypatch.setenv("ROADSENSE_DATA_DIR", str(tmp_path))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    real_server = uvicorn.Server(uvicorn.Config(server.app, log_level="warning"))
    thread = threading.Thread(target=real_server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: real_server.started)
    publisher = live_publisher.LivePublisher("http://127.0.0.1:%d/api/live-state" % port, 1.0, log=lambda line: None)
    try:
        with connect("ws://127.0.0.1:%d/ws/live" % port) as first, connect("ws://127.0.0.1:%d/ws/live" % port) as second:
            for sequence in (1, 2, 3):
                state = snapshot(sequence)
                state["ego"]["acceleration_mps2"] = math.nan  # unavailable: null on the wire
                publisher.publish(live_state.encode(state))
                for ws in (first, second):
                    received = json.loads(ws.recv(timeout=5))
                    assert received["simulation"]["sequence"] == sequence
                    assert received["ego"]["acceleration_mps2"] is None
    finally:
        publisher.close()
        real_server.should_exit = True
        thread.join(5)
