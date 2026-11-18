"""Experimental multi-sensor perception, shadow mode: transport.Frame -> tracks and reports.

Modes: "radar" (radar returns only), "lidar" (lidar clusters only) and "fused" (both, one tracker). The reports have
the fields of perception.track_report, so the unchanged prediction, risk and safety functions can rate them, plus an
"experimental" block: track status, uncertainties, existence, sources, clearance interval and lane probabilities.
shadow_decision() runs those unchanged functions on experimental reports for comparison only: nothing here feeds
the authoritative pipeline, warnings or the ego's control. Ground truth is never an input.
"""
import math
import time
from typing import NamedTuple

import numpy as np

from roadsense.autonomy import perception, prediction, risk, safety

from . import lidar, radar, tracker as trk

CLEARANCE_MAX_AGE_S = 0.25  # older sensor intervals are not used for the current clearance
# Map region of interest for lidar clusters: the ego's carriageway (perception.CARRIAGEWAY_Y, the lane map RoadSense
# already uses for lane relations), its median-side edge moved in by ROI_MEDIAN_MARGIN_M so the median guardrail is
# outside. Roadside trees, posts and the other carriageway are structure, not traffic. Clusters longer than
# MAX_VEHICLE_LENGTH_M (guardrail runs) are structure too.
ROI_MEDIAN_MARGIN_M = 0.3
MAX_VEHICLE_LENGTH_M = 20.0
LANE_CERTAIN = 0.8
LIDAR_SURFACE_SIGMA_M = 0.05


# Lidar near-face slack (visible near point's clearance - true surface clearance, m): (q01, q50, q99) by the height
# of the lowest visible point of the near face above the road and by range (calibration.json, W9 development runs).
# A high lowest return leaves room for structure hidden between beams (a bumper, a truck's chassis).
LIDAR_SLACK_M = {}
LIDAR_DEFAULT_SLACK = (0.0, 0.0, 1.5)
FACE_BINS = ((0.0, 0.5), (0.5, 1.0), (1.0, 99.0))
LIDAR_BANDS = ((0, 20), (20, 40), (40, 1e9))


def lidar_slack_key(face_low, rng):
    f = next(("%g-%g" % b for b in FACE_BINS if b[0] <= face_low < b[1]), "1-99")
    r = next(("%d-%s" % (lo, "" if hi > 1e8 else int(hi)) for lo, hi in LIDAR_BANDS if lo <= rng < hi))
    return "face %s range %s" % (f, r)


def lidar_slack(face_low, rng):
    return LIDAR_SLACK_M.get(lidar_slack_key(face_low, rng)) or LIDAR_SLACK_M.get("all") or LIDAR_DEFAULT_SLACK


def _load_calibration():
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
    if os.path.exists(path):
        with open(path) as f:
            LIDAR_SLACK_M.update({k: tuple(v) for k, v in json.load(f).get("lidar_slack_m", {}).items()})


_load_calibration()


class Config(NamedTuple):
    mode: str  # "radar", "lidar" or "fused"
    radars: tuple  # radar.RadarSpec of the radars used (disabled ones are left out)
    ego_box: tuple = radar.EGO_BOX


def profile_radars(bearing_reference="point"):
    """The experimental profile's radars (tools/sensor_profile.py PROFILE_RADARS), front corners excluded: they see
    the ego's own body on every sample (W8-W11 §6.3) and stay disabled until re-mounted or validated."""
    def spec(name, group, x, y, z, yaw, rng, hfov):
        return radar.RadarSpec(name, group, (x, y, z, yaw), 1.0, rng, hfov, bearing_reference)
    return (spec("profile radar front", "front", 4.054, 0.0, 0.142, 0.0, 150, 2.6),
            spec("profile radar rear", "rear", -1.16, 0.0, 0.33, math.pi, 100, 2.6),
            spec("profile radar corner rl", "corner_rl", -1.1, 0.85, 0.33, 2.3562, 80, 1.6),
            spec("profile radar corner rr", "corner_rr", -1.1, -0.85, 0.33, -2.3562, 80, 1.6))


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def lane_probabilities(world_y, sigma_y, ego_y):
    """P(each lane relation) for a lateral position with 1-sigma uncertainty."""
    low, high = perception.CARRIAGEWAY_Y
    ego_lane = perception.lane_index(ego_y)
    out = {}
    sigma = max(sigma_y, 0.05)
    for k in range(int(round((high - low) / perception.LANE_WIDTH_M))):
        a, b = low + k * perception.LANE_WIDTH_M, low + (k + 1) * perception.LANE_WIDTH_M
        p = _phi((b - world_y) / sigma) - _phi((a - world_y) / sigma)
        rel = "other"
        if ego_lane is not None:
            rel = {0: "ego_lane", -perception.right_step(): "left_lane",
                   perception.right_step(): "right_lane"}.get(k - ego_lane, "other")
        out[rel] = out.get(rel, 0.0) + p
    out["other"] = out.get("other", 0.0) + max(0.0, 1 - sum(out.values()))
    return {k: round(v, 3) for k, v in out.items()}


