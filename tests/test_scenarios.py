"""Phase 6C scenario generation and ground-truth outcomes (tools/scenarios.py), without Webots or SUMO."""
import importlib.util
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("scenarios", ROOT / "tools/scenarios.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)
WORLD = (ROOT / "simulation/webots/worlds/roadsense_highway.wbt").read_text()
NET = (ROOT / "simulation/webots/worlds/roadsense_highway_net/sumo.net.xml").read_text()


@pytest.mark.parametrize("family", sc.FAMILIES)
def test_a_seed_fixes_every_parameter(family):
    a, b = sc.draw(family, 7), sc.draw(family, 7)
    assert a == b and sc.routes(a) == sc.routes(b)
    assert sc.draw(family, 8) != a
    assert a["traffic"] == "left" and sc.draw(family, 7, "highway")["traffic"] == "right"
    assert abs(sum(a["mix"].values()) - 1) < 0.01
    assert all(150 <= p["at_m"] for p in a["potholes"] if family != "combined")


def test_each_family_has_its_hazards():
    assert sc.draw("normal", 1)["event"] is None
    for kind in ("sudden_brake", "cut_in", "tailgating"):
        assert sc.draw(kind, 1)["event"]["kind"] == kind
    assert 4 <= len(sc.draw("potholes", 1)["potholes"]) <= 10
    c = sc.draw("combined", 1)
    assert c["event"]["kind"] in ("sudden_brake", "cut_in")
    assert any(0 < p["at_m"] - c["event"]["at_m"] <= 60 for p in c["potholes"])
    assert sc.draw("mixed", 1)["mix"]["motorcycle"] >= 0.3


def test_left_hand_network_reverses_direction_and_keeps_every_lane_in_place():
    rht = ET.fromstring(sc.network(NET, sc.draw("normal", 1, "highway")))
    lht = ET.fromstring(sc.network(NET, sc.draw("normal", 1)))
    assert rht.get("lefthand") is None and lht.get("lefthand") == "true"
    for edge_r, edge_l in zip(rht.iter("edge"), lht.iter("edge")):
        assert (edge_l.get("from"), edge_l.get("to")) == (edge_r.get("to"), edge_r.get("from"))
        for lane_r, lane_l in zip(edge_r.iter("lane"), edge_l.iter("lane")):
            assert lane_l.get("index") == lane_r.get("index")
            assert lane_l.get("shape").split() == lane_r.get("shape").split()[::-1]
    start = {j.get("id"): j.get("incLanes") for j in lht.iter("junction")}
    assert start["Custom0"] == "0_0 0_1 0_2 0_3" and start["Custom1"] == ""
    first = next(lht.iter("edge")).get("shape").split()[0]
    assert first.startswith("0.00,")  # edge 0 now starts at Webots x = -5000 and runs toward +x


def test_the_world_places_the_ego_for_the_profile_and_keeps_the_rest():
    for profile, (x, yaw) in (("highway", ("-40.00", "3.14159")), ("highway_lht", ("-4960.00", "0.00000"))):
        w = sc.world(WORLD, sc.draw("normal", 1, profile), 8901)
        assert re.search(r"DEF WEBOTS_VEHICLE0 LincolnMKZ \{\n  translation %s 6.71 0.4\n  rotation 0 0 1 %s" % (x, yaw), w)
        assert "RoadSensePothole {" not in w and "WEBOTS_VEHICLE1" not in w
        assert "IMPORTABLE EXTERNPROTO \"../protos/RoadSensePothole.proto\"" in w
        assert "SumoInterface {\n  port 8901\n  seed " in w
        assert w.count("controller \"roadsense_ego\"") == 1
        vp = [float(c) for c in re.search(r"Viewpoint \{\n  orientation [^\n]+\n  position ([^\n]+)", w).group(1).split()]
        # SumoInterface shows SUMO vehicles only within its radius of the viewpoint, which follows the ego
        assert math.hypot(vp[0] - float(x), vp[1] - 6.71) == pytest.approx(math.hypot(-31.097 + 40, 12.815 - 6.71), abs=0.01)
    assert "translation -470 3.3 0.01" in WORLD  # the repository world is untouched


def test_traffic_starts_clear_of_the_ego():
    r = ET.fromstring(sc.routes(sc.draw("cut_in", 3)))
    for v in r.iter("vehicle"):
        if v.get("route") == "0":
            assert abs(float(v.get("departPos")) - 40) >= 60
    assert {t.get("id") for t in r.iter("vType")} == set(sc.VTYPES)


def test_a_max_speed_override_changes_only_the_named_vtypes():
    plain = sc.draw("normal", 3)
    fast = dict(plain, vtype_max_speed_mps={"car": 36.1})
    speeds = {t.get("id"): t.get("maxSpeed") for t in ET.fromstring(sc.routes(fast)).iter("vType")}
    assert speeds["car"] == "36.1" and speeds["truck"] == "22"
    assert sc.routes(plain).count('maxSpeed="27"') == 1  # no override: the routes are exactly as before
    assert sc.routes(fast).replace('maxSpeed="36.1"', 'maxSpeed="27"') == sc.routes(plain)


def test_planned_traffic_is_opt_in_and_drives_exactly_its_speed():
    plain = sc.draw("normal", 3)
    assert sc.routes(dict(plain, planned_traffic=[])) == sc.routes(plain)  # without it: the routes as before
    planned = [{"type": "car", "lane": 3, "depart_s": 9.0, "pos_m": 0.0, "speed_mps": 34.0},
               {"type": "motorcycle", "lane": 1, "depart_s": 2.5, "pos_m": 0.0, "speed_mps": 20.0}]
    with_planned = sc.routes(dict(plain, planned_traffic=planned))
    assert with_planned.startswith(sc.routes(plain).rsplit("</routes>", 1)[0])  # appended after the drawn traffic
    r = ET.fromstring(with_planned)
    types = {t.get("id"): t for t in r.iter("vType")}
    behind = [v for v in r.iter("vehicle") if v.get("id").startswith("p")]
    assert [v.get("depart") for v in behind] == ["2.5", "9.0"]  # in depart order, as SUMO requires
    limit = plain["speed_limit_kmh"] / 3.6
    for v, want in zip(behind, (20.0, 34.0)):
        t = types[v.get("type")]
        assert float(v.get("departPos")) < 40 and t.get("speedDev") == "0"  # behind the ego, at a fixed speed
        assert float(t.get("maxSpeed")) == want and min(want, limit * float(t.get("speedFactor"))) == pytest.approx(want)


def test_potholes_are_ahead_along_the_direction_of_travel():
    for profile, sign in (("highway", -1), ("highway_lht", 1)):
        p = sc.draw("potholes", 2, profile)
        start = sc.PROFILES[profile]["start_x"]
        for h, (x, y, _) in zip(p["potholes"], sc.pothole_positions(p)):
            assert math.isclose((x - start) * sign, h["at_m"])
            assert 1.25 < y < 12.5
        assert len(sc.pothole_nodes(p)) == len(p["potholes"])


def ego(x=0.0, speed=20.0, yaw=0.0):
    return {"x": x, "y": 7.0, "yaw": yaw, "speed": speed}


def car(x, y=7.0, speed=20.0, yaw=0.0, length=4.5):
    return {"id": "v", "type": "car", "x": x, "y": y, "yaw": yaw, "speed": speed, "length": length, "width": 1.8}


@pytest.mark.parametrize("yaw", [0.0, math.pi])
def test_outcomes_measure_true_gaps_and_ttc_in_either_direction(yaw):
    d = math.cos(yaw)
    # a car whose rear is 10 m ahead of the ego's front, 5 m/s slower; its front bumper is 4.5 m further on
    lead = car(d * (sc.EGO_FRONT_M + 10 + 4.5), speed=15.0, yaw=yaw)
    o = sc.frame_outcomes({"t": 1.0, "ego": ego(speed=20.0, yaw=yaw), "vehicles": [lead]}, [])
    assert o["lead_gap_m"] == pytest.approx(10.0) and o["lead_ttc_s"] == pytest.approx(2.0)
    assert o["lead_headway_s"] == pytest.approx(0.5) and o["unsafe_headway"] and not o["collision"]
    assert not o["near_miss"]
    o = sc.frame_outcomes({"t": 1.0, "ego": ego(yaw=yaw), "vehicles": [car(d * (sc.EGO_FRONT_M + 2), yaw=yaw)]}, [])
    assert o["collision"] and o["near_miss"]


def test_outcomes_ignore_other_lanes_and_see_followers_and_potholes():
    o = sc.frame_outcomes({"t": 0, "ego": ego(), "vehicles": [car(15, y=10.75)]}, [])
    assert o["lead_gap_m"] is None and not o["collision"]
    follower = car(-sc.EGO_REAR_M - 5, speed=22.0)
    o = sc.frame_outcomes({"t": 0, "ego": ego(speed=20.0), "vehicles": [follower]}, [(1.0, 7.3, 0, 1.0, 0.8)])
    assert o["follow_gap_m"] == pytest.approx(5.0) and o["follow_ttc_s"] == pytest.approx(2.5) and o["tailgated"]
    assert o["pothole_under_ego"] == [0]


def test_a_summary_counts_outcomes_over_frames():
    p = dict(sc.draw("normal", 1), potholes=[])
    frames = [{"t": 0.2 * i, "ego": ego(x=i), "vehicles": [car(i + 30, speed=20.0)]} for i in range(5)]
    summary, outcomes = sc.summarize(frames, p)
    assert summary["frames"] == 5 and summary["frames_with"]["collision"] == 0 and len(outcomes) == 5
    assert summary["min_lead_gap_m"] == pytest.approx(30 - 4.5 - sc.EGO_FRONT_M)
    assert summary["vehicle_observations_by_type"] == {"car": 5}


def test_plugin_writes_vehicle_coordinates():
    # Inspect the actual emitted frame with fake simulator APIs, catching recorder schema regressions.
    import sys
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "tools"))
    from scenario_plugin import SumoSupervisorPlugin
    plugin = SumoSupervisorPlugin.__new__(SumoSupervisorPlugin)
    plugin.done, plugin.event, plugin.frames = False, None, 0
    plugin.p = {"duration_s": 100}
    node = SimpleNamespace(getPosition=lambda: (-0.25, 6.5, 0),
                           getOrientation=lambda: [1, 0, 0, 0, 1, 0, 0, 0, 1],
                           getVelocity=lambda: [5, 0, 0, 0, 0, 0])
    plugin.sup = SimpleNamespace(getTime=lambda: 1.0, vehicles={0: SimpleNamespace(currentID="v", node=node, inUse=True)})
    plugin._ego_state = lambda: dict(ego(), travelled_m=10)
    plugin.traci = SimpleNamespace(vehicle=SimpleNamespace(
        getIDList=lambda: ["v"], getPosition=lambda _: (5001, 15.75), getTypeID=lambda _: "car@v",
        getAngle=lambda _: 90, getSpeed=lambda _: 10, getLength=lambda _: 4.5, getWidth=lambda _: 1.8,
        getRoadID=lambda _: "0", getLaneIndex=lambda _: 2))
    records = []
    plugin._write = records.append
    plugin.run(200)
    vehicle = records[0]["vehicles"][0]
    assert (vehicle["x"], vehicle["y"], vehicle["type"]) == (2, 6.5, "car")
    assert vehicle["speed"] == 5
    assert (records[0]["sumo_vehicles"][0]["x"], records[0]["sumo_vehicles"][0]["y"]) == (1, 7)
    assert sc.frame_outcomes(records[0], [])["collision"]
    plugin.sup.vehicles = {}
    plugin.run(200)
    assert records[1]["vehicles"] == [] and records[1]["missing_physical"] == ["v"]
