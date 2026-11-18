"""Experimental multi-sensor perception (shadow mode): transport, radar model, lidar, tracker, fusion."""
import itertools
import json
import math
import random
import struct
from pathlib import Path

import numpy as np
import pytest

from roadsense.autonomy import perception, pipeline
from roadsense.experimental import fusion, lidar, radar, transport
from roadsense.experimental import tracker as trk

ROOT = Path(__file__).resolve().parents[1]
FRONT = radar.RadarSpec("profile radar front", "front", (4.054, 0.0, 0.142, 0.0), 1.0, 150.0, 2.6)
REAR = radar.RadarSpec("profile radar rear", "rear", (-1.16, 0.0, 0.33, math.pi), 1.0, 100.0, 2.6)
MOUNTS = {s.name: (s.mount[0], s.mount[1], s.mount[2], 0.0, 0.0, s.mount[3]) for s in (FRONT, REAR)}


# -- assignment ------------------------------------------------------------------------------------------------

def _brute(cost):
    n_r, n_c = cost.shape
    best = (0, 0.0)
    for k in range(min(n_r, n_c), 0, -1):  # most pairs first, then least cost
        for rows in itertools.combinations(range(n_r), k):
            for cols in itertools.permutations(range(n_c), k):
                if all(cost[r, c] < 1e8 for r, c in zip(rows, cols)):
                    total = sum(cost[r, c] for r, c in zip(rows, cols))
                    if best[0] < k or total < best[1] - 1e-9:
                        best = (k, total)
        if best[0] == k:
            return best
    return best


@pytest.mark.parametrize("seed", range(30))
def test_assignment_is_optimal(seed):
    rng = np.random.default_rng(seed)
    n_r, n_c = rng.integers(1, 5, size=2)
    cost = rng.uniform(0, 10, (n_r, n_c))
    cost[rng.uniform(size=cost.shape) < 0.3] = 1e9  # gated out
    got = trk.assign(cost)
    assert len(set(got.values())) == len(got)
    assert all(cost[r, c] < 1e8 for r, c in got.items())
    k, total = _brute(cost)
    assert len(got) == k
    assert sum(cost[r, c] for r, c in got.items()) == pytest.approx(total)


# -- transport -------------------------------------------------------------------------------------------------

def _cfg(encoding=lidar.UINT16):
    return lidar.Config.webots("roof", (1.11, 0, 1.51, 0, 0, 0), 36, 16, 6.283, 0.5, 100.0,
                               lidar.ROOF16_ELEVATIONS, encoding=encoding, scale=0.01)


def _frame(seq=1, t=10.0, cfg=None, ranges=None, radar_t=None, mount=None):
    cfg = cfg or _cfg()
    ranges = np.full((16, 36), 20.0) if ranges is None else ranges
    scan = transport.RadarScan(FRONT.name, t if radar_t is None else radar_t, transport.OK,
                               mount or MOUNTS[FRONT.name], (1.0, 150.0, 2.6, 0.0785),
                               (transport.RadarReturn(20.0, 0.01, -1.5, -40.0),
                                transport.RadarReturn(5.0, -0.3, float("nan"), None)))
    return transport.encode(seq, t, gnss={"t": t, "position": (1.0, 2.0, 0.3), "velocity": (20.0, 0.1), "speed": 20.0},
                            imu={"t": t, "rpy": (0.0, 0.01, 0.02)}, radars=[scan], lidar_config=cfg,
                            lidar_frame=lidar.encode_frame(cfg, ranges, seq, t, compress=True), lidar_t=t)


