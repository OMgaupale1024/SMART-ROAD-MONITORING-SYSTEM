"""Web server: the live RoadSense state (POST /api/sensor-frame and /api/live-state, GET /api/live-state, WebSocket
/ws/live), and what must not disturb it: AI explanations, diagnostics, a paused or restarted simulation."""
import json
import math
import socket
import threading
import time

import pytest

pytest.importorskip("fastapi")  # web extra not installed
pytest.importorskip("httpx2")  # dev extra not installed (FastAPI's TestClient needs it)

from fastapi.testclient import TestClient

import fake_ollama
from roadsense.autonomy import live_publisher, live_state, pipeline
from roadsense.web import live, server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ROADSENSE_AI_EXPLAIN", raising=False)  # as shipped: no model
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


def test_precomputed_snapshot_cannot_claim_server_edge_processing(client):
    frame = snapshot()
    frame["simulation"]["processing"] = "edge"
    assert post(client, frame).status_code == 200
    assert "processing" not in client.get("/api/live-state").json()["simulation"]
    edge = client.get("/api/system").json()["edge"]
    assert edge["mode"] == "precomputed" and edge["latency_ms"] is None


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


def test_backend_and_simulation_agree_on_the_schema():
    assert live.SCHEMA == live_state.SCHEMA == "roadsense.live.v1"


def sensor_frame(sequence=1, t=0.2, run_id="edge-run"):
    return {
        "schema": pipeline.SENSOR_SCHEMA,
        "timestamp": t,
        "ego": {"vehicle_id": "EGO_ROADSENSE", "position": [-100 - 2 * sequence, 6.875, 0.3],
                "velocity": [-10, 0], "speed_mps": 10, "yaw_rad": math.pi, "steering_rad": 0,
                "lane": 1, "maneuver": "lane_keep"},
        "radars": {
            "radar front": {"targets": [], "limits": [1, 150, 2.6]},
            "radar rear": {"targets": [], "limits": [1, 100, 2.6]},
        },
        "objects": [],
        "sides": {"left": [10, 10], "right": [10, 10]},
        "simulation": {"run_id": run_id, "sequence": sequence, "active": True},
    }


@pytest.mark.parametrize("blocked, expected_lane", [
    ([], "left_lane"), (["left"], "right_lane"), (["left", "right"], None),
])
def test_edge_computes_and_streams_the_next_move(client, blocked, expected_lane):
    frame = sensor_frame()
    frame["objects"] = [[[48, 0, 0], "pothole severity=HIGH length=1.2 width=0.9 depth=0.08"]]
    for side in blocked:
        frame["sides"][side][0] = 2
    with client.websocket_connect("/ws/live") as ws:
        response = client.post("/api/sensor-frame", json=frame)
        assert response.status_code == 200, response.text
        state = ws.receive_json()
    assert client.get("/api/live-state").json() == state
    assert state["simulation"]["processing"] == "edge"
    assert state["hazards"][0]["relative"]["lane_relation"] == "ego_lane"
    decision = state["unified_safety"]
    assert decision["recommended_lane"] == expected_lane
    assert (decision["recommended_action"] == "CONSIDER_LANE_CHANGE") == (expected_lane is not None)
    assert decision["recommended_speed_kmh"] <= 36


def test_edge_tracks_raw_radar_returns_and_recommends_braking(client):
    for sequence in (1, 2, 3):
        frame = sensor_frame(sequence, sequence * 0.2)
        frame["radars"]["radar front"]["targets"] = [[14 - 2 * sequence, 0, None]]
        response = client.post("/api/sensor-frame", json=frame)
        assert response.status_code == 200, response.text
    state = client.get("/api/live-state").json()
    track, = state["tracks"]
    assert len(track["prediction"]["trajectory"]) > 1
    assert track["relative_speed_mps"] is None
    assert track["collision"]["ttc_s"] < 2
    decision = state["unified_safety"]
    assert decision["primary_threat"]["id"] == track["track_id"]
    assert decision["recommended_action"] == "BRAKE"
    assert decision["recommended_speed_kmh"] < 36 and decision["recommended_lane"] is None


def test_edge_rejects_malformed_and_stale_frames_and_resets_on_new_run(client):
    frame = sensor_frame()
    frame["objects"] = [[[48, 0, 0], "pothole severity=HIGH length=NaN width=0.9 depth=0.08"]]
    assert client.post("/api/sensor-frame", json=frame).status_code == 200
    state = client.get("/api/live-state").json()
    assert state["hazards"][0]["dimensions"] is None
    assert client.post("/api/sensor-frame", json=frame).status_code == 409
    stale = sensor_frame(2, t=0.1)
    assert client.post("/api/sensor-frame", json=stale).status_code == 409
    malformed = sensor_frame(2, t=0.4)
    malformed["ego"]["speed_mps"] = None
    assert client.post("/api/sensor-frame", json=malformed).status_code == 400
    assert client.get("/api/live-state").json() == state
    final = live_state.finished(frame)
    assert client.post("/api/sensor-frame", json=final).status_code == 200
    latest = client.get("/api/live-state").json()
    assert latest["hazards"] == state["hazards"] and latest["simulation"]["active"] is False
    assert client.post("/api/sensor-frame", json=sensor_frame(3, t=0.4)).status_code == 409
    assert client.post("/api/sensor-frame", json=sensor_frame(run_id="new-run")).status_code == 200
    assert client.get("/api/live-state").json()["hazards"] == []


