"""Phase 7A.3 P1: range-rate track initiation (opt-in) against a simulated RoadSense radar pair.

The simulated radar inverts perception.radar_detections: Webots' (distance, azimuth positive to the radar's right,
range rate) of each target's ego-frame position, with the world file's noise when seeded. Criterion T1 of
docs/phase7a3-plan.md: a constant-velocity target at any world speed 0-40 m/s and lateral speed 0-2 m/s confirms
within 0.6 s of its first detection.
"""
import math
import random

import pytest

from roadsense.autonomy import perception, pipeline, tracking

LIMITS = {"radar front": (1.0, 150.0, 2.6), "radar rear": (1.0, 100.0, 2.6)}
NOISE = {"range": 0.25, "azimuth": 0.001, "speed": 0.12}  # roadsense_highway.wbt


@pytest.fixture(autouse=True)
def lht_road():
    perception.use_road("highway_lht")  # carriageway heading +x
    yield
    perception.use_road("highway")


def radar_targets(ego, ego_v, targets, rng=None, first_speed=None):
    """{radar name: [(distance, azimuth, range rate)]} for targets [(id, (x, y), (vx, vy))] in the world frame."""
    out = {name: [] for name in LIMITS}
    for tid, (x, y), (vx, vy) in targets:
        lon, lat = perception.world_to_ego(x, y, ego)
        rel_lon, rel_lat = perception.world_to_ego(vx - ego_v[0], vy - ego_v[1], (0, 0, ego[2]))
        for name, (_source, mx, my, myaw) in perception.RADARS.items():
            dx, dy = lon - mx, lat - my
            distance = math.hypot(dx, dy)
            azimuth = (myaw - math.atan2(dy, dx) + math.pi) % (2 * math.pi) - math.pi
            low, high, fov = LIMITS[name]
            if not low < distance < high or abs(azimuth) > fov / 2 - perception.EDGE_MARGIN_RAD:
                continue
            rate = (rel_lon * dx + rel_lat * dy) / distance
            if first_speed is not None and tid in first_speed:
                rate = first_speed.pop(tid)  # what Webots reports on a target's first refresh, e.g. 0
            if rng is not None:
                distance += rng.gauss(0, NOISE["range"])
                azimuth += rng.gauss(0, NOISE["azimuth"])
                rate += rng.gauss(0, NOISE["speed"])
            out[name].append((distance, azimuth, rate))
            break
    return out


def drive(initiation, ego_speed, targets, seconds=2.0, noisy=False, first_speed=None, seed=7):
    """Run a tracker over constant-velocity targets [(id, (x0, y0), (vx, vy))]; the ego drives +x in lane y=6.88.
    Returns {target id: seconds from its first detection to a confirmed track within 2 m of it} and the tracker."""
    rng = random.Random(seed) if noisy else None
    tracker = tracking.Tracker(initiation)
    first, confirmed = {}, {}
    for k in range(1, round(seconds / 0.2) + 1):
        t = round(0.2 * k, 3)
        ego, ego_v = (ego_speed * t, 6.88, 0.0), (ego_speed, 0.0)
        now = [(tid, (x + vx * t, y + vy * t), (vx, vy)) for tid, (x, y), (vx, vy) in targets]
        detections = []
        for name, data in radar_targets(ego, ego_v, now, rng, first_speed).items():
            detections += perception.radar_detections(name, data, LIMITS[name], t, ego)
        for tid, pos, _ in now:
            if any(math.hypot(d.world_x - pos[0], d.world_y - pos[1]) < 2 for d in detections):
                first.setdefault(tid, t)
        tracker.update(t, detections, pose=ego, ego_velocity=ego_v)
        for tid, pos, _ in now:
            if tid not in confirmed and any(math.hypot(tr.history[-1][1] - pos[0], tr.history[-1][2] - pos[1]) < 2
                                            for tr in tracker.confirmed):
                confirmed[tid] = round(t - first[tid], 2)
    return confirmed, tracker


SPEEDS = [0.0, 5.0, 10.0, 15.0, 20.0, 24.0, 26.0, 30.0, 35.0, 40.0]


@pytest.mark.parametrize("noisy", [False, True])
@pytest.mark.parametrize("ego_speed", [11.0, 22.0])
@pytest.mark.parametrize("speed", SPEEDS)
def test_t1_every_world_speed_confirms_within_0_6_s(speed, ego_speed, noisy):
    # ahead in the ego lane: slower ones approach, faster ones recede
    confirmed, _ = drive("range_rate", ego_speed, [("car", (40.0, 6.88), (speed, 0.0))], noisy=noisy)
    assert confirmed.get("car", math.inf) <= 0.6