def test_transport_round_trip_and_invalid_returns():
    rx = transport.Receiver(MOUNTS)
    ranges = np.full((16, 36), 20.0)
    ranges[3, 4], ranges[5, 6] = np.inf, 123.456  # no return; beyond max range
    f = rx.accept(_frame(ranges=ranges))
    assert f.gnss["position"] == (1.0, 2.0, 0.3) and f.imu["rpy"][2] == pytest.approx(0.02)
    r = f.radars[FRONT.name].returns
    assert r[0].range_rate == pytest.approx(-1.5) and r[0].power == pytest.approx(-40.0)
    assert r[1].range_rate is None and r[1].power is None  # NaN speed: not valid, never a number
    got = f.lidar["ranges"]
    assert np.isnan(got[3, 4]) and np.isnan(got[5, 6])
    ok = ~np.isnan(got)
    assert np.max(np.abs(got[ok] - ranges[ok])) <= 0.005  # 1 cm quantisation
    assert f.dropped == ()


def test_transport_rejects_corrupt_truncated_and_out_of_order_frames():
    rx = transport.Receiver(MOUNTS)
    good = _frame(seq=5)
    bad = bytearray(good)
    bad[60] ^= 0xFF
    for data, reason in ((bytes(bad), "corrupt"), (good[:-3], "truncated"), (b"XXXX" + good[4:], "not_rssf"),
                         (good[:4] + struct.pack("<H", 2) + good[6:], "version"), (good * 600, "too_large")):
        with pytest.raises(transport.Rejected, match=reason):
            rx.accept(data)
    rx.accept(good)
    with pytest.raises(transport.Rejected, match="out_of_order"):
        rx.accept(_frame(seq=5))
    with pytest.raises(transport.Rejected, match="out_of_order"):
        rx.accept(_frame(seq=4))
    rx.accept(_frame(seq=8))
    assert rx.counts["sequence_gaps"] == 2 and rx.counts["accepted"] == 2


def test_transport_drops_stale_future_unknown_and_changed_sections():
    rx = transport.Receiver(MOUNTS, max_age_s=0.25)
    f = rx.accept(_frame(seq=1, radar_t=9.5))
    assert FRONT.name not in f.radars and ("radar", "stale") in f.dropped
    f = rx.accept(_frame(seq=2, radar_t=10.2))
    assert ("radar", "future") in f.dropped
    moved = (4.5,) + MOUNTS[FRONT.name][1:]
    f = rx.accept(_frame(seq=3, mount=moved))
    assert (FRONT.name, "mount_changed") in f.dropped
    rx2 = transport.Receiver(MOUNTS)
    cfg = _cfg()
    no_config = transport.encode(1, 1.0, lidar_frame=lidar.encode_frame(cfg, np.full((16, 36), 9.0), 1, 1.0), lidar_t=1.0)
    f = rx2.accept(no_config)
    assert f.lidar is None and ("lidar", "unknown_config") in f.dropped
    assert rx2.counts["dropped_sections"]["unknown_config"] == 1


def test_transport_outage_is_reported_not_silent():
    rx = transport.Receiver(MOUNTS)
    f = rx.accept(transport.encode(1, 1.0, lidar_health=transport.FAILED))
    assert f.lidar == {"t": 1.0, "health": transport.FAILED}


def test_v1_frames_round_trip_into_the_unchanged_pipeline():
    frame = {"schema": "roadsense.sensors.v1", "timestamp": 3.0,
             "ego": {"vehicle_id": "EGO_ROADSENSE", "position": [0.0, 6.7, 0.3], "velocity": [20.0, 0.0],
                     "speed_mps": 20.0, "yaw_rad": 0.0, "steering_rad": 0.0, "maneuver": "lane_keep", "lane": 1},
             "radars": {"radar front": {"targets": [(30.0, 0.02, -2.0)], "limits": (1.0, 150.0, 2.6)},
                        "radar rear": {"targets": [], "limits": (1.0, 100.0, 2.6)}},
             "objects": [], "sides": None}
    mounts = {n: (x, y, 0.2, 0.0, 0.0, yaw) for n, (_s, x, y, yaw) in perception.RADARS.items()}
    back = transport.to_v1(transport.Receiver(mounts).accept(transport.from_v1(frame, 1, mounts)),
                           {"lane": 1})
    a, b = pipeline.Pipeline("EGO_ROADSENSE"), pipeline.Pipeline("EGO_ROADSENSE")
    for k in range(4):
        f1 = json.loads(json.dumps(frame))
        f1["timestamp"] = f2_t = 3.0 + 0.2 * k
        f2 = dict(back, timestamp=f2_t)
        pos = lambda cyc: [(r["relative_position"]["longitudinal_m"], r["relative_position"]["lateral_m"])  # noqa: E731
                           for r in cyc.reports]
        got, want = pos(b.process(f2)), pos(a.process(f1))
        assert len(got) == len(want) and all(g == pytest.approx(w, abs=1e-4) for g, w in zip(got, want))


