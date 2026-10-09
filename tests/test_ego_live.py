"""EGO_ROADSENSE live state (Webots digital twin): the roadsense.live.v1 snapshot and its optional publisher."""
import copy
import http.server
import importlib.util
import json
import math
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

# The modules live next to the Webots controller, outside the roadsense package, and import each other from there.
_DIR = str(Path(__file__).resolve().parents[1] / "simulation/webots/controllers/roadsense_ego")
sys.path.insert(0, _DIR)
try:
    import hazard_map
    import hazards
    import live_publisher
    import live_state
    import perception
    import prediction
    import risk
    import safety
    import telemetry
    import tracking
finally:
    sys.path.remove(_DIR)

POSE = (-400.0, 3.2, math.pi)  # EGO_ROADSENSE in the median-side lane, heading -x


def _strict(text):
    """json.loads that refuses NaN and Infinity, as a browser's JSON.parse does."""
    return json.loads(text, parse_constant=lambda token: pytest.fail("non-finite JSON: %s" % token))


def _cycle(cars=((-430.0, 3.18, -10.0),), potholes=((-460.0, 3.3),), t=0.6):
    """One processing cycle from the real RoadSense modules: cars (world x, y, vx) seen for 0.6 s, potholes seen
    once. Returns the arguments of live_state.build except the run id and sequence."""
    tracker = tracking.Tracker()
    for k in range(3):
        tk = round(0.2 + 0.2 * k, 3)
        tracker.update(tk, [perception.Detection(tk, "radar_front", 0, 0, 0, 0, None, x + vx * (tk - 0.2), y)
                            for x, y, vx in cars])
    reports = [perception.track_report(tr, t, POSE, (-22.2, 0.0)) for tr in tracker.confirmed]
    assessments = [risk.assess(r, prediction.predict(r)) for r in reports]
    hmap = hazard_map.HazardMap("EGO_ROADSENSE")
    hmap.update([hazards.HazardDetection(t, "test_sensor", "pothole", x, y, "HIGH", 0.9,
                                         {"length_m": 1.4, "width_m": 1.0, "depth_m": 0.08}) for x, y in potholes])
    snapshot = hmap.snapshot(t, POSE)
    unified = safety.decide(t, 22.2, assessments, reports, snapshot, ["right_lane"], safety.Policy())
    ego = telemetry.make_record(t, "EGO_ROADSENSE", (POSE[0], POSE[1], 0.31), 22.2, POSE[2], 0.0, (t - 0.2, 22.0),
                                "lane_keep", 2)
    return t, ego, reports, assessments, snapshot, unified, POSE


def _state(**kwargs):
    return live_state.build(*_cycle(**kwargs), run_id="test-run", sequence=7)


# --- the snapshot


def test_snapshot_holds_one_processing_cycle_under_a_versioned_schema():
    t, ego, reports, assessments, snapshot, unified, pose = _cycle()
    state = live_state.build(t, ego, reports, assessments, snapshot, unified, pose, run_id="test-run", sequence=7)
    assert set(state) == {"schema", "timestamp", "ego", "road", "tracks", "hazards", "unified_safety", "simulation"}
    assert (state["schema"], state["timestamp"]) == ("roadsense.live.v1", 0.6)
    assert state["ego"] == ego and state["unified_safety"] == unified and state["hazards"] == snapshot["hazards"]
    assert state["ego"]["timestamp"] == state["unified_safety"]["timestamp"] == state["tracks"][0]["timestamp"] == 0.6
    sim = state["simulation"]
    assert (sim["source"], sim["active"], sim["run_id"], sim["sequence"]) == ("webots", True, "test-run", 7)
    assert abs(sim["generated_at_unix_s"] - time.time()) < 5


