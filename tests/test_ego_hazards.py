"""EGO_ROADSENSE road hazards (Webots digital twin): detections, the simulated sensor, the persistent hazard map."""
import json
import math
import sys
from pathlib import Path

import pytest

# The modules live next to the Webots controller, outside the roadsense package, and import perception.py from there.
_DIR = str(Path(__file__).resolve().parents[1] / "simulation/webots/controllers/roadsense_ego")
sys.path.insert(0, _DIR)
try:
    import hazard_map
    import hazards
finally:
    sys.path.remove(_DIR)

HIGHWAY_POSE = (-100.0, 6.875, math.pi)  # ego in the middle lane, heading -x like EGO_ROADSENSE
POTHOLE = "pothole severity=HIGH length=1.2 width=0.9 depth=0.08"


def _det(t, x, y, severity="HIGH", confidence=0.9, hazard_type="pothole", dimensions=None):
    return hazards.HazardDetection(t, "test_sensor", hazard_type, x, y, severity, confidence, dimensions)


def _pose(x, y=3.2):
    """EGO_ROADSENSE in the median-side lane, heading -x."""
    return x, y, math.pi


# --- simulated hazard sensor (the hazard camera's recognized objects)


def test_recognized_pothole_becomes_a_detection_on_the_ground():
    (d,) = hazards.simulated_detections([((20.0, 1.5, -1.4), POTHOLE)], 21.0, HIGHWAY_POSE)
    assert (d.timestamp, d.source, d.hazard_type, d.severity) == (21.0, "webots_simulated_road_sensor", "pothole", "HIGH")
    # the camera sits HAZARD_CAMERA ahead of the ego origin; heading -x, ahead is -x and left is -y
    assert (d.world_x, d.world_y) == pytest.approx((-100.0 - (hazards.HAZARD_CAMERA[0] + 20.0), 6.875 - 1.5))
    assert d.confidence == hazards.CONFIDENCE
    assert d.dimensions == pytest.approx({"length_m": 1.2, "width_m": 0.9, "depth_m": 0.08})


@pytest.mark.parametrize("position, label", [
    ((15.0, 0.0, -0.5), "BMW X5"),  # a car: recognized, but not a road hazard
    ((15.0, 0.0, -0.5), ""),
    ((15.0, 0.0, -1.4), "pothole severity=SEVERE length=1 width=1 depth=0.1"),  # not a RoadSense severity
    ((15.0, 0.0, -1.4), "pothole length=1 width=1 depth=0.1"),  # no severity
    ((float("nan"), 0.0, -1.4), POTHOLE),
])
def test_unusable_recognized_objects_are_ignored(position, label):
    assert hazards.simulated_detections([(position, label)], 21.0, HIGHWAY_POSE) == []


def test_pothole_without_readable_size_is_still_detected():
    (d,) = hazards.simulated_detections([((15.0, 0.0, -1.4), "pothole severity=LOW length=? width=0.4")], 3.0,
                                        HIGHWAY_POSE)
    assert (d.severity, d.dimensions) == ("LOW", None)


# --- ego-relative state


@pytest.mark.parametrize("ego_y, hazard_y, relation", [
    (3.2, 3.3, "ego_lane"),
    (3.2, 6.9, "right_lane"),
    (6.875, 3.3, "left_lane"),
    (3.2, 10.6, "other_lane"),  # two lanes over
    (3.2, 0.5, "off_road"),  # the median
    (3.2, -3.2, "off_road"),  # the other carriageway
    (-3.2, 3.3, "unknown"),  # EGO_ROADSENSE off its carriageway
])
def test_lane_relation(ego_y, hazard_y, relation):
    assert hazard_map.lane_relation(hazard_y, ego_y) == relation


def test_relative_state_ahead_then_behind():
    assert hazard_map.relative(-470.0, 3.3, _pose(-400.0)) == {
        "longitudinal_m": 70.0, "lateral_m": -0.1, "distance_m": 70.0, "bearing_deg": -0.1,
        "lane_relation": "ego_lane", "direction": "ahead"}
    assert hazard_map.relative(-470.0, 3.3, _pose(-480.0)) == {
        "longitudinal_m": -10.0, "lateral_m": -0.1, "distance_m": 10.0, "bearing_deg": -179.4,
        "lane_relation": "ego_lane", "direction": "behind"}


# --- hazard map