def test_simulation_publisher_feeds_websocket_clients_through_a_real_server():
    uvicorn = pytest.importorskip("uvicorn")
    from websockets.sync.client import connect

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


# --- the edge path keeps working whatever the simulation, the dashboard or the model do


def pothole_frame(sequence=1, t=0.2, run_id="edge-run", blocked=("left", "right")):
    """A frame with one HIGH pothole in the car's lane, 75 m ahead of the hazard camera at sequence 1 and 2 m
    nearer with each frame, as the car of sensor_frame() moves. With both sides blocked the decision is MONITOR at
    CAUTION for the first 27 frames: not urgent, and with a cause, so one the model is asked to explain."""
    frame = sensor_frame(sequence, t, run_id)
    frame["objects"] = [[[77 - 2 * sequence, 0, 0], "pothole severity=HIGH length=1.2 width=0.9 depth=0.08"]]
    for side in blocked:
        frame["sides"][side][0] = 2
    return frame


def test_paused_simulation_and_skipped_frames_are_accepted_when_the_stream_resumes(client):
    for sequence in (1, 2):
        assert client.post("/api/sensor-frame", json=sensor_frame(sequence, 0.2 * sequence)).status_code == 200
    # Webots was paused, then the network dropped frames 3-6: the next frame is later in time and in sequence
    assert client.post("/api/sensor-frame", json=sensor_frame(7, 1.4)).status_code == 200
    state = client.get("/api/live-state").json()
    assert (state["simulation"]["sequence"], state["timestamp"]) == (7, 1.4)
    assert client.get("/api/system").json()["edge"]["run"]["missed_frames"] == 4


def test_explanation_follows_the_edge_decision_without_any_model(client):
    before = client.get("/api/ai-explanation").json()
    assert (before["status"], before["explanation"], before["is_current"]) == ("disabled", None, False)

    assert client.post("/api/sensor-frame", json=pothole_frame()).status_code == 200
    decision = client.get("/api/live-state").json()["unified_safety"]
    assert (decision["recommended_action"], decision["overall_risk"]) == ("MONITOR", "CAUTION")
    answer = client.get("/api/ai-explanation").json()
    assert answer["schema"] == "roadsense.ai.v1" and answer["is_current"] is True
    assert answer["explanation"]["text"] == (
        "HIGH-severity pothole PH_001 is ahead in your lane. "
        "RoadSense recommends monitoring it; no speed or lane change is recommended now.")
    assert (answer["explanation"]["origin"], answer["explanation"]["fallback_reason"]) == ("deterministic", "disabled")
    assert answer["explanation"]["source"] == {
        "run_id": "edge-run", "sequence": 1, "sim_time_s": 0.2, "risk": "CAUTION", "action": "MONITOR",
        "threat_id": "PH_001", "threat_type": "road_hazard", "recommended_lane": None,
        "cause": {"kind": "road_hazard", "id": "PH_001", "hazard": "pothole", "severity": "HIGH", "lane": "ego_lane"}}

    client.post("/api/sensor-frame", json=sensor_frame(run_id="new-run"))  # a restart: nothing mapped any more
    answer = client.get("/api/ai-explanation").json()
    assert answer["explanation"]["source"]["run_id"] == "new-run" and "PH_001" not in answer["explanation"]["text"]


@pytest.fixture
def ollama():
    fake = fake_ollama.serve()
    yield fake
    fake.shutdown()
    fake.server_close()


def ai_app(monkeypatch, url):
    monkeypatch.setenv("ROADSENSE_AI_EXPLAIN", "1")
    monkeypatch.setenv("ROADSENSE_AI_URL", url)
    monkeypatch.setenv("ROADSENSE_AI_COOLDOWN_S", "0")
    monkeypatch.setenv("ROADSENSE_AI_TIMEOUT_S", "2")
    return TestClient(server.app)


