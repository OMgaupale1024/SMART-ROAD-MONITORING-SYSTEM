"""Phase 7A.3 perception audit helpers (tools/perception_audit.py) and the tracker limit it found."""
import sys
from pathlib import Path

from roadsense.autonomy import perception, tracking

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import perception_audit as pa  # noqa: E402

EGO = {"x": 0.0, "y": 6.88, "yaw": 0.0}


def vehicle(vid, lon, lat, length=4.5, width=1.8):
    return {"id": vid, "centre": (lon, EGO["y"] + lat), "yaw": 0.0, "length": length, "width": width}


def test_the_radar_model_has_a_blind_zone_alongside_and_sees_ahead_and_behind():
    seen = pa.visibility(EGO, [vehicle("ahead", 30, 0), vehicle("behind", -30, 0), vehicle("alongside", 1.5, -3.75)])
    assert seen["ahead"] == "radar front" and seen["behind"] == "radar rear"
    assert seen["alongside"] == "not_visible:fov"  # between the front and rear radars' fields of view


def test_a_nearer_vehicle_occludes_the_one_behind_it():
    seen = pa.visibility(EGO, [vehicle("near", 20, 0), vehicle("far", 50, 0)])
    assert seen["near"] == "radar front" and seen["far"] == "not_visible:occluded"


def frame(t, vehicles, ego_speed=15.0):
    return {"t": t, "ego": {"x": 0.0, "y": 6.88, "yaw": 0.0, "speed": ego_speed}, "vehicles": vehicles}


def gt_vehicle(vid, x, speed):  # ground-truth vehicles carry their front-bumper x
    return {"id": vid, "type": "car", "x": x, "y": 6.88, "yaw": 0.0, "speed": speed, "length": 4.5, "width": 1.8}


def test_a_near_miss_made_only_by_an_inserted_vehicles_zero_first_speed_is_an_artifact():
    # inserted 18 m ahead at the ego's speed, but its first recorded speed is 0: TTC 14.6/15 < 1.5 s, once
    frames = [frame(10.0, []), frame(10.2, [gt_vehicle("rs_lead", 22.5, 0.0)]),
              frame(10.4, [gt_vehicle("rs_lead", 25.5, 15.0)])]
    assert pa.insertion_artifacts(frames) == [(10.2, "rs_lead")]
    # an ordinary vehicle, or a staged one whose near miss continues, is not
    frames[2]["vehicles"][0].update(x=6.0, speed=0.0)
    assert pa.insertion_artifacts(frames) == []


def test_unconfirmed_tracks_of_vehicles_faster_than_25_mps_never_confirm():
    """Documents the Phase 7A.3 finding (a new track is predicted not to move, within a 5 m gate); the proposed
    tracker change must turn this into a passing test for every speed up to 40 m/s."""
    def confirmed(speed):
        tr = tracking.Tracker()
        for k in range(25):
            t = round(0.2 * k, 3)
            tr.update(t, [perception.Detection(t, "radar_front", 0, 0, 0, 0, None, speed * t, 7.0)])
        return len(tr.confirmed)
    assert confirmed(24.9) == 1
    assert confirmed(25.1) == 0


def test_a_miss_rate_is_missed_over_observable_frames():
    import collections
    seen, missed = {"speed": collections.Counter({"25-30 m/s": 16})}, {"speed": collections.Counter({"25-30 m/s": 14})}
    assert pa.miss_rates(missed, seen)["speed"]["25-30 m/s"] == {"missed": 14, "observable_frames": 16,
                                                                 "miss_rate": 0.875}