def test_first_detection_creates_a_hazard_with_a_roadsense_id():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    (new,) = m.update([_det(21.0, -470.0, 3.3)])
    assert new == {
        "hazard_id": "PH_001",
        "type": "pothole",
        "world_position": {"x": -470.0, "y": 3.3},
        "severity": "HIGH",
        "confidence": 0.9,
        "dimensions": None,
        "first_seen_s": 21.0,
        "last_seen_s": 21.0,
        "observation_count": 1,
        "source_vehicle": "EGO_ROADSENSE",
        "source": "test_sensor",
        "status": "ACTIVE",
    }
    assert m.get("PH_001") == new and m.get("PH_002") is None


def test_repeated_detections_update_the_same_hazard():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3)])
    assert m.update([_det(21.2, -469.7, 3.4)]) == []  # nothing new
    assert m.update([_det(21.4, -470.3, 3.2)]) == []
    (hazard,) = m.active_hazards()
    assert (hazard["hazard_id"], hazard["first_seen_s"], hazard["last_seen_s"], hazard["observation_count"]) == (
        "PH_001", 21.0, 21.4, 3)
    assert (hazard["world_position"]["x"], hazard["world_position"]["y"]) == pytest.approx((-470.0, 3.3))  # the mean


def test_seen_times_are_rounded_to_the_millisecond_like_track_records():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(0.2 + 0.2 + 20.000000000000004, -470.0, 3.3)])  # simulation times are float sums
    m.update([_det(20.600000000000005, -470.0, 3.3)])
    assert (m.get("PH_001")["first_seen_s"], m.get("PH_001")["last_seen_s"]) == (20.4, 20.6)


def test_two_detections_of_a_new_hazard_in_one_cycle_make_one_hazard():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    assert [h["hazard_id"] for h in m.update([_det(21.0, -470.0, 3.3), _det(21.0, -470.4, 3.1)])] == ["PH_001"]
    assert m.get("PH_001")["observation_count"] == 2


def test_merge_radius_separates_nearby_hazards():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3)])
    assert m.update([_det(21.2, -470.0 - (hazard_map.MERGE_RADIUS_M - 0.1), 3.3)]) == []
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3)])
    assert [h["hazard_id"] for h in m.update([_det(21.2, -470.0 - (hazard_map.MERGE_RADIUS_M + 0.1), 3.3)])] == [
        "PH_002"]


def test_ids_follow_discovery_order_and_hazard_type():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3)])
    m.update([_det(30.0, -700.0, 6.9, severity="MEDIUM"), _det(30.0, -950.0, 3.2, severity="LOW")])
    m.update([_det(31.0, -700.0, 6.9, hazard_type="rough_road")])  # same place, another kind of hazard
    assert [h["hazard_id"] for h in m.active_hazards()] == ["PH_001", "PH_002", "PH_003", "HZ_001"]


def test_confidence_rises_slightly_with_each_observation_and_stays_below_one():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3, confidence=0.9)])
    m.update([_det(21.2, -470.0, 3.3, confidence=0.9)])
    # each consistent observation closes CONFIDENCE_GAIN x its confidence of the gap to 1
    assert m.get("PH_001")["confidence"] == pytest.approx(1 - 0.1 * (1 - hazard_map.CONFIDENCE_GAIN * 0.9))
    for k in range(200):
        m.update([_det(21.4 + 0.2 * k, -470.0, 3.3, confidence=0.9)])
    assert m.get("PH_001")["confidence"] == hazard_map.MAX_CONFIDENCE < 1.0


@pytest.mark.parametrize("first, then, kept", [("HIGH", "LOW", "HIGH"), ("LOW", "MEDIUM", "MEDIUM")])
def test_severity_is_the_highest_reported(first, then, kept):
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3, severity=first)])
    m.update([_det(21.2, -470.0, 3.3, severity=then)])
    assert m.get("PH_001")["severity"] == kept


def test_dimensions_follow_the_latest_detection_that_has_them():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    size = {"length_m": 1.2, "width_m": 0.9, "depth_m": 0.08}
    m.update([_det(21.0, -470.0, 3.3)])
    m.update([_det(21.2, -470.0, 3.3, dimensions=size)])
    m.update([_det(21.4, -470.0, 3.3)])
    assert m.get("PH_001")["dimensions"] == size


@pytest.mark.parametrize("detection", [
    _det(21.0, float("nan"), 3.3),
    _det(21.0, -470.0, None),
    _det(21.0, -470.0, 3.3, severity="SEVERE"),
    _det(21.0, -470.0, 3.3, confidence=1.5),
    _det(21.0, -470.0, 3.3, confidence=float("nan")),
    _det(21.0, -470.0, 3.3, hazard_type=""),
])
def test_malformed_detections_are_ignored(detection):
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    assert m.update([detection]) == [] and m.active_hazards() == []
    assert m.update([]) == []