# -- radar model -----------------------------------------------------------------------------------------------

def test_radar_measurement_flags_and_conservative_clearance():
    m = radar.measure(FRONT, 1.0, 30.0, 0.0, -3.0)
    assert m.lon == pytest.approx(34.054) and not m.flags
    lo, hi = m.clearance
    point = 34.054 - radar.EGO_BOX[1]
    assert lo < point - 2.0 and hi < point  # the surface is nearer than the point, by an evidence-based slack
    edge = radar.measure(FRONT, 1.0, 12.0, 1.3, -1.0)
    assert "bearing_clamped" in edge.flags and edge.sigma_bearing >= 0.15  # kept: one-sided bearing, not dropped
    first = radar.measure(FRONT, 1.0, 30.0, 0.1, 1.9e5)
    assert "velocity_unreliable" in first.flags and first.range_rate is None and first.range == pytest.approx(30.0)
    clutter = radar.measure(FRONT, 1.0, 1.0, 1.3, 0.0)
    assert {"self_suspect", "range_clamped"} <= clutter.flags and clutter.clearance[0] == 0.0


# -- lidar -----------------------------------------------------------------------------------------------------

def _scene(cfg, boxes, pitch=0.0):
    """A synthetic range image: flat road at z = 0, boxes (x0, x1, y0, y1, z0, z1) in the ego frame, sensor
    pitched by `pitch` (nose down positive), by ray marching."""
    r = np.full((cfg.layers, cfg.width), np.inf)
    mx, my, mz = cfg.mount[:3]
    cp, sp = math.cos(pitch), math.sin(pitch)
    for li, el in enumerate(cfg.elevations):
        for ci in range(cfg.width):
            az = cfg.az0 + ci * cfg.az_step
            d = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
            d = np.array([cp * d[0] + sp * d[2], d[1], -sp * d[0] + cp * d[2]])  # vehicle pitched nose down
            best = np.inf
            if d[2] < 0:
                best = mz / -d[2]
            for x0, x1, y0, y1, z0, z1 in boxes:
                lo, hi = 0.0, np.inf
                for o, dd, a, b in ((mx, d[0], x0, x1), (my, d[1], y0, y1), (mz, d[2], z0, z1)):
                    if abs(dd) < 1e-12:
                        if not a <= o <= b:
                            lo, hi = 1, 0
                        continue
                    t1, t2 = sorted(((a - o) / dd, (b - o) / dd))
                    lo, hi = max(lo, t1), min(hi, t2)
                if lo <= hi and lo < best:
                    best = lo
            r[li, ci] = best if best <= cfg.max_range else np.inf
    return r


def test_lidar_attitude_levelling_keeps_the_road_out_of_obstacles():
    cfg = lidar.Config.webots("roof", (1.11, 0, 1.51, 0, 0, 0), 360, 16, 6.283, 0.5, 100.0, lidar.ROOF16_ELEVATIONS)
    ranges = _scene(cfg, [], pitch=0.07)
    assert len(lidar.process(cfg, ranges, radar.EGO_BOX)["clusters"]) > 0  # unlevelled: the road becomes obstacles
    levelled = lidar.process(cfg, ranges, radar.EGO_BOX, attitude=(0.0, 0.07))
    assert levelled["clusters"] == []