class Cycle(NamedTuple):
    t: float
    reports: list
    timing_ms: dict
    counts: dict


class ExperimentalPerception:
    def __init__(self, config):
        self.config = config
        self.specs = {s.name: s for s in config.radars}
        self.tracker = trk.Tracker(config.radars, lidar_enabled=config.mode != "radar")

    def process(self, frame):
        t0 = time.perf_counter()
        timing = {"radar_model": 0.0, "lidar": 0.0, "tracking": 0.0, "reports": 0.0}
        counts = {"radar_measurements": 0, "lidar_clusters": 0, "radar_outages": 0, "lidar_outage": 0}
        g, imu = frame.gnss, frame.imu
        if g is None or imu is None or g.get("health", 0) != 0:
            return Cycle(frame.t, [], timing, dict(counts, no_ego_state=1))
        ego = trk.EgoState(g["t"], g["position"][0], g["position"][1], imu["rpy"][2], *g["velocity"])
        if self.config.mode in ("radar", "fused"):
            for name, spec in self.specs.items():
                scan = frame.radars.get(name)
                if scan is None or scan.health == 2:
                    counts["radar_outages"] += 1  # no penalty for unseen tracks: the radar could not see
                    continue
                a = time.perf_counter()
                ms = [radar.measure(spec, scan.t, r.distance, r.azimuth, r.range_rate, r.power) for r in scan.returns]
                b = time.perf_counter()
                self.tracker.radar_batch(scan.t, ego._replace(t=scan.t), name, ms)
                timing["radar_model"] += (b - a) * 1e3
                timing["tracking"] += (time.perf_counter() - b) * 1e3
                counts["radar_measurements"] += len(ms)
        if self.config.mode in ("lidar", "fused"):
            ld = frame.lidar
            if ld is None or ld["health"] != 0:
                counts["lidar_outage"] = 1
            else:
                a = time.perf_counter()
                cfg, ranges = ld["config"], ld["ranges"]
                res = lidar.process(cfg, ranges, self.config.ego_box, attitude=tuple(imu["rpy"][:2]))
                clusters = [self._cluster(ld["t"], c, cfg) for c in res["clusters"] if self._on_road(c, ego)]
                counts["lidar_structure"] = len(res["clusters"]) - len(clusters)
                ground = res["ground"]
                b = time.perf_counter()
                self.tracker.lidar_batch(ld["t"], ego._replace(t=ld["t"]), clusters,
                                         visibility=lambda lon, lat: lidar.free_space(cfg, ranges, ground, lon, lat))
                timing["lidar"] += (b - a) * 1e3
                timing["tracking"] += (time.perf_counter() - b) * 1e3
                counts["lidar_clusters"] += len(clusters)
        a = time.perf_counter()
        self.tracker.prune(frame.t)
        self.tracker.group(frame.t, ego)
        timing["tracking"] += (time.perf_counter() - a) * 1e3
        a = time.perf_counter()
        reports = [self.report(tr, frame.t, ego) for tr in self.tracker.confirmed()]
        timing["reports"] = (time.perf_counter() - a) * 1e3
        timing["total"] = (time.perf_counter() - t0) * 1e3
        counts["tracks"], counts["confirmed"] = len(self.tracker.tracks), len(reports)
        return Cycle(frame.t, reports, timing, counts)

    @staticmethod
    def _on_road(c, ego):
        if c["x"][1] - c["x"][0] > MAX_VEHICLE_LENGTH_M:
            return False
        low, high = perception.CARRIAGEWAY_Y
        ys = [ego.to_world(x, y)[1] for x in c["x"] for y in c["y"]]
        return max(ys) >= low + ROI_MEDIAN_MARGIN_M and min(ys) <= high

    def _cluster(self, t, c, cfg):
        near_lon, near_lat = c["near"]
        clear = radar.footprint_distance(near_lon, near_lat, self.config.ego_box)
        hidden = max(0.0, c["near_range_interval"][1] - c["near_range_interval"][0])  # low structure between beams
        s = max(c["sigma_lateral"], LIDAR_SURFACE_SIGMA_M)
        q01, _q50, q99 = lidar_slack(c["face_low"], c["range"])
        return trk.LidarCluster(t, c["near"], c["x"], c["y"], c["n"],
                                (max(0.0, clear - max(hidden, q99) - 2 * s), clear - min(0.0, q01) + 2 * s), s)

    def clearance(self, tr, t, ego, lon, lat, rel_speed):
        """(lo, hi, basis, conflict) from the track's own sensor intervals of the last CLEARANCE_MAX_AGE_S, widened by
        the relative motion since; no sensor interval: the predicted centre less the radar slack (conservative)."""
        got = {}
        for kind, (tm, lo, hi) in tr.clearance.items():
            age = t - tm
            if 0 <= age <= CLEARANCE_MAX_AGE_S:
                d = rel_speed * age
                got[kind] = (max(0.0, lo - d), hi + d)
        if "radar" in got and "lidar" in got:
            lo, hi = max(got["radar"][0], got["lidar"][0]), min(got["radar"][1], got["lidar"][1])
            if lo <= hi:
                return lo, hi, "radar+lidar", False
            return (min(got["radar"][0], got["lidar"][0]), max(got["radar"][1], got["lidar"][1]),
                    "radar+lidar", True)
        if got:
            kind, (lo, hi) = next(iter(got.items()))
            return lo, hi, kind, False
        c = radar.footprint_distance(lon, lat, self.config.ego_box)
        s = 2 * tr.sigma_pos()
        length = tr.extent()[0]
        return max(0.0, c - max(radar.slack("front", lat, c)[2], length / 2) - s), c + s, "predicted", False

    def report(self, tr, t, ego):
        lon, lat = ego.to_ego(tr.x[0], tr.x[1])
        rel = tr.x[2:] - np.array([ego.vx, ego.vy])
        c, s = math.cos(ego.yaw), math.sin(ego.yaw)
        v_lon, v_lat = rel[0] * c + rel[1] * s, rel[1] * c - rel[0] * s
        dist = math.hypot(lon, lat)
        range_rate = (lon * v_lon + lat * v_lat) / max(dist, 0.1)
        R = np.array([[c, s], [-s, c]])
        Pe = R @ tr.P[:2, :2] @ R.T
        Pv = R @ tr.P[2:, 2:] @ R.T
        lo, hi, basis, conflict = self.clearance(tr, t, ego, lon, lat, math.hypot(v_lon, v_lat))
        lanes = lane_probabilities(tr.x[1], math.sqrt(tr.P[1, 1]), ego.y)
        best = max(lanes, key=lanes.get)
        recent = sorted(k for k, tm in tr.last_by.items() if t - tm <= 0.5)
        return {
            "track_id": tr.id, "object_type": "vehicle", "timestamp": round(t, 3),
            "relative_position": {"longitudinal_m": round(lon, 2), "lateral_m": round(lat, 2)},
            "distance_m": round(dist, 2), "bearing_deg": round(math.degrees(math.atan2(lat, lon)), 1),
            "relative_velocity": {"longitudinal_mps": round(v_lon, 2), "lateral_mps": round(v_lat, 2)},
            "relative_speed_mps": round(range_rate, 2),
            "lane_relation": perception.lane_relation(tr.x[1], ego.y),
            "source": "+".join(recent) or "coasting",
            "age_s": round(t - tr.first, 2), "last_seen": round(min(tr.last_hit, t), 3),
            "experimental": {
                "status": "coasting" if t - tr.last_hit > 1e-6 else "confirmed",
                "sigma_m": {"longitudinal": round(math.sqrt(Pe[0, 0]), 2), "lateral": round(math.sqrt(Pe[1, 1]), 2)},
                "sigma_mps": {"longitudinal": round(math.sqrt(Pv[0, 0]), 2), "lateral": round(math.sqrt(Pv[1, 1]), 2)},
                "existence": round(1 / (1 + math.exp(-tr.existence)), 3),
                "last_update_s": {k: round(t - v, 2) for k, v in tr.last_by.items()},
                "clearance_m": {"lo": round(lo, 2), "hi": round(hi, 2), "basis": basis, "conflict": conflict},
                "lane_probabilities": lanes, "lane_uncertain": lanes[best] < LANE_CERTAIN,
                "extent_m": {"length_visible": round(tr.length, 2), "width_visible": round(tr.width, 2),
                             "length_known": tr.seen_alongside},
                "group": tr.group, "world_velocity": [round(float(tr.x[2]), 2), round(float(tr.x[3]), 2)],
            },
        }


