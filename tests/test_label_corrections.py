"""Phase 7A.3 Stage 2: initialization-frame speed correction (P7) and cut-in staging v2."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import cutin_staging  # noqa: E402
import scenario_dataset as ds  # noqa: E402

EGO = {"x": 0.0, "y": 6.88, "yaw": 0.0, "speed": 15.0}


def vehicle(vid, x, speed, y=6.88):  # ground-truth x is the front bumper
    return {"id": vid, "type": "car", "x": x, "y": y, "yaw": 0.0, "speed": speed, "length": 4.5, "width": 1.8}


def frame(t, vehicles, logical=None, ego_x=0.0):
    sumo = [dict(v, speed=logical.get(v["id"], v["speed"])) for v in vehicles] if logical else vehicles
    return {"t": t, "ego": dict(EGO, x=ego_x), "vehicles": vehicles, "sumo_vehicles": sumo}


def test_an_inserted_vehicle_moving_at_ego_speed_gets_its_speed_back():
    frames = [frame(10.0, []), frame(10.2, [vehicle("rs_lead", 22.5, 0.0)], {"rs_lead": 15.0}, 3.0),
              frame(10.4, [vehicle("rs_lead", 25.5, 15.0)], ego_x=6.0)]
    corrected, fixes = ds.correct_initialization(frames)
    assert [f["vehicle"] for f in fixes] == ["rs_lead"] and corrected[1]["vehicles"][0]["speed"] == 15.0
    assert frames[1]["vehicles"][0]["speed"] == 0.0  # the original is never modified
    # the one-frame "near miss" (TTC 14.6 m / 15 m/s < 1.5 s) was the artifact; the corrected frame has none
    assert ds.outcomes(frames[1])["forward_near_miss"] and not ds.outcomes(corrected[1])["forward_near_miss"]


def test_stationary_and_slow_vehicles_and_later_frames_are_untouched():
    stopped = [frame(0.2, [vehicle("v1", 30.0, 0.0)], {"v1": 0.0}), frame(0.4, [vehicle("v1", 30.0, 0.0)])]
    assert ds.correct_initialization(stopped)[1] == []
    disputed = [frame(0.2, [vehicle("v2", 30.0, 0.0)], {"v2": 0.0}), frame(0.4, [vehicle("v2", 33.0, 0.0)])]
    assert ds.correct_initialization(disputed)[1] == []  # SUMO says it stands: two sources must agree
    later = [frame(0.2, [vehicle("v3", 30.0, 15.0)]), frame(0.4, [vehicle("v3", 33.0, 0.0)], {"v3": 15.0}),
             frame(0.6, [vehicle("v3", 36.0, 15.0)])]
    assert ds.correct_initialization(later)[1] == []  # only a vehicle's first frame


def test_a_genuine_near_miss_with_a_stopped_vehicle_is_kept():
    frames = [frame(0.2, [vehicle("v4", 18.0, 0.0)], {"v4": 0.0}), frame(0.4, [vehicle("v4", 18.0, 0.0)], ego_x=3.0)]
    corrected, fixes = ds.correct_initialization(frames)
    assert fixes == [] and ds.outcomes(corrected[0])["forward_near_miss"]


def test_cut_in_staging_v2_spans_realistic_warning_times():
    gradual = [cutin_staging.draw("gradual", s)["event"] for s in range(1, 60)]
    sudden = [cutin_staging.draw("sudden", s)["event"] for s in range(1, 60)]
    assert min(e["staging"]["design_warning_s"] for e in gradual) >= 3.5
    assert max(e["staging"]["design_warning_s"] for e in sudden) <= 2.0
    assert all(e["staging"]["stress_only"] for e in sudden) and not any(e["staging"]["stress_only"] for e in gradual)
    assert all(e["staging"]["act_gap_m"] <= e["gap_m"] <= cutin_staging.SPAWN_MAX_M for e in gradual + sudden)
    assert all(e["settle_s"] == e["staging"]["observe_s"] for e in gradual)  # the plugin's observation period
    assert cutin_staging.draw("assertive", 3) == cutin_staging.draw("assertive", 3)  # reproducible


def test_lateral_entry_follows_the_supervisor_profile():
    slow, fast = cutin_staging.lateral_entry_s(5.0, 1.8), cutin_staging.lateral_entry_s(1.0, 1.8)
    assert fast < slow and fast >= (3.75 / 2 - 0.9) / cutin_staging.MAX_LATERAL_MPS  # the 2.5 m/s cap binds