def test_lidar_free_space_never_treats_no_return_as_empty():
    cfg = lidar.Config.webots("roof", (1.11, 0, 1.51, 0, 0, 0), 720, 16, 6.283, 0.5, 100.0, lidar.ROOF16_ELEVATIONS)
    car = (20.0, 24.5, -0.9, 0.9, 0.2, 1.5)
    ranges = _scene(cfg, [car])
    g = lidar.process(cfg, ranges, radar.EGO_BOX)["ground"]
    assert lidar.free_space(cfg, ranges, g, 21.0, 0.0) == lidar.OCCUPIED
    assert lidar.free_space(cfg, ranges, g, 35.0, 0.0) == lidar.OCCLUDED  # behind the car
    assert lidar.free_space(cfg, ranges, g, 12.0, 3.0) == lidar.SEEN_THROUGH
    blind = np.full_like(ranges, np.nan)
    assert lidar.free_space(cfg, blind, g, 12.0, 3.0) == lidar.UNKNOWN


# -- tracker ---------------------------------------------------------------------------------------------------

def _ego(t, v=20.0, y=6.7):
    return trk.EgoState(t, v * t, y, 0.0, v, 0.0)


def _radar_returns(ego, targets, spec=FRONT, rng=None):
    """Webots-style returns of world points (x, y, vx, vy) seen by spec at ego."""
    out = []
    x, y, _z, yaw = spec.mount
    for tx, ty, vx, vy in targets:
        lon, lat = ego.to_ego(tx, ty)
        dx, dy = lon - x, lat - y
        d = math.hypot(dx, dy)
        az = -math.remainder(math.atan2(dy, dx) - yaw, 2 * math.pi)
        if not (spec.min_range < d < spec.max_range and abs(az) < spec.hfov / 2):
            continue
        rr = ((vx - ego.vx) * (tx - ego.x - x) + (vy - ego.vy) * (ty - ego.y - y)) / d
        if rng is not None:
            d, az, rr = d + rng.gauss(0, 0.25), az + rng.gauss(0, 0.001), rr + rng.gauss(0, 0.12)
        out.append(radar.measure(spec, ego.t, d, az, rr))
    return out


def _run_radar(tr, steps, targets_at, dt=0.1, healthy=lambda t: True, spec=FRONT, rng=None, start=0):
    for k in range(start + 1, start + steps + 1):
        t = k * dt
        ego = _ego(t)
        tr.radar_batch(t, ego, spec.name, _radar_returns(ego, targets_at(t), spec, rng) if healthy(t) else [],
                       healthy=healthy(t))
        tr.prune(t)
    return ego


def test_tracker_confirms_and_estimates_velocity():
    tr = trk.Tracker([FRONT])
    rng = random.Random(1)
    ego = _run_radar(tr, 30, lambda t: [(40 + 15 * t, 6.7, 15.0, 0.0)], rng=rng)
    (t1,) = tr.confirmed()
    assert t1.x[2] == pytest.approx(15.0, abs=0.6) and abs(t1.x[3]) < 0.6
    assert ego.to_ego(t1.x[0], t1.x[1])[1] == pytest.approx(0.0, abs=0.3)


def test_tracker_keeps_neighbours_apart_and_identities_stable():
    tr = trk.Tracker([FRONT])
    rng = random.Random(2)
    ids = set()
    for k in range(1, 41):
        t = k * 0.1
        ego = _ego(t)
        tr.radar_batch(t, ego, FRONT.name, _radar_returns(ego, [(50 + 20 * t, 6.7, 20.0, 0.0),
                                                                 (50 + 20 * t, 10.45, 20.0, 0.0)], rng=rng))
        tr.prune(t)
        ids |= {x.id for x in tr.confirmed()}
    assert len(tr.confirmed()) == 2 and len(ids) == 2


def test_first_refresh_speed_is_rejected_but_the_position_kept():
    tr = trk.Tracker([FRONT])
    _run_radar(tr, 10, lambda t: [(40 + 20 * t, 6.7, 20.0, 0.0)])
    (t1,) = tr.confirmed()
    ego = _ego(1.1)
    m = _radar_returns(ego, [(40 + 22, 6.7, 20.0, 0.0)])[0]._replace(range_rate=55.0)  # plausible-looking garbage
    tr.radar_batch(1.1, ego, FRONT.name, [m])
    assert tr.stats["rr_rejected"] == 1 and t1.last_hit == pytest.approx(1.1)
    assert t1.x[2] == pytest.approx(20.0, abs=1.0)