def _three_potholes():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3)])
    m.update([_det(30.0, -700.0, 6.9, severity="MEDIUM")])
    m.update([_det(42.0, -950.0, 3.2, severity="LOW")])
    return m


def test_nearest_hazard_may_be_behind():
    m = _three_potholes()
    nearest = m.nearest_hazard(_pose(-480.0))  # 10 m past PH_001, 220 m before PH_002
    assert (nearest["hazard_id"], nearest["relative"]["direction"]) == ("PH_001", "behind")
    assert hazard_map.HazardMap("EGO_ROADSENSE").nearest_hazard(_pose(-480.0)) is None


def test_hazards_ahead_nearest_first_within_a_distance():
    m = _three_potholes()
    assert [h["hazard_id"] for h in m.hazards_ahead(_pose(-480.0))] == ["PH_002", "PH_003"]
    assert [h["hazard_id"] for h in m.hazards_ahead(_pose(-480.0), max_distance_m=300.0)] == ["PH_002"]
    assert m.hazards_ahead(_pose(-1000.0)) == []


def test_hazard_stays_in_the_map_after_ego_passes_it():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3)])
    assert m.hazards_ahead(_pose(-410.0))[0]["relative"]["direction"] == "ahead"
    m.update([])  # driving on, nothing in view
    (hazard,) = m.snapshot(60.0, _pose(-1300.0))["hazards"]
    assert (hazard["hazard_id"], hazard["relative"]["direction"], hazard["relative"]["distance_m"]) == (
        "PH_001", "behind", 830.0)


def test_snapshot_is_serializable():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    m.update([_det(21.0, -470.0, 3.3, dimensions={"length_m": 1.2, "width_m": 0.9, "depth_m": 0.08})])
    snapshot = m.snapshot(23.5, _pose(-480.0))
    assert snapshot == {
        "timestamp": 23.5,
        "source_vehicle": "EGO_ROADSENSE",
        "hazards": [{
            "hazard_id": "PH_001",
            "type": "pothole",
            "world_position": {"x": -470.0, "y": 3.3},
            "severity": "HIGH",
            "confidence": 0.9,
            "dimensions": {"length_m": 1.2, "width_m": 0.9, "depth_m": 0.08},
            "first_seen_s": 21.0,
            "last_seen_s": 21.0,
            "observation_count": 1,
            "source_vehicle": "EGO_ROADSENSE",
            "source": "test_sensor",
            "status": "ACTIVE",
            "relative": {"longitudinal_m": -10.0, "lateral_m": -0.1, "distance_m": 10.0, "bearing_deg": -179.4,
                         "lane_relation": "ego_lane", "direction": "behind"},
        }],
    }
    assert json.loads(json.dumps(snapshot)) == snapshot
    snapshot["hazards"][0]["world_position"]["x"] = 0.0  # a copy: the map is unchanged
    assert m.get("PH_001")["world_position"]["x"] == -470.0


# --- console


def test_discovery_event_line():
    m = hazard_map.HazardMap("EGO_ROADSENSE")
    (new,) = m.update([_det(20.4, -470.0, 3.3)])
    assert hazard_map.format_event(20.4, new, _pose(-410.0)) == (
        "[RoadSense:HAZARD_EVENT] t=20.4s discovered PH_001 type=pothole severity=HIGH distance=60.0m lane=ego_lane"
        " conf=0.90")


def test_hazard_summary_lists_the_map_and_the_nearest_hazard_ahead():
    m = _three_potholes()
    assert hazard_map.format_summary(m.snapshot(44.0, _pose(-900.0))) == "\n".join([
        "[RoadSense:HAZARD] t=44.0s map=3 ahead=1 nearest_ahead=PH_003@50.0m severity=LOW lane=ego_lane",
        "  PH_001 pothole behind 430.0m ego_lane   HIGH   conf=0.90 obs=1",
        "  PH_002 pothole behind 200.0m right_lane MEDIUM conf=0.90 obs=1",
        "  PH_003 pothole ahead   50.0m ego_lane   LOW    conf=0.90 obs=1",
    ])
    assert hazard_map.format_summary(hazard_map.HazardMap("EGO_ROADSENSE").snapshot(5.0, _pose(-100.0))) == (
        "[RoadSense:HAZARD] t=5.0s map=0 ahead=0 nearest_ahead=none")
