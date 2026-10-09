"""EGO_ROADSENSE prediction and collision risk (Webots digital twin): trajectories, CPA, TTC, risk levels, console."""
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


prediction = _load("prediction")
risk = _load("risk")

# Where a vehicle's centre enters EGO_ROADSENSE's safety envelope, in the ego frame (origin at the rear axle, x ahead,
# y left): ahead or behind when the bumpers come within the safety buffer, alongside when the sides do.
AHEAD = risk.EGO_CENTRE_M + risk.EGO_HALF_LENGTH_M + risk.TARGET_HALF_LENGTH_M + risk.SAFETY_BUFFER_M
BEHIND = risk.EGO_CENTRE_M - risk.EGO_HALF_LENGTH_M - risk.TARGET_HALF_LENGTH_M - risk.SAFETY_BUFFER_M
SIDE = risk.EGO_HALF_WIDTH_M + risk.TARGET_HALF_WIDTH_M + risk.SAFETY_BUFFER_M


def _report(lon, lat, v_lon, v_lat, age=5.0, track_id="TRACK_001", lane="ego_lane"):
    """A perception.track_report record: a vehicle's position and velocity relative to EGO_ROADSENSE."""
    return {
        "track_id": track_id,
        "object_type": "vehicle",
        "timestamp": 42.0,
        "relative_position": {"longitudinal_m": lon, "lateral_m": lat},
        "distance_m": round(math.hypot(lon, lat), 2),
        "bearing_deg": round(math.degrees(math.atan2(lat, lon)), 1),
        "relative_velocity": {"longitudinal_mps": v_lon, "lateral_mps": v_lat},
        "relative_speed_mps": None,
        "lane_relation": lane,
        "source": "radar_front" if lon >= 0 else "radar_rear",
        "age_s": age,
        "last_seen": 42.0,
    }


def _assess(*args, **kwargs):
    report = _report(*args, **kwargs)
    return risk.assess(report, prediction.predict(report))


# --- prediction


def test_constant_velocity_trajectory_moves_the_relative_position_on_at_the_relative_velocity():
    result = prediction.predict(_report(30.0, 0.5, -6.0, 0.2, track_id="TRACK_014"))
    assert (result["track_id"], result["model"]) == ("TRACK_014", "constant_velocity")
    points = {p["t_s"]: (p["longitudinal_m"], p["lateral_m"]) for p in result["trajectory"]}
    assert {0.5, 1.0, 1.5, 2.0, 2.5, 3.0} <= set(points)
    assert [p["t_s"] for p in result["trajectory"]] == sorted(points)
    assert points[0.5] == pytest.approx((27.0, 0.6))
    assert points[1.0] == pytest.approx((24.0, 0.7))
    assert points[2.0] == pytest.approx((18.0, 0.9))
    assert points[3.0] == pytest.approx((12.0, 1.1))


def test_vehicle_keeping_pace_stays_where_it_is_and_is_safe():
    a = _assess(20.0, 0.0, 0.0, 0.0)
    assert {(p["longitudinal_m"], p["lateral_m"]) for p in a["trajectory"]} == {(20.0, 0.0)}
    assert (a["closing"], a["conflict"], a["ttc_s"], a["risk"]) == (False, False, None, "SAFE")
    assert (a["time_to_cpa_s"], a["min_separation_m"]) == pytest.approx((0.0, 20.0 - risk.EGO_CENTRE_M))


# --- conflicts and TTC


def test_same_lane_ttc_is_the_bumper_gap_less_the_buffer_over_the_closing_speed():
    a = _assess(30.0, 0.3, -6.0, 0.0)  # EGO_ROADSENSE 6 m/s faster than the car ahead in its lane
    gap = 30.0 - (risk.EGO_CENTRE_M + risk.EGO_HALF_LENGTH_M) - risk.TARGET_HALF_LENGTH_M
    assert a["ttc_s"] == pytest.approx((gap - risk.SAFETY_BUFFER_M) / 6.0, abs=0.01)
    assert (a["closing"], a["conflict"], a["prediction_model"]) == (True, True, "constant_velocity")
    # the centres would line up 0.3 m apart
    assert (a["time_to_cpa_s"], a["min_separation_m"]) == pytest.approx(((30.0 - risk.EGO_CENTRE_M) / 6.0, 0.3),
                                                                        abs=0.01)