def clearance_view(report):
    """The report moved so risk.py's assumed target size puts its surface at the clearance lower bound: risk.py
    rates a centre with a 5 m x 1.9 m body; this places that body's near face where the experimental clearance
    interval says the real surface may be (ahead, behind or alongside)."""
    lo = report["experimental"]["clearance_m"]["lo"]
    p = dict(report["relative_position"])
    front = risk.EGO_CENTRE_M + risk.EGO_HALF_LENGTH_M
    rear = risk.EGO_CENTRE_M - risk.EGO_HALF_LENGTH_M
    if p["longitudinal_m"] > front:
        p["longitudinal_m"] = front + lo + risk.TARGET_HALF_LENGTH_M
    elif p["longitudinal_m"] < rear:
        p["longitudinal_m"] = rear - lo - risk.TARGET_HALF_LENGTH_M
    else:
        p["lateral_m"] = math.copysign(risk.EGO_HALF_WIDTH_M + lo + risk.TARGET_HALF_WIDTH_M, p["lateral_m"])
    return dict(report, relative_position=p)


def shadow_decision(t, ego_speed_mps, reports, policy, use_clearance=False):
    """The unchanged prediction/risk/safety functions applied to experimental reports (vehicles only; no hazard
    map), for comparison. Never published, never acted on."""
    rs = [clearance_view(r) for r in reports] if use_clearance else reports
    assessments = [risk.assess(r, prediction.predict(r)) for r in rs]
    return safety.decide(t, ego_speed_mps, assessments, rs, {"hazards": []}, (), policy), assessments