@pytest.mark.parametrize("lateral", [0.5, 1.0, 2.0, -2.0])
@pytest.mark.parametrize("speed", [8.0, 30.0, 40.0])
def test_t1_lateral_motion_up_to_2_mps_confirms(speed, lateral):
    confirmed, _ = drive("range_rate", 22.0, [("car", (30.0, 3.13), (speed, lateral))], noisy=True)
    assert confirmed.get("car", math.inf) <= 0.6


def test_the_legacy_tracker_keeps_its_25_mps_limit():
    assert drive("legacy", 22.0, [("car", (40.0, 6.88), (24.0, 0.0))])[0].get("car", math.inf) <= 0.6
    assert "car" not in drive("legacy", 22.0, [("car", (40.0, 6.88), (26.0, 0.0))])[0]


def test_oncoming_and_overtaking_from_behind():
    targets = [("oncoming", (120.0, -3.2), (-30.0, 0.0)),  # the other carriageway, closing at 52 m/s
               ("from_behind", (-40.0, 3.13), (36.0, 0.0))]  # the rear radar, overtaking in the next lane
    confirmed, _ = drive("range_rate", 22.0, targets, noisy=True)
    assert confirmed.get("oncoming", math.inf) <= 0.6 and confirmed.get("from_behind", math.inf) <= 0.6


def test_neighbouring_lanes_stay_two_separate_tracks():
    # side by side 3.84 m apart, both faster than the legacy limit
    targets = [("lane_a", (45.0, 3.13), (32.0, 0.0)), ("lane_b", (45.0, 6.97), (32.0, 0.0))]
    confirmed, tracker = drive("range_rate", 22.0, targets, seconds=3.0, noisy=True)
    assert set(confirmed) == {"lane_a", "lane_b"}
    lanes = sorted(round(tr.history[-1][2]) for tr in tracker.confirmed)
    assert lanes == [3, 7]  # one track per lane, neither swapped across
    for tr in tracker.confirmed:  # each track stayed in its own lane throughout
        assert max(y for _, _, y in tr.history) - min(y for _, _, y in tr.history) < 1.0


def test_crossing_trajectories():
    targets = [("crossing", (35.0, 14.0), (0.0, -1.5)),  # crossing the carriageway on foot
               ("lane_change", (30.0, 10.8), (30.0, -2.5))]  # a fast lane change toward the ego lane
    confirmed, _ = drive("range_rate", 15.0, targets, seconds=2.4, noisy=True)
    assert confirmed.get("crossing", math.inf) <= 0.6
    assert confirmed.get("lane_change", math.inf) <= 0.8  # 2.5 m/s across is beyond T1's 2 m/s


@pytest.mark.parametrize("first", [0.0, 70.0, -45.0])
def test_a_wrong_first_range_rate_only_delays_confirmation_by_a_cycle(first):
    # Webots computes the range rate from two refreshes: a target's first one is often far off (Phase 7A.3 captures)
    confirmed, _ = drive("range_rate", 22.0, [("car", (40.0, 6.88), (35.0, 0.0))], noisy=True,
                         first_speed={"car": first})
    assert confirmed.get("car", math.inf) <= 0.8


def test_range_rate_is_only_the_radial_component():
    # a vehicle ahead-left: one range rate pins the velocity along the line of sight, not across it
    d = perception.Detection(0.2, "radar_front", 6.0, 3.7, 7.1, 31.7, -10.0, 6.0, 10.58)
    track = tracking.Track(d)
    track.radial = tracking.radial_measurement(d, (0.0, 6.88, 0.0), (20.0, 0.0))
    (ux, uy), z = track.radial
    (vx, vy), cov = tracking.initial_velocity(track)
    along_los = ux * (cov[0][0] * ux + cov[0][1] * uy) + uy * (cov[1][0] * ux + cov[1][1] * uy)
    across_los = uy * (cov[0][0] * uy - cov[0][1] * ux) - ux * (cov[1][0] * uy - cov[1][1] * ux)
    assert along_los < tracking.RANGE_RATE_SIGMA_MPS ** 2 and across_los > 25 * along_los
    assert abs(vx * ux + vy * uy - z) < 0.1  # the estimate honours the measured radial speed


def test_the_default_and_the_pipeline_stay_legacy():
    assert tracking.Tracker().initiation == "legacy"
    assert pipeline.Pipeline("EGO").tracker.initiation == "legacy"
    with pytest.raises(ValueError):
        tracking.Tracker("kalman")