def test_vehicle_closing_from_behind_in_the_lane_gets_a_ttc():
    a = _assess(-15.0, 0.0, 5.0, 0.0)
    assert a["ttc_s"] == pytest.approx((BEHIND + 15.0) / 5.0, abs=0.01)
    assert (a["closing"], a["conflict"]) == (True, True)


@pytest.mark.parametrize("lon, v_lon", [(30.0, 4.0), (-12.0, -4.0)])  # pulling away ahead, falling back behind
def test_receding_vehicle_gets_no_ttc(lon, v_lon):
    a = _assess(lon, 0.0, v_lon, 0.0)
    assert (a["closing"], a["conflict"], a["ttc_s"], a["risk"]) == (False, False, None, "SAFE")
    assert (a["time_to_cpa_s"], a["min_separation_m"]) == pytest.approx((0.0, abs(lon - risk.EGO_CENTRE_M)))


def test_vehicle_alongside_keeping_pace_gets_no_ttc():
    a = _assess(1.0, -3.75, 0.0, 0.0, lane="right_lane")
    assert (a["closing"], a["conflict"], a["ttc_s"], a["risk"]) == (False, False, None, "SAFE")


@pytest.mark.parametrize("lon, lat, v_lon, v_lat", [
    (20.0, 3.75, -8.0, 0.0),  # overtaken in the next lane
    (12.0, -6.0, -8.0, 1.0),  # drifting over from two lanes away, but past EGO_ROADSENSE before it gets there
    (10.0, 2.9, -36.0, -2.0),  # oncoming and drifting over, past before it reaches EGO_ROADSENSE's side
])
def test_vehicle_closing_in_on_a_path_that_misses_gets_no_ttc(lon, lat, v_lon, v_lat):
    a = _assess(lon, lat, v_lon, v_lat, lane="other")
    assert (a["closing"], a["conflict"], a["ttc_s"], a["risk"]) == (True, False, None, "SAFE")
    assert a["min_separation_m"] > SIDE


def test_vehicle_overtaken_in_the_next_lane_is_closest_alongside():
    a = _assess(20.0, 3.75, -8.0, 0.0, lane="left_lane")
    assert (a["time_to_cpa_s"], a["min_separation_m"]) == pytest.approx(((20.0 - risk.EGO_CENTRE_M) / 8.0, 3.75),
                                                                        abs=0.01)


def test_vehicle_merging_from_the_next_lane_conflicts_without_closing_longitudinally():
    a = _assess(4.0, -3.75, 0.0, 1.0, lane="right_lane")  # alongside, moving over at 1 m/s
    assert a["ttc_s"] == pytest.approx((3.75 - SIDE) / 1.0, abs=0.01)
    assert (a["closing"], a["conflict"]) == (True, True)


def test_crossing_vehicle_conflicts_once_inside_on_both_axes():
    a = _assess(20.0, -6.0, -8.0, 2.0, lane="other")
    # inside longitudinally from (20 - AHEAD) / 8 s, laterally from (6 - SIDE) / 2 s, whichever is later
    assert a["ttc_s"] == pytest.approx(max((20.0 - AHEAD) / 8.0, (6.0 - SIDE) / 2.0), abs=0.01)


def test_cpa_is_the_closest_approach_of_the_centres_within_the_horizon():
    a = _assess(20.0, -6.0, -8.0, 2.0, lane="other")
    times = [k / 1000 for k in range(int(prediction.HORIZONS_S[-1] * 1000) + 1)]
    separation, t = min((math.hypot(20.0 - 8.0 * t - risk.EGO_CENTRE_M, -6.0 + 2.0 * t), t) for t in times)
    assert (a["time_to_cpa_s"], a["min_separation_m"]) == pytest.approx((t, separation), abs=0.01)