def test_model_explanation_of_the_edge_decision_reaches_the_endpoint(monkeypatch, ollama):
    with ai_app(monkeypatch, ollama.url) as client:
        assert client.post("/api/sensor-frame", json=pothole_frame()).status_code == 200
        wait_for(lambda: client.get("/api/ai-explanation").json()["status"] == "ready")
        answer = client.get("/api/ai-explanation").json()
        assert answer["availability"] == "available" and answer["is_current"] is True
        assert answer["explanation"]["origin"] == "llm" and "PH_001" in answer["explanation"]["text"]
        assert answer["explanation"]["source"]["sequence"] == 1 and answer["explanation"]["latency_ms"] > 0
        assert client.get("/api/system").json()["ai"]["latency_ms"]["last"] > 0
    request, = ollama.requests
    assert (request["model"], request["stream"], request["think"]) == ("qwen3:0.6b", False, False)
    assert "PH_001" in request["prompt"] and "tools" not in request


def test_sensor_frames_are_processed_at_once_while_the_model_never_answers(monkeypatch, ollama):
    release = threading.Event()
    ollama.script = lambda handler, body: release.wait(10)  # a hung or overloaded model
    try:
        with ai_app(monkeypatch, ollama.url) as client:
            started = time.perf_counter()
            with client.websocket_connect("/ws/live") as ws:
                for sequence in range(1, 21):
                    response = client.post("/api/sensor-frame", json=pothole_frame(sequence, 0.2 * sequence))
                    assert response.status_code == 200, response.text
                    assert recv(ws)["simulation"]["sequence"] == sequence  # and published, in order
            elapsed = time.perf_counter() - started
            answer = client.get("/api/ai-explanation").json()
            assert answer["status"] == "generating" and answer["explanation"]["origin"] == "deterministic"
            assert answer["explanation"]["fallback_reason"] == "awaiting_model" and answer["is_current"] is True
            latency = client.get("/api/system").json()["edge"]["latency_ms"]
            assert latency["samples"] == 20 and latency["handler"]["max"] < 500  # ms: no frame waited for the model
            assert elapsed < 5.0
    finally:
        release.set()
    assert len(ollama.requests) == 1  # one request in flight, however many frames


def test_sensor_frames_are_processed_while_ollama_is_stopped(monkeypatch):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        stopped = "http://127.0.0.1:%d/api/generate" % unused.getsockname()[1]
    with ai_app(monkeypatch, stopped) as client:
        for sequence in range(1, 6):
            assert client.post("/api/sensor-frame", json=pothole_frame(sequence, 0.2 * sequence)).status_code == 200
        assert client.get("/api/live-state").json()["unified_safety"]["recommended_action"] == "MONITOR"
        wait_for(lambda: client.get("/api/ai-explanation").json()["availability"] == "unavailable")
        wait_for(lambda: client.get("/api/ai-explanation").json()["last_error"] is not None)
        answer = client.get("/api/ai-explanation").json()
        assert answer["explanation"]["origin"] == "deterministic" and "PH_001" in answer["explanation"]["text"]
        assert answer["last_error"]["kind"] == "unavailable"
        assert client.post("/api/sensor-frame", json=pothole_frame(6, 1.2)).status_code == 200


def test_a_failing_listener_never_costs_a_state():
    def broken(state):
        raise RuntimeError("explainer bug")

    hub = live.LiveStateHub(listeners=[broken])
    for k in (1, 2):
        assert hub.ingest(json.dumps(snapshot(k)).encode())["accepted"] is True
    assert json.loads(hub.latest_json) == snapshot(2) and hub.metrics.summary()["frames"]["accepted"] == 2


# --- hazard records


def test_hazard_map_is_served_as_records_in_simulation_coordinates(client):
    assert client.get("/api/hazards").json() == {
        "schema": "roadsense.hazards.v1", "run_id": None, "coordinate_frame": "webots_world", "geo_configured": False,
        "records": []}
    for sequence in (1, 2, 3):  # three observations of one pothole are one record
        assert client.post("/api/sensor-frame", json=pothole_frame(sequence, 0.2 * sequence)).status_code == 200
    hazards = client.get("/api/hazards").json()
    record, = hazards["records"]
    live_hazard, = client.get("/api/live-state").json()["hazards"]
    assert (hazards["run_id"], hazards["geo_configured"]) == ("edge-run", False)
    assert record["schema"] == "roadsense.hazard.v1" and record["record_id"] == "edge-run:PH_001"
    assert (record["type"], record["severity"], record["observations"], record["status"]) == ("pothole", "HIGH", 3, "ACTIVE")
    assert record["confidence"] == live_hazard["confidence"] > 0.9
    assert record["position"] == {"frame": "webots_world", "x_m": live_hazard["world_position"]["x"],
                                  "y_m": live_hazard["world_position"]["y"], "geo": None}  # no GPS position is made up
    assert record["dimensions_m"] == {"length": 1.2, "width": 0.9, "depth": 0.08}
    assert record["observed"] == {"clock": "simulation", "first_seen_s": 0.2, "last_seen_s": 0.6}
    assert record["provenance"] == {"simulated": True, "sensor": "webots_simulated_road_sensor",
                                    "vehicle_id": "EGO_ROADSENSE", "run_id": "edge-run", "processor": "edge"}