def test_occluded_or_out_of_service_tracks_coast_seen_through_ones_expire():
    def make():
        tr = trk.Tracker([FRONT])
        _run_radar(tr, 15, lambda t: [(30 + 20 * t, 6.7, 20.0, 0.0)])
        return tr
    tr = make()  # radar outage: no penalty, coasts until MAX_COAST_S
    _run_radar(tr, 25, lambda t: [], healthy=lambda t: False, start=15)
    assert len(tr.confirmed()) == 1 and tr.stats["late_batches"] == 0
    tr = make()  # healthy radar sees nothing where the track is: expected misses remove it within about 1 s
    _run_radar(tr, 9, lambda t: [], start=15)
    assert len(tr.confirmed()) == 1  # a few misses are tolerated (radar detection probability)
    _run_radar(tr, 3, lambda t: [], start=24)
    assert tr.confirmed() == [] and tr.stats["late_batches"] == 0
    tr = make()  # lidar beams pass through the spot: removed faster than by radar misses
    t = 1.5
    for k in range(3):
        t += 0.1
        tr.lidar_batch(t, _ego(t), [], visibility=lambda lon, lat: lidar.SEEN_THROUGH)
        tr.prune(t)
    assert tr.confirmed() == []


def test_a_radar_miss_never_cancels_a_lidar_hit():
    tr = trk.Tracker([FRONT])
    _run_radar(tr, 10, lambda t: [(30 + 20 * t, 6.7, 20.0, 0.0)])
    (t1,) = tr.confirmed()
    for k in range(11, 40):
        t = k * 0.1
        ego = _ego(t)
        lon, lat = ego.to_ego(30 + 20 * t, 6.7)
        c = trk.LidarCluster(t, (lon - 2.25, lat), (lon - 2.25, lon + 0.5), (lat - 0.9, lat + 0.9), 80, (0, 1), 0.05)
        tr.lidar_batch(t, ego, [c])
        tr.radar_batch(t, ego, FRONT.name, [])  # the radar keeps missing it
        tr.prune(t)
    assert tr.confirmed() == [t1]


def test_truck_fragments_join_the_truck_but_another_vehicle_does_not():
    tr = trk.Tracker([])
    rear = lambda t: -2.0 + 4.0 * t  # noqa: E731  an 8 m truck overtaking the ego in the next lane
    for k in range(1, 30):
        t = k * 0.1
        c = trk.LidarCluster(t, (rear(t), 3.5), (rear(t), rear(t) + 8.0), (2.5, 4.5), 200, (1.0, 1.2), 0.05)
        tr.lidar_batch(t, _ego(t), [c])
        tr.prune(t)
    (truck,) = tr.confirmed()
    assert truck.seen_alongside and truck.extent()[0] == pytest.approx(8.0)
    t = 3.0
    r = rear(t)
    cab = trk.LidarCluster(t, (r + 4.5, 3.5), (r + 4.5, r + 8.0), (2.5, 4.5), 120, (1.0, 1.2), 0.05)
    chassis = trk.LidarCluster(t, (r, 3.5), (r, r + 3.9), (2.6, 4.4), 40, (1.0, 1.2), 0.05)
    other = trk.LidarCluster(t, (r + 11.0, 3.5), (r + 11.0, r + 15.5), (2.6, 4.4), 60, (5.0, 5.2), 0.05)
    tr.lidar_batch(t, _ego(t), [cab, chassis, other])
    assert tr.stats["fragments"] == 1 and len(tr.tracks) == 2