def test_closest_approach_beyond_the_horizon_is_at_the_horizon():
    a = _assess(60.0, 0.0, -5.0, 0.0)  # a slower car far ahead: it would take 11.7 s for the centres to line up
    horizon = prediction.HORIZONS_S[-1]
    assert (a["conflict"], a["ttc_s"], a["risk"]) == (False, None, "SAFE")
    assert (a["time_to_cpa_s"], a["min_separation_m"]) == pytest.approx(
        (horizon, 60.0 - 5.0 * horizon - risk.EGO_CENTRE_M), abs=0.01)


def test_conflict_follows_the_predicted_trajectory_whatever_model_made_it():
    report = _report(4.0, -3.75, 0.0, 0.0, lane="right_lane")  # alongside, keeping pace now...
    turning_in = {"track_id": "TRACK_001", "model": "test_lane_change", "trajectory": [
        {"t_s": 1.0, "longitudinal_m": 4.0, "lateral_m": -3.75},
        {"t_s": 2.0, "longitudinal_m": 4.0, "lateral_m": 0.0},  # ...then moving over between 1 and 2 s
    ]}
    a = risk.assess(report, turning_in)
    assert a["ttc_s"] == pytest.approx(1.0 + (3.75 - SIDE) / 3.75, abs=0.01)
    assert (a["prediction_model"], a["trajectory"]) == ("test_lane_change", turning_in["trajectory"])


# --- risk levels


@pytest.mark.parametrize("ttc, level", [
    (risk.CRITICAL_TTC_S - 0.2, "CRITICAL"),
    (risk.CRITICAL_TTC_S + 0.2, "HIGH"),
    (risk.HIGH_TTC_S - 0.2, "HIGH"),
    (risk.HIGH_TTC_S + 0.2, "CAUTION"),
    (prediction.HORIZONS_S[-1] - 0.2, "CAUTION"),
    (prediction.HORIZONS_S[-1] + 0.5, "SAFE"),  # beyond the prediction horizon: no conflict predicted
])
def test_risk_level_follows_the_time_to_conflict(ttc, level):
    a = _assess(AHEAD + 5.0 * ttc, 0.0, -5.0, 0.0)  # closing at 5 m/s on the car ahead
    assert (a["risk"], a["conflict"]) == (level, level != "SAFE")


@pytest.mark.parametrize("v_lon, v_lat", [(0.0, 0.0), (-1.0, 0.2), (1.0, -0.2)])  # keeping pace, closing, pulling away
def test_vehicle_already_inside_the_envelope_is_critical(v_lon, v_lat):
    a = _assess(AHEAD - 0.1, 0.5, v_lon, v_lat)  # bumpers closer than the buffer
    assert (a["ttc_s"], a["conflict"], a["risk"]) == (0.0, True, "CRITICAL")


@pytest.mark.parametrize("age, level", [(0.4, "HIGH"), (risk.MATURE_AGE_S - 0.2, "HIGH"), (risk.MATURE_AGE_S, "CRITICAL")])
def test_track_younger_than_the_maturity_age_is_rated_at_most_high(age, level):
    a = _assess(AHEAD + 5.0, 0.0, -5.0, 0.0, age=age)  # TTC 1 s
    assert a["ttc_s"] == pytest.approx(1.0, abs=0.01)
    assert (a["conflict"], a["risk"]) == (True, level)