def test_each_track_carries_its_report_prediction_and_collision_risk():
    state = _state()
    (track,) = state["tracks"]
    t, ego, (report,), (assessment,), *_ = _cycle()
    assert {k: track[k] for k in report} == report  # the perception record, unchanged
    assert track["prediction"] == {"model": "constant_velocity", "trajectory": assessment["trajectory"]}
    assert track["collision"] == {k: assessment[k] for k in (
        "ttc_s", "time_to_cpa_s", "min_separation_m", "closing", "conflict", "risk")}
    assert track["collision"]["risk"] in ("HIGH", "CRITICAL") and track["collision"]["ttc_s"] > 0  # closing at 12 m/s


def test_trajectory_and_track_share_the_ego_frame():
    (track,) = _state()["tracks"]
    p, v = track["relative_position"], track["relative_velocity"]
    first = track["prediction"]["trajectory"][0]  # 0.5 s ahead, same frame: x ahead, y left
    assert (first["longitudinal_m"], first["lateral_m"]) == pytest.approx(
        (p["longitudinal_m"] + 0.5 * v["longitudinal_mps"], p["lateral_m"] + 0.5 * v["lateral_mps"]), abs=0.01)


def test_safe_track_has_null_ttc_in_json():
    state = _state(cars=((-430.0, 3.18, -24.0),))  # pulling away from EGO_ROADSENSE
    collision = _strict(live_state.encode(state))["tracks"][0]["collision"]
    assert (collision["risk"], collision["ttc_s"], collision["conflict"]) == ("SAFE", None, False)


def test_empty_road_gives_empty_lists_and_a_safe_decision():
    state = _state(cars=(), potholes=())
    assert (state["tracks"], state["hazards"]) == ([], [])
    assert state["unified_safety"]["overall_risk"] == "SAFE"
    assert _strict(live_state.encode(state))["tracks"] == []


def test_lanes_are_given_relative_to_ego():
    road = live_state.road(POSE)  # ego at y = 3.2; lane centres at y = 3.125, 6.875, 10.625, 14.375
    assert road["lane_width_m"] == perception.LANE_WIDTH_M and road["heading_deg"] == 0.0
    assert [(lane["relation"], lane["driving"]) for lane in road["lanes"]] == [
        ("ego_lane", True), ("right_lane", True), ("other_lane", True), ("other_lane", False)]  # pedestrians' lane
    assert [lane["center_lateral_m"] for lane in road["lanes"]] == pytest.approx(
        [0.075, -3.675, -7.425, -11.175], abs=0.006)  # the median-side lane's centre is just left of the car


def test_road_heading_is_relative_to_ego_heading():
    # EGO_ROADSENSE turned 3 degrees to its left: the road now runs 3 degrees to its right
    assert live_state.road((POSE[0], POSE[1], math.pi + math.radians(3)))["heading_deg"] == pytest.approx(-3.0)


def test_lane_relation_is_unknown_off_the_carriageway():
    assert {lane["relation"] for lane in live_state.road((-5200.0, -3.0, math.pi))["lanes"]} == {"unknown"}


def test_encoding_turns_unavailable_numbers_into_null():
    state = _state()
    state["ego"]["acceleration_mps2"] = float("nan")
    state["hazards"][0]["dimensions"]["depth_m"] = float("inf")
    state["tracks"][0]["collision"]["min_separation_m"] = float("-inf")
    decoded = _strict(live_state.encode(state))
    assert decoded["ego"]["acceleration_mps2"] is None
    assert decoded["hazards"][0]["dimensions"]["depth_m"] is None
    assert decoded["tracks"][0]["collision"]["min_separation_m"] is None


def test_encoding_round_trips_every_finite_value():
    state = _state()
    assert _strict(live_state.encode(state)) == json.loads(json.dumps(state))


def test_building_and_encoding_change_no_input():
    cycle = _cycle()
    before = copy.deepcopy(cycle)
    live_state.encode(live_state.build(*cycle, run_id="test-run", sequence=1))
    assert cycle == before


def test_finished_snapshot_marks_the_simulation_inactive_with_the_next_sequence():
    state = _state()
    final = live_state.finished(state)
    assert (final["simulation"]["active"], final["simulation"]["sequence"]) == (False, 8)
    assert state["simulation"]["active"] is True and final["tracks"] == state["tracks"]


