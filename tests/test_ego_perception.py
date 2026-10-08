"""EGO_ROADSENSE perception and tracking (Webots digital twin): radar geometry, ego/world frames, lanes, tracks."""
import importlib.util
import math
from pathlib import Path

import pytest

# The modules live next to the Webots controller, outside the roadsense package.
_DIR = Path(__file__).resolve().parents[1] / "simulation/webots/controllers/roadsense_ego"


def _load(name):
    spec = importlib.util.spec_from_file_location("roadsense_ego_" + name, _DIR / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perception = _load("perception")
tracking = _load("tracking")

HIGHWAY_POSE = (-100.0, 6.875, math.pi)  # ego in the middle lane, heading -x like EGO_ROADSENSE


def _detect(name, *targets, pose=HIGHWAY_POSE, t=1.0):
    """Detections of one radar seeing 1-150 m over 2.6 rad; targets are (distance, azimuth, speed)."""
    return perception.radar_detections(name, list(targets), (1.0, 150.0, 2.6), t, pose)


def _det(t, x, y, range_rate=None, source="radar_front"):
    """A detection at world (x, y); the ego-frame fields don't matter to the tracker."""
    return perception.Detection(t, source, 0.0, 0.0, 0.0, 0.0, range_rate, x, y)


# --- radar targets -> ego-frame detections


def test_front_radar_target_straight_ahead():
    (d,) = _detect("radar front", (20.0, 0.0, -5.0))
    assert (d.source, d.timestamp, d.range_rate_mps) == ("radar_front", 1.0, -5.0)
    assert (d.longitudinal_m, d.lateral_m) == pytest.approx((24.054, 0.0))  # radar 4.054 m ahead of the ego origin
    assert (d.distance_m, d.bearing_deg) == pytest.approx((24.054, 0.0))


def test_positive_radar_azimuth_is_to_the_radars_right():
    (d,) = _detect("radar front", (20.0, 0.1, 0.0))
    assert (d.longitudinal_m, d.lateral_m) == pytest.approx((23.9541, -1.9967), abs=1e-4)
    assert (d.distance_m, d.bearing_deg) == pytest.approx((24.0372, -4.7648), abs=1e-4)


def test_rear_radar_faces_backwards_so_its_right_is_the_cars_left():
    (d,) = _detect("radar rear", (10.0, 0.2, 3.0))
    assert (d.source, d.range_rate_mps) == ("radar_rear", 3.0)
    assert (d.longitudinal_m, d.lateral_m) == pytest.approx((-10.9607, 1.9867), abs=1e-4)


def test_targets_clamped_to_the_radar_limits_are_dropped():
    # Webots clamps what it can't place: the ego's own body to minRange, a vehicle reaching into range to
    # maxRange, a vehicle alongside whose origin is outside the field of view to its edge (plus angular noise)
    detections = _detect("radar front", (1.0, 0.2, 0.0), (150.0, 0.0, 0.0), (8.0, 1.3, 0.0), (8.0, -1.2992, 0.0),
                         (60.0, 0.0, 0.0), (8.0, -1.29, 0.0))
    assert [d.distance_m for d in detections] == pytest.approx([64.054, 9.9202], abs=1e-4)


def test_implausible_range_rates_are_unknown():
    # nan on the radar's first refresh, ~50 km/s when SUMO teleports a car onto the road
    detections = _detect("radar front", (30.0, 0.0, math.nan), (31.0, 0.0, 50000.0), (32.0, 0.0, -7.5))
    assert [d.range_rate_mps for d in detections] == [None, None, -7.5]


def test_detection_world_position_uses_the_ego_pose():
    (d,) = _detect("radar front", (20.0, 0.0, 0.0))
    assert (d.world_x, d.world_y) == pytest.approx((-124.054, 6.875))  # 24.054 m along the -x heading


# --- ego/world frames


@pytest.mark.parametrize("pose, world, ego", [
    ((10.0, 5.0, math.pi / 2), (9.0, 7.0), (2.0, 1.0)),
    # EGO_ROADSENSE heads -x, so the median side (smaller y) is its left
    (HIGHWAY_POSE, (-130.0, 3.125), (30.0, 3.75)),
    (HIGHWAY_POSE, (-90.0, 10.625), (-10.0, -3.75)),
])
def test_world_and_ego_frames(pose, world, ego):
    assert perception.world_to_ego(*world, pose) == pytest.approx(ego)
    assert perception.ego_to_world(*ego, pose) == pytest.approx(world)


# --- lanes


@pytest.mark.parametrize("ego_y, target_y, relation", [
    (6.875, 7.03, "ego_lane"),
    (6.875, 3.18, "left_lane"),
    (6.875, 10.87, "right_lane"),
    (6.875, 14.7, "other"),  # two lanes to the right
    (3.2, 10.87, "other"),
    (3.2, 0.5, "other"),  # median
    (6.875, -3.18, "other"),  # opposite carriageway
    (10.6, 8.0, "left_lane"),
])
def test_lane_relation(ego_y, target_y, relation):
    assert perception.lane_relation(target_y, ego_y) == relation


# --- tracking


def _run(tracker, cycles, targets, start=0.2):
    """Feed one detection per target every 0.2 s; targets are (x0, y0, vx, vy) moving in the world frame."""
    for k in range(cycles):
        t = round(start + 0.2 * k, 3)
        tracker.update(t, [_det(t, x + vx * (t - start), y + vy * (t - start)) for x, y, vx, vy in targets])
    return t


def test_tracks_seen_over_0_4_s_are_confirmed_with_numbered_ids_and_a_velocity():
    tracker = tracking.Tracker()
    cars = [(-120.0, 7.03, -10.0, 0.0), (-150.0, 3.18, -10.0, 0.0)]
    _run(tracker, 2, cars)  # 0.2, 0.4 s
    assert len(tracker.tracks) == 2 and tracker.confirmed == []
    assert [tr.velocity for tr in tracker.tracks] == [None, None]
    _run(tracker, 1, [(x - 4.0, y, 0.0, 0.0) for x, y, _, _ in cars], start=0.6)
    assert [tr.track_id for tr in tracker.confirmed] == ["TRACK_001", "TRACK_002"]
    assert [(tr.first_seen, tr.last_seen) for tr in tracker.confirmed] == [(0.2, 0.6), (0.2, 0.6)]
    assert [tr.velocity for tr in tracker.confirmed] == [pytest.approx((-10.0, 0.0))] * 2


def test_a_glimpse_is_dropped_without_using_an_id():
    tracker = tracking.Tracker()
    tracker.update(0.2, [_det(0.2, -120.0, -3.18)])  # e.g. an oncoming car seen once under the guardrail
    tracker.update(0.6, [])
    assert len(tracker.tracks) == 1  # coasts 0.4 s
    tracker.update(0.8, [])
    assert tracker.tracks == []
    _run(tracker, 3, [(-150.0, 7.03, -10.0, 0.0)], start=1.0)
    assert [tr.track_id for tr in tracker.confirmed] == ["TRACK_001"]


def test_side_by_side_cars_keep_their_ids():
    tracker = tracking.Tracker()
    # two cars 3.85 m apart in neighbouring lanes, moving at 13.9 m/s, the detections listed in a changing order
    for k in range(20):
        t = round(0.2 + 0.2 * k, 3)
        cars = [_det(t, -120.0 - 13.9 * 0.2 * k, 7.03), _det(t, -121.0 - 13.9 * 0.2 * k, 10.87)]
        tracker.update(t, cars[::-1] if k % 2 else cars)
    a, b = tracker.tracks
    assert (a.track_id, b.track_id) == ("TRACK_001", "TRACK_002") and tracker.confirmed == [a, b]
    assert a.position_at(t)[1] == pytest.approx(7.03) and b.position_at(t)[1] == pytest.approx(10.87)
    assert a.last_seen == b.last_seen == t


def test_velocity_is_the_displacement_over_the_last_1_5_seconds():
    tracker = tracking.Tracker()
    t = _run(tracker, 6, [(-120.0, 7.03, -10.0, 0.5)])  # 0.2 .. 1.2 s
    assert tracker.tracks[0].velocity == pytest.approx((-10.0, 0.5))
    # from 1.2 s on at -20 m/s: 1.4 s later the velocity still includes some of the slower part, 1.6 s later not
    x, y = tracker.tracks[0].position_at(t)
    t = _run(tracker, 7, [(x - 4.0, y, -20.0, 0.0)], start=round(t + 0.2, 3))  # 1.4 .. 2.6 s
    assert tracker.tracks[0].velocity[0] == pytest.approx((-158.0 - -128.0) / 1.6)  # since x = -128 at 1.0 s
    _run(tracker, 1, [(-130.0 - 20.0 * 1.6, y, 0.0, 0.0)], start=round(t + 0.2, 3))  # 2.8 s
    assert tracker.tracks[0].velocity == pytest.approx((-20.0, 0.0))


def test_unseen_track_coasts_at_most_3_s_then_expires():
    tracker = tracking.Tracker()
    _run(tracker, 21, [(-120.0, 7.03, -10.0, 0.0)])  # seen 0.2 .. 4.2 s, last at x = -160
    tracker.update(7.2, [])  # unseen for exactly 3.0 s: still kept, moved on at its velocity
    (track,) = tracker.tracks
    assert track.position_at(7.2) == pytest.approx((-190.0, 7.03))
    tracker.update(7.4, [])
    assert tracker.tracks == []


def test_track_coasts_no_longer_than_it_was_seen():
    tracker = tracking.Tracker()
    _run(tracker, 6, [(-120.0, 7.03, -10.0, 0.0)])  # seen for 1.0 s, until 1.2 s
    tracker.update(2.2, [])
    assert len(tracker.tracks) == 1
    tracker.update(2.4, [])
    assert tracker.tracks == []


def test_track_seen_again_while_coasting_keeps_its_id_and_ids_are_not_reused():
    tracker = tracking.Tracker()
    _run(tracker, 6, [(-120.0, 7.03, -10.0, 0.0)])
    tracker.update(2.0, [_det(2.0, -138.0, 7.03)])  # 0.8 s gap, where it was predicted
    assert [tr.track_id for tr in tracker.tracks] == ["TRACK_001"]
    tracker.update(5.2, [])  # expired
    _run(tracker, 3, [(-174.0, 7.03, -10.0, 0.0)], start=5.4)
    assert [tr.track_id for tr in tracker.tracks] == ["TRACK_002"]


def test_detection_outside_the_gate_starts_a_new_track():
    tracker = tracking.Tracker()
    _run(tracker, 6, [(-120.0, 7.03, -10.0, 0.0)])  # predicted at x = -132 at 1.4 s
    tracker.update(1.4, [_det(1.4, -132.0, 10.87)])  # 3.84 m away: the next lane's car, not this one
    assert [(tr.track_id, tr.last_seen) for tr in tracker.tracks] == [("TRACK_001", 1.2), (None, 1.4)]


def test_track_keeps_its_latest_range_rate_and_source():
    tracker = tracking.Tracker()
    tracker.update(0.2, [_det(0.2, -120.0, 7.03, range_rate=-8.3)])
    tracker.update(0.4, [_det(0.4, -122.0, 7.03, range_rate=-8.1, source="radar_rear")])
    (track,) = tracker.tracks
    assert (track.range_rate, track.source) == (-8.1, "radar_rear")


# --- report


def test_track_report_is_ego_centric():
    tracker = tracking.Tracker()
    for k in range(6):  # 0.2 .. 1.2 s, a car in the left lane at 13.9 m/s, last seen at x = -123.9
        t = round(0.2 + 0.2 * k, 3)
        tracker.update(t, [_det(t, -110.0 - 13.9 * 0.2 * k, 3.125, range_rate=-8.2)])
    report = perception.track_report(tracker.confirmed[0], 1.2, HIGHWAY_POSE, (-22.2, 0.0))
    assert report == {
        "track_id": "TRACK_001",
        "object_type": "vehicle",
        "timestamp": 1.2,
        "relative_position": {"longitudinal_m": 23.9, "lateral_m": 3.75},
        "distance_m": 24.19,
        "bearing_deg": 8.9,
        "relative_velocity": {"longitudinal_mps": -8.3, "lateral_mps": 0.0},  # ego 8.3 m/s faster
        "relative_speed_mps": -8.2,
        "lane_relation": "left_lane",
        "source": "radar_front",
        "age_s": 1.0,
        "last_seen": 1.2,
    }


def test_summary_lists_tracks_nearest_first():
    def report(track_id, lon, lat, vel, range_rate, lane, last_seen):
        return {"track_id": track_id, "relative_position": {"longitudinal_m": lon, "lateral_m": lat},
                "distance_m": math.hypot(lon, lat), "relative_velocity": vel, "relative_speed_mps": range_rate,
                "lane_relation": lane, "source": "radar_rear" if lon < 0 else "radar_front", "age_s": 2.4,
                "last_seen": last_seen}

    reports = [report("TRACK_001", 42.3, 0.1, {"longitudinal_mps": -3.2, "lateral_mps": 0.0}, -3.21, "ego_lane", 24.0),
               report("TRACK_002", -18.6, 3.7, {"longitudinal_mps": 8.3, "lateral_mps": -0.04}, None, "left_lane",
                      23.6)]
    assert perception.format_summary(24.0, reports) == "\n".join([
        "[RoadSense:PERCEPTION] t=24.0s tracks=2 nearest=TRACK_002@19.0m",
        "  TRACK_002 behind  19.0m lon=-18.6 lat=+3.7 rel_vel=(+8.3,-0.0)m/s rel_speed=n/a left_lane radar_rear age=2.4s"
        " unseen=0.4s",
        "  TRACK_001 ahead   42.3m lon=+42.3 lat=+0.1 rel_vel=(-3.2,+0.0)m/s rel_speed=-3.2m/s ego_lane"
        " radar_front age=2.4s",
    ])
    assert perception.format_summary(3.0, []) == "[RoadSense:PERCEPTION] t=3.0s tracks=0 nearest=none"