@pytest.mark.parametrize("position, velocity", [
    ({"longitudinal_m": 12.0, "lateral_m": 0.0}, None),  # no velocity
    ({"longitudinal_m": 12.0, "lateral_m": 0.0}, {"longitudinal_mps": float("nan"), "lateral_mps": 0.0}),
    ({"longitudinal_m": float("inf"), "lateral_m": 0.0}, {"longitudinal_mps": -5.0, "lateral_mps": 0.0}),
    ({"longitudinal_m": 12.0, "lateral_m": None}, {"longitudinal_mps": -5.0, "lateral_mps": 0.0}),
])
def test_track_without_a_finite_position_and_velocity_gets_no_prediction_or_rating(position, velocity):
    report = dict(_report(0.0, 0.0, 0.0, 0.0), relative_position=position, relative_velocity=velocity)
    result = prediction.predict(report)
    assert result["trajectory"] == []
    a = risk.assess(report, result)
    assert {k: a[k] for k in ("ttc_s", "time_to_cpa_s", "min_separation_m", "closing", "conflict", "risk")} == {
        "ttc_s": None, "time_to_cpa_s": None, "min_separation_m": None, "closing": None, "conflict": False,
        "risk": "SAFE"}


# --- ego safety state


def test_ego_safety_reports_the_most_critical_track_and_counts_the_conflicts():
    assessments = [
        _assess(60.0, 3.75, -1.0, 0.0, track_id="TRACK_001", lane="left_lane"),  # SAFE
        _assess(AHEAD + 5.0 * 4.0, 0.0, -5.0, 0.0, track_id="TRACK_002"),  # CAUTION, TTC 4 s
        _assess(AHEAD + 5.0 * 2.5, 0.0, -5.0, 0.0, track_id="TRACK_003"),  # HIGH, TTC 2.5 s
        _assess(AHEAD + 5.0 * 1.0, 0.0, -5.0, 0.0, age=0.6, track_id="TRACK_004"),  # young: HIGH, TTC 1 s
    ]
    assert [a["risk"] for a in assessments] == ["SAFE", "CAUTION", "HIGH", "HIGH"]
    assert risk.ego_safety(42.0, assessments) == {
        "timestamp": 42.0,
        "overall_risk": "HIGH",
        "most_critical_track": "TRACK_004",  # HIGH like TRACK_003, but sooner
        "minimum_ttc_s": pytest.approx(1.0, abs=0.01),
        "active_conflicts": 3,
    }


def test_ego_safety_without_conflicts_is_safe():
    assert risk.ego_safety(3.0, []) == {"timestamp": 3.0, "overall_risk": "SAFE", "most_critical_track": None,
                                         "minimum_ttc_s": None, "active_conflicts": 0}
    assert risk.ego_safety(3.0, [_assess(30.0, 0.0, 4.0, 0.0)])["overall_risk"] == "SAFE"


# --- console


def _assessment(track_id, level, ttc, t_cpa, separation):
    return {"track_id": track_id, "prediction_model": "constant_velocity", "ttc_s": ttc, "time_to_cpa_s": t_cpa,
            "min_separation_m": separation, "closing": True, "conflict": ttc is not None, "risk": level,
            "trajectory": []}


def test_safety_summary_lists_the_conflicts_most_critical_first():
    reports = [_report(22.0, 0.1, -3.6, 0.0, track_id="TRACK_001"),
               _report(19.4, -0.2, -6.9, 0.0, track_id="TRACK_014"),
               _report(-12.5, 3.7, -0.4, 0.0, track_id="TRACK_020", lane="left_lane")]
    assessments = [_assessment("TRACK_001", "CAUTION", 4.27, 5.0, 2.56),
                   _assessment("TRACK_014", "HIGH", 1.84, 2.6, 0.2),
                   _assessment("TRACK_020", "SAFE", None, 0.0, 14.0)]
    assert risk.format_summary(42.0, reports, assessments) == "\n".join([
        "[RoadSense:SAFETY] t=42.0s risk=HIGH conflicts=2 min_ttc=1.8s critical=TRACK_014",
        "  TRACK_014 ahead   19.4m ego_lane rel_vel=(-6.9,+0.0)m/s TTC=1.8s CPA=0.2m@2.6s HIGH",
        "  TRACK_001 ahead   22.0m ego_lane rel_vel=(-3.6,+0.0)m/s TTC=4.3s CPA=2.6m@5.0s CAUTION",
    ])
    assert risk.format_summary(3.0, [], []) == (
        "[RoadSense:SAFETY] t=3.0s risk=SAFE conflicts=0 min_ttc=n/a critical=none")