def test_stream_listener_summarises_a_snapshot():
    pytest.importorskip("websockets")  # the dev tool needs the web extra
    spec = importlib.util.spec_from_file_location("live_listen", Path(_DIR).parents[3] / "tools/live_listen.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    line = tool.summary(_strict(live_state.encode(_state())))
    assert line.startswith("seq=7 t=0.6s speed=79.9km/h tracks=1 nearest=TRACK_001@34.0m conflicts=1 hazards=1 "
                           "next_hazard=PH_001@60.0m risk=")
    assert line.endswith("threat=TRACK_001")
    assert tool.summary(_strict(live_state.encode(live_state.finished(_state())))).endswith("(simulation ended)")


# --- configuration


def test_publishing_is_off_unless_asked_for():
    assert live_publisher.config_from_environment({}) is None
    assert live_publisher.config_from_environment({"ROADSENSE_LIVE_PUBLISH": "0"}) is None
    assert live_publisher.config_from_environment({"ROADSENSE_LIVE_PUBLISH": "1"}) == live_publisher.Config(
        "http://127.0.0.1:8000/api/live-state", 0.2, 0.5)


def test_configuration_comes_from_the_environment():
    config = live_publisher.config_from_environment({
        "ROADSENSE_LIVE_PUBLISH": "yes", "ROADSENSE_LIVE_URL": "http://127.0.0.1:9000/api/live-state",
        "ROADSENSE_LIVE_PERIOD_S": "0.5", "ROADSENSE_LIVE_TIMEOUT_S": "0.25"})
    assert config == live_publisher.Config("http://127.0.0.1:9000/api/live-state", 0.5, 0.25)


@pytest.mark.parametrize("name, value", [("ROADSENSE_LIVE_PERIOD_S", "fast"), ("ROADSENSE_LIVE_PERIOD_S", "0"),
                                         ("ROADSENSE_LIVE_TIMEOUT_S", "-1"), ("ROADSENSE_LIVE_TIMEOUT_S", "nan")])
def test_unusable_setting_falls_back_to_its_default_with_a_warning(name, value):
    lines = []
    config = live_publisher.config_from_environment({"ROADSENSE_LIVE_PUBLISH": "on", name: value}, log=lines.append)
    assert config == live_publisher.Config("http://127.0.0.1:8000/api/live-state", 0.2, 0.5)
    assert len(lines) == 1 and name in lines[0]


# --- publisher, against a real local HTTP server


class _Server(http.server.ThreadingHTTPServer):
    def server_bind(self):  # without HTTPServer's reverse-DNS lookup of the address, which can stall for 30 s
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class _Backend:
    """A local HTTP server that records POSTed bodies; `gate` holds requests back while cleared."""

    def __init__(self, port=0, delay_s=0.0):
        self.bodies, self.gate = [], threading.Event()
        self.gate.set()
        backend = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                backend.gate.wait(5)
                time.sleep(delay_s)
                backend.bodies.append(body)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = _Server(("127.0.0.1", port), Handler)
        self.url = "http://127.0.0.1:%d/api/live-state" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_publisher_posts_each_snapshot_as_json():
    backend, lines = _Backend(), []
    publisher = live_publisher.LivePublisher(backend.url, 0.5, log=lines.append)
    try:
        publisher.publish(b'{"n":1}')
        _wait_for(lambda: backend.bodies == [b'{"n":1}'])
        publisher.publish(b'{"n":2}')
        _wait_for(lambda: len(backend.bodies) == 2)
        assert lines == ["[RoadSense:LIVE] publishing live state to %s" % backend.url]
    finally:
        publisher.close()
        backend.close()


def test_publish_never_waits_and_only_the_newest_waiting_snapshot_is_sent():
    backend = _Backend()
    backend.gate.clear()  # the backend stalls the first request
    publisher = live_publisher.LivePublisher(backend.url, 5.0, log=lambda line: None)
    try:
        publisher.publish(b"1")
        _wait_for(lambda: publisher.in_flight)
        started = time.monotonic()
        for n in (b"2", b"3", b"4", b"5"):
            publisher.publish(n)
        assert time.monotonic() - started < 0.05
        backend.gate.set()
        _wait_for(lambda: backend.bodies == [b"1", b"5"])
        assert publisher.skipped == 3
    finally:
        publisher.close()
        backend.close()


def test_unreachable_backend_is_reported_once_and_retried_with_backoff():
    lines = []
    url = "http://127.0.0.1:%d/api/live-state" % _free_port()  # nothing listens there
    publisher = live_publisher.LivePublisher(url, 0.5, log=lines.append, first_backoff_s=0.05, max_backoff_s=0.2)
    try:
        for k in range(40):
            publisher.publish(b"%d" % k)
            time.sleep(0.01)
        _wait_for(lambda: publisher.failed >= 3)
        assert len(lines) == 1 and lines[0].startswith("[RoadSense:LIVE] cannot reach %s" % url)
        assert publisher.failed < 10  # backing off, not one attempt per snapshot
    finally:
        publisher.close()


def test_slow_backend_times_out_without_holding_up_the_caller():
    backend, lines = _Backend(delay_s=1.0), []
    publisher = live_publisher.LivePublisher(backend.url, 0.1, log=lines.append, first_backoff_s=5.0)
    try:
        started = time.monotonic()
        publisher.publish(b"1")
        assert time.monotonic() - started < 0.05
        _wait_for(lambda: publisher.failed == 1, timeout=2.0)
        assert "timed out" in lines[0]
    finally:
        publisher.close(timeout_s=0.1)
        backend.close()


def test_publisher_recovers_when_the_backend_comes_up():
    port, lines = _free_port(), []
    url = "http://127.0.0.1:%d/api/live-state" % port
    publisher = live_publisher.LivePublisher(url, 0.5, log=lines.append, first_backoff_s=0.05, max_backoff_s=0.1)
    backend = None
    try:
        publisher.publish(b"lost")
        _wait_for(lambda: publisher.failed >= 1)
        publisher.publish(b"newest")  # replaces the failed snapshot waiting for its retry
        backend = _Backend(port=port)
        _wait_for(lambda: backend.bodies == [b"newest"])
        assert lines[0].startswith("[RoadSense:LIVE] cannot reach") and lines[-1].startswith(
            "[RoadSense:LIVE] publishing live state to %s again" % url)
    finally:
        publisher.close()
        if backend:
            backend.close()


def test_failed_snapshot_is_retried_while_nothing_newer_comes():
    port = _free_port()
    publisher = live_publisher.LivePublisher("http://127.0.0.1:%d/api/live-state" % port, 0.5, log=lambda line: None,
                                             first_backoff_s=0.05, max_backoff_s=0.1)
    backend = None
    try:
        publisher.publish(b"only")  # e.g. the final snapshot of a run, while the server restarts
        _wait_for(lambda: publisher.failed >= 1)
        backend = _Backend(port=port)
        _wait_for(lambda: backend.bodies == [b"only"])
    finally:
        publisher.close()
        if backend:
            backend.close()


def test_close_sends_the_last_waiting_snapshot():
    backend = _Backend()
    publisher = live_publisher.LivePublisher(backend.url, 0.5, log=lambda line: None)
    try:
        publisher.publish(b"final")
        publisher.close()
        assert backend.bodies == [b"final"]
    finally:
        backend.close()


def test_backend_problems_never_reach_the_caller():
    def broken(payload):
        raise RuntimeError("anything at all")

    publisher = live_publisher.LivePublisher("http://127.0.0.1:1/x", 0.5, log=lambda line: None, send=broken)
    try:
        publisher.publish(b"1")  # no exception here or later
        _wait_for(lambda: publisher.failed == 1)
    finally:
        publisher.close()
