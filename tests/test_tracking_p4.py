"""Phase 7A.3 P4: the opt-in track lifecycle, Tracker(..., lifecycle="p4"), on test_tracking_p1's simulated radar pair.
The tests only place targets and hide their returns; no truth reaches a tracker."""
import math

import pytest

import test_tracking_p1 as t1
from roadsense.autonomy import perception, tracking

C = {"initiation": "range_rate", "estimator": "kalman"}  # configuration C, P1+P2
D = dict(C, lifecycle="p4")  # configuration D, P1+P2+P4
LEGACY_P4 = {"lifecycle": "p4"}  # the ablation: P4 on the legacy tracker


@pytest.fixture(autouse=True)
def lht_road():
    perception.use_road("highway_lht")  # carriageway heading +x
    yield
    perception.use_road("highway")


def run(config, ego_speed, targets, seconds, hidden=lambda tid, t: False):
    """The ego drives +x at ego_speed in lane y=6.88 past constant-velocity targets [(id, (x0, y0), (vx, vy))];
    hidden(id, t) drops that target's returns. {t: ({track id: (zone, position, velocity)}, [(id, reason)])}."""
    tracker, out = tracking.Tracker(**config), {}
    for k in range(1, round(seconds / 0.2) + 1):
        t = round(0.2 * k, 3)
        ego, ego_v = (ego_speed * t, 6.88, 0.0), (ego_speed, 0.0)
        now = [(tid, (x + vx * t, y + vy * t), (vx, vy)) for tid, (x, y), (vx, vy) in targets if not hidden(tid, t)]
        detections = []
        for name, data in t1.radar_targets(ego, ego_v, now).items():
            detections += perception.radar_detections(name, data, t1.LIMITS[name], t, ego)
        tracker.update(t, detections, pose=ego, ego_velocity=ego_v)
        out[t] = ({tr.track_id: (getattr(tr, "zone", None), tr.position_at(t), tr.velocity) for tr in tracker.confirmed},
                  [(tr.track_id, why) for tr, why in tracker.expired])
    return out


def without_p4(config):
    return {k: v for k, v in config.items() if k != "lifecycle"}


def test_the_defaults_stay_legacy():
    assert tracking.Tracker().lifecycle == "legacy" and tracking.Tracker("range_rate", "kalman").lifecycle == "legacy"
    with pytest.raises(ValueError):
        tracking.Tracker(lifecycle="p3")


@pytest.fixture
def configuration_d(monkeypatch):
    original = tracking.Tracker
    monkeypatch.setattr(tracking, "Tracker", lambda initiation: original(initiation, "kalman", "p4"))


@pytest.mark.parametrize("noisy", [False, True])
@pytest.mark.parametrize("ego", [11.0, 22.0])
@pytest.mark.parametrize("speed", t1.SPEEDS)
def test_t1_every_speed_confirms_within_0_6_s(configuration_d, noisy, ego, speed):
    confirmed, _ = t1.drive("range_rate", ego, [("car", (40.0, 6.88), (speed, 0.0))], noisy=noisy)
    assert confirmed.get("car", math.inf) <= 0.6 + 1e-9


def test_p1_and_p2_cases_through_configuration_d(configuration_d):
    t1.test_oncoming_and_overtaking_from_behind()
    t1.test_neighbouring_lanes_stay_two_separate_tracks()
    t1.test_crossing_trajectories()
    for lateral in (0.5, -2.0):
        t1.test_t1_lateral_motion_up_to_2_mps_confirms(30.0, lateral)


@pytest.mark.parametrize("config", [D, LEGACY_P4])
def test_a_track_past_the_radars_reach_expires_at_once(config):
    target = [("car", (120.0, 6.88), (22.0, 0.0))]  # pulling away at 10 m/s: past 150 m from the radar after 3.4 s
    p4, legacy = run(config, 12.0, target, 5.0), run(without_p4(config), 12.0, target, 5.0)
    (tid,) = p4[3.4][0]
    assert p4[3.6][1] == [(tid, "beyond sensor range")] and not p4[3.6][0]
    assert tid in legacy[4.4][0]  # the legacy rule coasts it on, out of reach