def test_radar_born_and_lidar_born_tracks_of_one_car_become_one():
    tr = trk.Tracker([FRONT])
    for k in range(1, 20):
        t = k * 0.1
        ego = _ego(t)
        lon, lat = ego.to_ego(30 + 20 * t, 6.7)
        c = trk.LidarCluster(t, (lon - 1.8, lat), (lon - 1.8, lon + 0.5), (lat - 0.9, lat + 0.9), 80, (0, 1), 0.05)
        tr.radar_batch(t, ego, FRONT.name, _radar_returns(ego, [(30 + 20 * t, 6.7, 20.0, 0.0)]))
        tr.lidar_batch(t, ego, [c])
        tr.prune(t)
    assert len(tr.tracks) == 1 and {k for _t, k in tr.tracks[0].hits} == {"radar", "lidar"}


def test_lidar_born_track_is_confirmed_only_with_a_settled_velocity():
    tr = trk.Tracker([])
    rng = random.Random(3)
    for k in range(1, 30):
        t = k * 0.1
        ego = _ego(t)
        lon, lat = ego.to_ego(30 + 14 * t, 3.0)  # 6 m/s slower than the ego, next lane
        j = rng.gauss(0, 0.15)  # the visible face jitters
        c = trk.LidarCluster(t, (lon - 2.25 + j, lat), (lon - 2.25 + j, lon - 1.0), (lat - 0.9, lat + 0.9), 60,
                             (0, 1), 0.05)
        tr.lidar_batch(t, ego, [c])
        tr.prune(t)
        if tr.confirmed():
            break
    (t1,) = tr.confirmed()
    assert k >= 4 and abs(t1.x[2] - 14.0) < 4.0  # not on 3 hits with a guessed speed
    wide = trk.LidarCluster(t, (lon - 2.25, lat), (lon - 2.25, lon - 1.0), (lat - 0.9, lat + 3.0), 90, (0, 1), 0.05)
    trk.Tracker._extent(t1, wide)
    assert t1.width < 2.6  # a 3.9 m wide cluster is two objects, not this car's width


def test_late_batches_are_counted_not_applied():
    tr = trk.Tracker([FRONT])
    _run_radar(tr, 10, lambda t: [(30 + 20 * t, 6.7, 20.0, 0.0)])
    before = tr.confirmed()[0].x.copy()
    ego = _ego(0.5)
    tr.radar_batch(0.5, ego, FRONT.name, _radar_returns(ego, [(40, 6.7, 20.0, 0.0)]))
    assert tr.stats["late_batches"] >= 1 and np.allclose(tr.confirmed()[0].x, before)


# -- fusion and shadow safety ----------------------------------------------------------------------------------

def test_lane_probabilities_flag_ambiguous_positions():
    perception.use_road("highway_lht")
    try:
        p = fusion.lane_probabilities(6.7, 0.2, 6.7)
        assert p["ego_lane"] > 0.99
        q = fusion.lane_probabilities(5.0, 0.6, 6.7)  # on a lane line
        assert max(q.values()) < fusion.LANE_CERTAIN
    finally:
        perception.use_road("highway")


def test_clearance_view_places_the_assumed_body_at_the_lower_bound():
    rep = {"relative_position": {"longitudinal_m": 30.0, "lateral_m": 0.2},
           "experimental": {"clearance_m": {"lo": 20.0}}}
    v = fusion.clearance_view(rep)
    assert v["relative_position"]["longitudinal_m"] == pytest.approx(3.89 + 20.0 + 2.5)
    assert rep["relative_position"]["longitudinal_m"] == 30.0  # the report itself is not changed


def test_authoritative_pipeline_never_imports_the_experimental_package():
    for path in (ROOT / "src/roadsense").rglob("*.py"):
        if "experimental" in path.parts:
            continue
        assert "experimental" not in path.read_text(), path
    assert "experimental" not in (ROOT / "simulation/webots/controllers/roadsense_ego/roadsense_ego.py").read_text()


def test_calibration_constants_come_from_development_runs_only():
    doc = json.loads((ROOT / "src/roadsense/experimental/calibration.json").read_text())
    assert doc["runs"] and all(not r.startswith(("corr-", "orig-")) for r in doc["runs"])
    assert radar.SLACK_M and fusion.LIDAR_SLACK_M