@pytest.mark.parametrize("config", [D, LEGACY_P4])
def test_a_vehicle_gone_from_clear_view_expires_after_three_misses(config):
    target = [("car", (40.0, 6.88), (20.0, 0.0))]
    gone = lambda tid, t: t >= 4.0 - 1e-9  # noqa: E731
    p4, legacy = run(config, 20.0, target, 7.0, gone), run(without_p4(config), 20.0, target, 7.0, gone)
    (tid,) = p4[3.8][0]
    assert p4[4.2][0][tid][0] == "visible" and p4[4.4][1] == [(tid, "missed while visible")]
    assert tid in legacy[6.6][0]


def test_a_vehicle_hidden_behind_a_nearer_one_coasts_on():
    targets = [("near", (20.0, 6.88), (20.0, 0.0)), ("far", (50.0, 6.88), (20.0, 0.0))]
    hidden = lambda tid, t: tid == "far" and t >= 2.0 - 1e-9  # noqa: E731
    p4, legacy = run(D, 20.0, targets, 7.0, hidden), run(C, 20.0, targets, 7.0, hidden)
    (far,) = [k for k, (_, (x, _y), _) in p4[1.8][0].items() if x - 20.0 * 1.8 > 35]
    assert p4[5.8][0][far][0] == "occluded" and (far, "coast limit") in p4[6.0][1]
    assert far not in legacy[4.0][0]


@pytest.mark.parametrize("config", [D, LEGACY_P4])
def test_a_stopped_vehicle_lost_close_ahead_is_held(config):
    target = [("car", (6.0, 6.88), (0.0, 0.0))]  # 1.95 m from the front radar, where it is often missed
    lost = lambda tid, t: t >= 1.0 - 1e-9  # noqa: E731
    p4, legacy = run(config, 0.0, target, 12.0, lost), run(without_p4(config), 0.0, target, 12.0, lost)
    (tid,) = p4[0.8][0]
    zone, (x, y), v = p4[12.0][0][tid]
    assert zone == "near" and math.hypot(x - 6.0, y - 6.88) < 0.2 and v == (0.0, 0.0)
    assert not legacy[5.0][0]


def test_an_overtaking_vehicle_keeps_its_track_through_the_blind_zone():
    target = [("car", (-30.0, 10.87), (25.0, 0.0))]  # 5 m/s faster in the next lane: blind from about 5.6 to 7.0 s
    p4 = run(D, 20.0, target, 10.0)
    (before,) = p4[5.4][0]
    assert p4[6.4][0][before][0] == "blind" and set(p4[8.0][0]) == {before}


def test_an_unseen_lane_change_decays_while_the_c_estimate_runs_on():
    target = [("car", (-30.0, 12.0), (25.0, -0.3))]
    p4, c = run(D, 20.0, target, 6.6), run(C, 20.0, target, 6.6)
    (tid,) = p4[5.4][0]
    assert p4[6.6][0][tid][0] == "blind"
    assert abs(p4[6.6][0][tid][2][1]) < 0.6 * 0.3 < abs(c[6.6][0][tid][2][1])


def test_a_coasting_duplicate_beside_a_measured_track_expires():
    targets = [("front", (42.0, 6.88), (20.0, 0.0)), ("rear", (40.0, 6.88), (20.0, 0.0))]  # two returns, one car
    hidden = lambda tid, t: tid == "rear" and t >= 3.0 - 1e-9  # noqa: E731
    p4, legacy = run(D, 20.0, targets, 5.0, hidden), run(C, 20.0, targets, 5.0, hidden)
    (rear,) = [k for k, (_, (x, _y), _) in p4[2.8][0].items() if x - 20.0 * 2.8 < 41]
    assert p4[3.0][1] == [(rear, "duplicate of a measured track")] and len(p4[3.0][0]) == 1
    assert len(legacy[4.0][0]) == 2
