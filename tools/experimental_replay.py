"""Offline replay and evaluation of the experimental perception pipeline (shadow mode) against the original one.

    PYTHONPATH=src venv/bin/python tools/experimental_replay.py calibrate RUN... --out src/roadsense/experimental/calibration.json
    PYTHONPATH=src venv/bin/python tools/experimental_replay.py replay RUN... --out results.json [--variants ...]
    PYTHONPATH=src venv/bin/python tools/experimental_replay.py warnings RUN... --out warnings.json
    PYTHONPATH=src venv/bin/python tools/experimental_replay.py demo RUN [--from T --to T]
    PYTHONPATH=src venv/bin/python tools/experimental_replay.py tables results.json [warnings.json]

RUN is a tools/sensor_profile.py run directory recorded with --capture-frames. Trackers (docs/experimental-perception.md):

  A / B  RoadSense's authoritative pipeline (pipeline.Pipeline, unchanged) on the sensor frames its own controller
         captured: A on original-simulator runs (orig-*), B on corrected-simulator runs (corr-*);
  C      experimental tracker, radar only (front, rear and rear-corner radars);
  D      experimental tracker, lidar only;
  E      experimental tracker, radar and lidar fused;
  E-*    robustness variants of E: radar_front_outage, lidar_outage, lidar_late (one cycle late), corrupt_1pct.

C-E read the profile's recorded sensors through the RSSF transport (encode -> Receiver -> perception), so every
evaluated observation crossed the binary interface. Ground truth (the drawn bodies in records.jsonl) only scores:
it never reaches any tracker. All recordings are development evidence, not acceptance data.
"""
import argparse
import json
import math
import os
import random
import resource
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("ROADSENSE_ROAD_PROFILE", "highway_lht")

from roadsense.autonomy import perception, pipeline, prediction, risk, safety  # noqa: E402
from roadsense.experimental import fusion, lidar, radar, transport  # noqa: E402
from roadsense.experimental import tracker as trk  # noqa: E402
import sensor_profile as sp  # noqa: E402

perception.use_road("highway_lht")
POLICY = safety.Policy(traffic="left")
LEVELS = risk.LEVELS
MATCH_M = 2.0  # a track within this distance of a body's footprint is a track of that body
REGION = (-60.0, 100.0, 9.0)  # lon min, lon max, |lat| max: where every tracker should see vehicles
ZONES = {"left": (-6.0, 4.5, 1.0, 4.5), "right": (-6.0, 4.5, -4.5, -1.0)}
RADAR_MOUNTS = {name: (x, y, z, 0.0, 0.0, yaw) for name, (x, y, z, yaw, *_r) in sp.PROFILE_RADARS.items()}
CORRUPT_SEED = 7


def pct(v, q):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    i = int(k)
    return round(v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i), 4)


def st(v, qs=(0.5, 0.95)):
    v = [x for x in v if x is not None]
    out = {"n": len(v)}
    for q in qs:
        out["p%d" % round(q * 100)] = pct(v, q)
    return out


def share(n, d):
    return None if not d else round(n / d, 4)


# ---------------------------------------------------------------------------------------------------------------
# recordings

def read_run(run_dir):
    run_dir = Path(run_dir)
    lines = [json.loads(l) for l in open(run_dir / "rec/records.jsonl")]
    start, recs = lines[0], [r for r in lines[1:] if r["type"] == "sample"]
    seen, since = set(), {}
    for r in recs:  # bodies put on the road in the last INSERTION_SKIP_S (see insertions())
        nodes = {g["node"] for g in r["gt"] if g["wrapper"][0] < 9000}
        for n in nodes - seen:
            since[n] = r["t"]
        r["_fresh"] = {n for n in nodes if r["t"] - since[n] <= INSERTION_SKIP_S}
        seen = nodes
    info = json.loads((run_dir / "run.json").read_text())
    blob = None
    if (run_dir / "rec/lidar.bin").exists() and (run_dir / "rec/lidar.bin").stat().st_size:
        blob = np.memmap(run_dir / "rec/lidar.bin", dtype="<f4", mode="r")
    events = []
    gt = run_dir / "ground_truth.jsonl"
    if gt.exists():
        for line in open(gt):
            r = json.loads(line)
            if r.get("record") == "event":
                events.append(r)
    return {"dir": run_dir, "name": run_dir.name, "start": start, "recs": recs, "info": info, "lidar": blob,
            "events": events, "scenario": json.loads((run_dir / "scenario.json").read_text())}


def lidar_config(start, encoding=lidar.UINT16):
    lid = start["lidar"]
    return lidar.Config.webots("profile lidar roof", sp.PROFILE_LIDAR + (0.0, 0.0, 0.0), lid["w"], lid["layers"],
                               lid["fov"], lid["min_range"], lid["max_range"], lidar.ROOF16_ELEVATIONS,
                               encoding=encoding, scale=0.01)


def truth(rec):
    """Every recorded vehicle body in the ego frame at rec["t"] (scoring only; cached on the record)."""
    if "_truth" in rec:
        return rec["_truth"]
    ego, ev = rec["ego"], rec["ego_v"]
    c, s = math.cos(ego[3]), math.sin(ego[3])
    out = []
    for g in rec["gt"]:
        if g["model_type"] not in sp.MODEL_BOXES or g["wrapper"][0] > 9000:  # parked in the pool
            continue
        b = sp._body(ego, g["model_type"], g["model"])
        rvx, rvy = g["v"][0] - ev[0], g["v"][1] - ev[1]
        yaw_rel = math.remainder(g["model"][3] - ego[3], 2 * math.pi)
        wx = ego[0] + b["centre"][0] * c - b["centre"][1] * s
        wy = ego[1] + b["centre"][0] * s + b["centre"][1] * c
        out.append({"id": g["node"], "type": g["model_type"], "lon": b["centre"][0], "lat": b["centre"][1],
                    "box": b["box"], "length": b["length"], "clearance": sp._box_clearance(b["box"]),
                    "v_lon": rvx * c + rvy * s, "v_lat": rvy * c - rvx * s, "same_dir": abs(yaw_rel) < 0.5,
                    "world_y": wy, "world_x": wx})
    rec["_truth"] = out
    return out


def relevant(b):
    lo, hi, lat = REGION
    return b["same_dir"] and lo <= b["lon"] <= hi and abs(b["lat"]) <= lat


def zone_of(b, ego_y):
    if not relevant(b):
        return None
    x0, x1, y0, y1 = b["box"]
    for z, (a0, a1, c0, c1) in ZONES.items():
        if x0 <= a1 and x1 >= a0 and y0 <= c1 and y1 >= c0:
            return "blind_" + z
    rel = perception.lane_relation(b["world_y"], ego_y)
    if b["lon"] > 0 and rel == "ego_lane":
        return "ego_lane_ahead" if b["lon"] <= 60 else "ego_lane_far"
    if rel in ("left_lane", "right_lane") and abs(b["lon"]) <= 30:
        return "adjacent_near"
    return "behind" if b["lon"] < 0 else "ahead_other"


def model_class(t):
    return {"MotorbikeSimple": "motorcycle", "ScooterSimple": "motorcycle", "TruckSimple": "truck",
            "BusSimple": "bus", "RoadSenseAutorickshaw": "autorickshaw"}.get(t, "car")


# ---------------------------------------------------------------------------------------------------------------
# radar clearance slack calibration (development runs only)

def calibrate(runs, out):
    groups = {"profile radar front": "front", "profile radar rear": "rear", "profile radar corner rl": "corner_rl",
              "profile radar corner rr": "corner_rr"}
    samples = {}
    for run_dir in runs:
        r = read_run(run_dir)
        for rec in r["recs"]:
            if "radars" not in rec:
                continue
            bodies = [b for b in truth(rec)]
            for name, group in groups.items():
                x, y, z, yaw, rng, hfov, _v = sp.PROFILE_RADARS[name]
                spec = radar.RadarSpec(name, group, (x, y, z, yaw), 1.0, rng, hfov, "point")
                for d, az, v, pw in rec["radars"].get(name, []):
                    m = radar.measure(spec, rec["t"], d, az, v, pw)
                    if m.flags & {"self_suspect", "range_clamped", "bearing_clamped"}:
                        continue
                    best = min(bodies, key=lambda b: sp._rect_dist(m.lon, m.lat, *b["box"]), default=None)
                    if best is None or sp._rect_dist(m.lon, m.lat, *best["box"]) > 1.5:
                        continue
                    s = radar.footprint_distance(m.lon, m.lat) - best["clearance"]
                    key = ("inline" if abs(m.lat) < 1.5 else "side") + " " + radar.band(m.range)
                    samples.setdefault(group, {}).setdefault(key, []).append(s)
                    samples[group].setdefault("all", []).append(s)
    lidar_samples = {}
    for run_dir in runs:
        r = read_run(run_dir)
        if r["lidar"] is None:
            continue
        cfg = lidar_config(r["start"], lidar.FLOAT32)
        for rec in r["recs"]:
            if "lidar" not in rec:
                continue
            n, off = rec["lidar"]["n"], rec["lidar"]["offset"] // 4
            ranges = np.asarray(r["lidar"][off:off + n], dtype=np.float64).reshape(cfg.layers, cfg.width)
            res = lidar.process(cfg, ranges, radar.EGO_BOX, attitude=tuple(rec["imu_rpy"][:2]))
            per_body = {}
            for b in truth(rec):
                x0, x1, y0, y1 = b["box"]
                for c in res["clusters"]:
                    if sp._rect_dist(c["near"][0], c["near"][1], x0 - 0.5, x1 + 0.5, y0 - 0.5, y1 + 0.5) == 0:
                        clear = radar.footprint_distance(*c["near"])
                        prev = per_body.get(b["id"])
                        if prev is None or clear < prev[0]:
                            per_body[b["id"]] = (clear, c, b)
            for clear, c, b in per_body.values():
                s_ = clear - b["clearance"]
                lidar_samples.setdefault(fusion.lidar_slack_key(c["face_low"], c["range"]), []).append(s_)
                lidar_samples.setdefault("all", []).append(s_)
    table, counts = {}, {}
    for g, bands in samples.items():
        for k, v in bands.items():
            counts["%s %s" % (g, k)] = len(v)
            if len(v) >= 40:
                table.setdefault(g, {})[k] = [round(pct(v, 0.01), 3), round(pct(v, 0.5), 3), round(pct(v, 0.99), 3)]
    lidar_table = {k: [round(pct(v, 0.01), 3), round(pct(v, 0.5), 3), round(pct(v, 0.99), 3)]
                   for k, v in lidar_samples.items() if len(v) >= 40}
    counts.update({"lidar " + k: len(v) for k, v in lidar_samples.items()})
    doc = {"schema": "roadsense.experimental-calibration.v1",
           "about": "measured clearance - true surface clearance (m), quantiles q01/q50/q99: radar by group, "
                    "geometry and range; lidar (nearest cluster point on each body) by the lowest visible face "
                    "height and range. Calibrated on W9 development runs only, never on the validation runs",
           "runs": [Path(r).name for r in runs], "samples": counts, "radar_slack_m": table,
           "lidar_slack_m": lidar_table}
    Path(out).write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1))


# ---------------------------------------------------------------------------------------------------------------
# RSSF encoding of a profile recording (what the vehicle would send)

def rssf_stream(run, variant="", lidar_cfg=None):
    """Yields (record, bytes) per radar instant: GNSS, IMU, the profile radars and (if recorded) the lidar."""
    cfg = lidar_cfg or (lidar_config(run["start"]) if run["lidar"] is not None else None)
    rng = random.Random(CORRUPT_SEED)
    pending_lidar = None
    seq = 0
    for k, rec in enumerate(r for r in run["recs"] if "radars" in r):
        t = rec["t"]
        seq += 1
        yaw = rec["imu_rpy"][2]
        sp_ = rec["gps_speed"]
        gnss = {"t": t, "position": rec["gps"], "velocity": (sp_ * math.cos(yaw), sp_ * math.sin(yaw)), "speed": sp_}
        scans = []
        for name, rets in rec["radars"].items():
            x, y, z, ryaw, rmax, hfov, vfov = sp.PROFILE_RADARS[name]
            health = transport.OK if rets else transport.NO_DATA
            if variant == "radar_front_outage" and name == "profile radar front" and _outage(run, t):
                continue
            scans.append(transport.RadarScan(name, t, health, RADAR_MOUNTS[name], (1.0, rmax, hfov, vfov),
                                             tuple(transport.RadarReturn(d, az, v, pw) for d, az, v, pw in rets)))
        frame_lidar, lidar_t, health = None, None, transport.OK
        if cfg is not None and "lidar" in rec:
            n = rec["lidar"]["n"]
            off = rec["lidar"]["offset"] // 4
            ranges = np.asarray(run["lidar"][off:off + n], dtype=np.float64).reshape(cfg.layers, cfg.width)
            data = lidar.encode_frame(cfg, ranges, k, t, compress=True)
            if variant == "lidar_outage" and _outage(run, t):
                data, health = None, transport.FAILED
            if variant == "lidar_late":
                data, pending_lidar = pending_lidar, (data, t)
                if data is not None:
                    data, lidar_t = data
            else:
                lidar_t = t
            frame_lidar = data
        elif cfg is not None:
            health = transport.NO_DATA
        include_cfg = cfg is not None and (seq == 1 or seq % transport.CONFIG_EVERY == 0)
        b = transport.encode(seq, t, gnss=gnss, imu={"t": t, "rpy": rec["imu_rpy"]}, radars=scans,
                             lidar_config=cfg if include_cfg else None, lidar_frame=frame_lidar, lidar_t=lidar_t,
                             lidar_health=health if frame_lidar is None else transport.OK)
        if variant == "corrupt_1pct" and rng.random() < 0.01:
            b = bytearray(b)
            b[rng.randrange(len(b))] ^= 0xFF
            b = bytes(b)
        yield rec, b


def _outage(run, t):
    """The outage window of a robustness variant: 10 s around the scenario's event, else the run's middle 10 s."""
    act = next((e["t"] for e in run["events"] if e.get("what") == "act"), None)
    mid = act if act is not None else run["recs"][-1]["t"] / 2
    return mid - 5.0 <= t <= mid + 5.0


# ---------------------------------------------------------------------------------------------------------------
# trackers

def original_cycles(run):
    """A/B: the authoritative pipeline, unchanged, on the frames RoadSense's own controller built."""
    pipe = pipeline.Pipeline("EGO_ROADSENSE", POLICY)
    out = []
    for line in open(run["dir"] / "frames.jsonl"):
        frame = json.loads(line)
        a = time.perf_counter()
        cycle = pipe.process(frame)
        ms = (time.perf_counter() - a) * 1e3
        vehicle = safety.decide(cycle.timestamp, frame["ego"]["speed_mps"], cycle.assessments, cycle.reports,
                                {"hazards": []}, (), POLICY)
        out.append({"t": cycle.timestamp, "reports": cycle.reports, "level": vehicle["overall_risk"],
                    "level_auth": cycle.decision["overall_risk"], "ms": {"total": ms}, "frame": frame,
                    "speed": frame["ego"]["speed_mps"]})
    return out


def experimental_cycles(run, mode, variant=""):
    conf = fusion.Config(mode, fusion.profile_radars("point" if run["name"].startswith("corr") else "origin"))
    per = fusion.ExperimentalPerception(conf)
    rx = transport.Receiver(RADAR_MOUNTS)
    out, sizes, decode_ms, rejected = [], [], [], 0
    for rec, data in rssf_stream(run, variant):
        sizes.append(len(data))
        a = time.perf_counter()
        try:
            frame = rx.accept(data)
        except transport.Rejected:
            rejected += 1
            continue
        decode_ms.append((time.perf_counter() - a) * 1e3)
        cyc = per.process(frame)
        speed = rec["gps_speed"]
        cen, _ = fusion.shadow_decision(cyc.t, speed, cyc.reports, POLICY)
        clr, _ = fusion.shadow_decision(cyc.t, speed, cyc.reports, POLICY, use_clearance=True)
        cyc.timing_ms["decode"] = decode_ms[-1]
        out.append({"t": cyc.t, "reports": cyc.reports, "level": cen["overall_risk"],
                    "level_clearance": clr["overall_risk"], "ms": cyc.timing_ms, "counts": cyc.counts,
                    "speed": speed, "age_s": max([frame.t - s.t for s in frame.radars.values()]
                                                 + ([frame.t - frame.lidar["t"]] if frame.lidar else []), default=0.0)})
    return out, {"payload_bytes": st(sizes, (0.5, 0.95, 1.0)), "decode_ms": st(decode_ms, (0.5, 0.95, 0.99)),
                 "receiver": rx.counts, "rejected_frames": rejected, "tracker": per.tracker.stats}


# ---------------------------------------------------------------------------------------------------------------
# truth decisions and scoring

def truth_report(b, t):
    return {"track_id": "T%d" % b["id"], "timestamp": t,
            "relative_position": {"longitudinal_m": b["lon"], "lateral_m": b["lat"]},
            "relative_velocity": {"longitudinal_mps": b["v_lon"], "lateral_mps": b["v_lat"]},
            "age_s": 10.0, "last_seen": t, "experimental": {"clearance_m": {"lo": b["clearance"]}}}


def oracle_levels(rec, speed, include_fresh=False):
    cache = rec.setdefault("_oracle", {})
    key = (round(speed, 3), include_fresh)
    if key not in cache:
        cache[key] = _oracle_levels(rec, speed, include_fresh)
    return cache[key]


def _oracle_levels(rec, speed, include_fresh=False):
    bodies = [b for b in truth(rec) if -100 <= b["lon"] <= 150 and (include_fresh or b["id"] not in rec["_fresh"])]
    reps = [truth_report(b, rec["t"]) for b in bodies]
    out = []
    for use in (False, True):
        rs = [fusion.clearance_view(r) for r in reps] if use else reps
        a = [risk.assess(r, prediction.predict(r)) for r in rs]
        out.append(safety.decide(rec["t"], speed, a, rs, {"hazards": []}, (), POLICY)["overall_risk"])
    return out


INSERTION_SKIP_S = 1.0


def _insertion_artefact(cyc, rec, experimental):
    """For about two frames after SUMO puts a vehicle on the road, Webots reports nonsense velocities for it (radar
    range rates of 1e5, then -26 to -53 m/s; the body's own velocity swings by 40 m/s): a simulator artefact no real
    vehicle produces. The oracle ignores such bodies for INSERTION_SKIP_S; a tracker's HIGH frame is set aside (and
    counted) when every report rating HIGH is on one of them, or is a ghost within 10 m of one."""
    bodies = truth(rec)
    culprits = []
    for r in cyc["reports"]:
        v = fusion.clearance_view(r) if experimental else r
        if LEVELS.index(risk.assess(v, prediction.predict(v))["risk"]) >= 2:
            culprits.append(r)
    for r in culprits:
        p = r["relative_position"]
        near = [b for b in bodies if sp._rect_dist(p["longitudinal_m"], p["lateral_m"], *b["box"]) <= MATCH_M]
        if any(b["id"] not in rec["_fresh"] for b in near):
            return False
        if not near and not any(b["id"] in rec["_fresh"] and math.hypot(p["longitudinal_m"] - b["lon"],
                                                                        p["lateral_m"] - b["lat"]) <= 10.0
                                for b in bodies):
            return False  # a ghost, but not one an insertion explains
    return bool(culprits)


CRASH_DECEL_MPS2 = 10.0  # beyond any brake: the ego hit something


def ego_collision_t(run):
    """When the ego crashed (its stock driving controller is not RoadSense's and does not always stop in time):
    a speed drop faster than CRASH_DECEL_MPS2 over 0.2 s. Frames from 1 s before then are not scored: the ego's
    own crash dynamics (pitch, bounce) are outside what perception is evaluated on. Drawn bodies overlapping the
    ego without a crash (the original simulator's model offsets) are kept."""
    recs = run["recs"]
    for i, a in enumerate(recs):
        b = next((r for r in recs[i + 1:] if r["t"] >= a["t"] + 0.2 - 1e-6), None)
        if b is not None and (a["gps_speed"] - b["gps_speed"]) / (b["t"] - a["t"]) > CRASH_DECEL_MPS2:
            return round(a["t"] - 1.0, 3)
    return None


def score(run, cycles, experimental):
    cut = run.get("collision_t")
    recs = {round(r["t"], 3): r for r in run["recs"] if cut is None or r["t"] < cut}
    m = {k: [] for k in ("pos_lon", "pos_lat", "vel", "clear_opt", "clear_cover", "clear_width", "point_opt")}
    zone_hits, class_hits = {}, {}
    false_tracks = frames = dup = dup_ungrouped = skipped = 0
    ids, lane_ok, lane_n, lane_unc = {}, 0, 0, 0
    first_seen, first_track, last_rel = {}, {}, {}
    levels = {"centre": [], "clearance": []}
    for cyc in cycles:
        rec = recs.get(round(cyc["t"], 3))
        if rec is None:
            continue
        frames += 1
        bodies = truth(rec)
        ego_y = rec["ego"][1]
        reps = cyc["reports"]
        cost = np.full((len(reps), len(bodies)), 1e9)
        for i, r in enumerate(reps):
            p = r["relative_position"]
            for j, b in enumerate(bodies):
                d = sp._rect_dist(p["longitudinal_m"], p["lateral_m"], *b["box"])
                if d <= MATCH_M:
                    cost[i, j] = d
        pairs = trk.assign(cost)
        matched_b = {j: i for i, j in pairs.items()}
        for i, r in enumerate(reps):
            p = r["relative_position"]
            if i not in pairs and -60 <= p["longitudinal_m"] <= 100 and abs(p["lateral_m"]) <= 12:
                near = [j for j, b in enumerate(bodies)
                        if sp._rect_dist(p["longitudinal_m"], p["lateral_m"], *b["box"]) <= MATCH_M + 1.0]
                if near:
                    dup += 1
                    if not experimental or all(reps[matched_b[j]]["experimental"]["group"] != r["experimental"]["group"]
                                               for j in near if j in matched_b):
                        dup_ungrouped += 1
                else:
                    false_tracks += 1
        for j, b in enumerate(bodies):
            if not relevant(b):
                last_rel.pop(b["id"], None)
                continue
            z = zone_of(b, ego_y)
            cls = model_class(b["type"])
            hit = j in matched_b
            zone_hits.setdefault(z, [0, 0])
            zone_hits[z][0] += hit
            zone_hits[z][1] += 1
            class_hits.setdefault(cls, [0, 0])
            class_hits[cls][0] += hit
            class_hits[cls][1] += 1
            key = b["id"]
            if key not in last_rel or cyc["t"] - last_rel[key] > 1.0:
                first_seen.setdefault(key, []).append(cyc["t"])
            last_rel[key] = cyc["t"]
            if not hit:
                continue
            r = reps[matched_b[j]]
            first_track.setdefault((key, len(first_seen[key]) - 1), cyc["t"])
            ids.setdefault(key, [])
            if not ids[key] or ids[key][-1] != r["track_id"]:
                ids[key].append(r["track_id"])
            p, v = r["relative_position"], r["relative_velocity"]
            m["pos_lon"].append(abs(p["longitudinal_m"] - b["lon"]))
            m["pos_lat"].append(abs(p["lateral_m"] - b["lat"]))
            if r["age_s"] >= 1.0:
                m["vel"].append(math.hypot(v["longitudinal_mps"] - b["v_lon"], v["lateral_mps"] - b["v_lat"]))
            c_true = b["clearance"]
            m["point_opt"].append(radar.footprint_distance(p["longitudinal_m"], p["lateral_m"]) - c_true)
            if experimental:
                cl = r["experimental"]["clearance_m"]
                m["clear_opt"].append(cl["lo"] > c_true + 0.1)
                m["clear_cover"].append(cl["lo"] - 0.1 <= c_true <= cl["hi"] + 0.1)
                m["clear_width"].append(cl["hi"] - cl["lo"])
                if r["experimental"]["lane_uncertain"]:
                    lane_unc += 1
                    continue
            lane_n += 1
            lane_ok += r["lane_relation"] == perception.lane_relation(b["world_y"], ego_y)
        oc, ocl = oracle_levels(rec, cyc["speed"])
        if rec["_fresh"] and LEVELS.index(cyc["level_clearance" if experimental else "level"]) >= 2 \
                and LEVELS.index(ocl if experimental else oc) <= 1 and _insertion_artefact(cyc, rec, experimental):
            skipped += 1
            continue
        levels["centre"].append((cyc["t"], oc, cyc["level"]))
        if experimental:
            levels["clearance"].append((cyc["t"], ocl, cyc["level_clearance"]))
    latency = [first_track[(k, e)] - t0 for k, fs in first_seen.items() for e, t0 in enumerate(fs)
               if (k, e) in first_track]
    episodes = sum(len(v) for v in first_seen.values())
    switches = sum(max(0, len(v) - 1) for v in ids.values())
    out = {"frames": frames,
           "recall_by_zone": {z: {"recall": share(h, n), "n": n} for z, (h, n) in sorted(zone_hits.items())},
           "recall_by_class": {c: {"recall": share(h, n), "n": n} for c, (h, n) in sorted(class_hits.items())},
           "false_tracks_per_frame": share(false_tracks, frames), "duplicates_per_frame": share(dup, frames),
           "duplicates_ungrouped_per_frame": share(dup_ungrouped, frames),
           "id_switches": switches, "tracked_vehicles": len(ids), "episodes": episodes,
           "never_tracked_episodes": episodes - len(latency),
           "confirm_latency_s": st(latency), "pos_err_lon_m": st(m["pos_lon"]), "pos_err_lat_m": st(m["pos_lat"]),
           "vel_err_mps": st(m["vel"]), "point_clearance_optimism_m": st(m["point_opt"]),
           "lane_accuracy": share(lane_ok, lane_n), "lane_uncertain_share": share(lane_unc, lane_n + lane_unc)}
    if experimental:
        out["clearance"] = {"optimistic_share": share(sum(m["clear_opt"]), len(m["clear_opt"])),
                            "coverage": share(sum(m["clear_cover"]), len(m["clear_cover"])),
                            "width_m": st(m["clear_width"])}
    out["safety"] = {k: dict(safety_scores(v, run), insertion_artefact_frames=skipped) for k, v in levels.items() if v}
    return out


def safety_scores(levels, run):
    hi = lambda l: LEVELS.index(l) >= 2  # noqa: E731
    o_hi = [x for x in levels if hi(x[1])]
    missed = sum(1 for x in o_hi if not hi(x[2]))
    false = sum(1 for x in levels if hi(x[2]) and not hi(x[1]) and LEVELS.index(x[1]) <= 1)
    act = next((e["t"] for e in run["events"] if e.get("what") == "act"), None)
    first = {}
    if act is not None:
        for name, idx in (("oracle", 1), ("tracker", 2)):
            first[name] = next((round(t - act, 2) for t, *ls in levels if t >= act - 5 and hi(ls[idx - 1])), None)
    return {"oracle_high_frames": len(o_hi), "missed_high_frames": missed, "false_high_frames": false,
            "first_high_after_event_s": first}


# ---------------------------------------------------------------------------------------------------------------
# warning metrics, raw and filtered (scoring only: the same trackers, nothing else changes)

def _hi(level):
    return LEVELS.index(level) >= 2


def warning_tiers(run, cycles, experimental):
    """Missed and false HIGH frames against the oracle in four tiers, with what each excludes:

    raw_all       every scored cycle, the oracle including just-inserted bodies, no frame set aside
    raw_precrash  raw_all before the crash (crash = the deceleration spike, ego_collision_t + 1 s)
    filtered      before ego_collision_t (1 s before the crash); oracle ignores bodies inserted in the last 1 s;
                  tracker HIGH frames caused only by such bodies set aside (insertion artefacts)
    last_second   the final second before the crash, filtered the same way (and its raw counts)

    A/B are rated on their reports (centre); C/D/E on the clearance view, the conservative one."""
    cut = run["collision_t"]
    crash = None if cut is None else cut + 1.0
    recs = {round(r["t"], 3): r for r in run["recs"]}
    tiers = {k: {"frames": 0, "oracle_high": 0, "missed_high": 0, "false_high": 0}
             for k in ("raw_all", "raw_precrash", "filtered", "last_second", "last_second_raw")}
    excl = {"post_crash_frames": 0, "pre_crash_second_frames": 0, "insertion_artefact_frames": 0,
            "frames_oracle_changed_by_insertions": 0, "cycles_without_record": 0}
    level_key = "level_clearance" if experimental else "level"
    oracle_i = 1 if experimental else 0

    def add(tier, o, lv):
        d = tiers[tier]
        d["frames"] += 1
        d["oracle_high"] += _hi(o)
        d["missed_high"] += _hi(o) and not _hi(lv)
        d["false_high"] += _hi(lv) and LEVELS.index(o) <= 1

    for cyc in cycles:
        rec = recs.get(round(cyc["t"], 3))
        if rec is None:
            excl["cycles_without_record"] += 1
            continue
        lv = cyc[level_key]
        o_raw = oracle_levels(rec, cyc["speed"], include_fresh=True)[oracle_i]
        o = oracle_levels(rec, cyc["speed"])[oracle_i]
        excl["frames_oracle_changed_by_insertions"] += o != o_raw
        add("raw_all", o_raw, lv)
        if crash is not None and cyc["t"] >= crash:
            excl["post_crash_frames"] += 1
            continue
        add("raw_precrash", o_raw, lv)
        last = cut is not None and cyc["t"] >= cut
        if last:
            excl["pre_crash_second_frames"] += 1
            add("last_second_raw", o_raw, lv)
        if rec["_fresh"] and _hi(lv) and LEVELS.index(o) <= 1 and _insertion_artefact(cyc, rec, experimental):
            excl["insertion_artefact_frames"] += 1
            continue
        add("last_second" if last else "filtered", o, lv)
    return {"crash_t": crash, "tiers": tiers, "excluded": excl}


def clearance_by_class(run, cycles):
    """Clearance interval vs truth per vehicle class (matched reports, before the 1 s pre-crash cut): optimistic =
    lower bound more than 0.1 m beyond the true clearance; covered = truth within [lo - 0.1, hi + 0.1]."""
    cut = run["collision_t"]
    recs = {round(r["t"], 3): r for r in run["recs"] if cut is None or r["t"] < cut}
    out = {}
    for cyc in cycles:
        rec = recs.get(round(cyc["t"], 3))
        if rec is None:
            continue
        bodies = truth(rec)
        for r in cyc["reports"]:
            p = r["relative_position"]
            near = [(sp._rect_dist(p["longitudinal_m"], p["lateral_m"], *b["box"]), b) for b in bodies]
            near = [x for x in near if x[0] <= MATCH_M]
            if not near:
                continue
            b = min(near, key=lambda x: x[0])[1]
            cl = r["experimental"]["clearance_m"]
            d = out.setdefault(model_class(b["type"]), {"n": 0, "optimistic": 0, "covered": 0, "excess": []})
            d["n"] += 1
            d["covered"] += cl["lo"] - 0.1 <= b["clearance"] <= cl["hi"] + 0.1
            if cl["lo"] > b["clearance"] + 0.1:
                d["optimistic"] += 1
                d["excess"].append((cl["lo"] - b["clearance"], b["lon"], b["lat"], cl["basis"]))
    res = {}
    for k, d in out.items():
        ex = sorted(d["excess"])
        res[k] = {"n": d["n"], "optimistic_share": share(d["optimistic"], d["n"]),
                  "coverage": share(d["covered"], d["n"]),
                  "optimism_m": st([e[0] for e in ex], (0.5, 0.95, 1.0)) if ex else None,
                  "worst": {"m": round(ex[-1][0], 2), "lon": round(ex[-1][1], 1), "lat": round(ex[-1][2], 1),
                            "basis": ex[-1][3]} if ex else None,
                  "optimistic_within_20m": sum(1 for e in ex if e[1] <= 20 + 3.89 and e[1] >= -20)}
    return res


def warnings_cmd(runs, out):
    results = {}
    for run_dir in runs:
        run = read_run(run_dir)
        run["collision_t"] = ego_collision_t(run)
        name = run["name"]
        res = {"collision_t": run["collision_t"]}
        orig = original_cycles(run)
        res["A" if name.startswith("orig") else "B"] = warning_tiers(run, orig, False)
        if name.startswith("corr"):
            for label, mode in (("C", "radar"), ("D", "lidar"), ("E", "fused")):
                cyc, _ = experimental_cycles(run, mode)
                res[label] = dict(warning_tiers(run, cyc, True), clearance_by_class=clearance_by_class(run, cyc))
        results[name] = res
        print(name, "done", flush=True)
    Path(out).write_text(json.dumps(results, indent=1) + "\n")


def demo(run_dir, t0=None, t1=None, step=0.5):
    """Local demonstration on a recording: the authoritative decision beside the shadow (fused, clearance view)
    one, the oracle, and the nearest ego-lane shadow track. Nothing is sent anywhere."""
    run = read_run(run_dir)
    auth = original_cycles(run)
    recs = {round(r["t"], 3): r for r in run["recs"]}
    cyc, info = experimental_cycles(run, "fused")
    act = next((e["t"] for e in run["events"] if e.get("what") == "act"), None)
    print("%s  event %s  (RSSF frames: %d rejected; fused cycle p95 %.1f ms)" % (
        run["name"], "act at %.1f s" % act if act is not None else "none", info["rejected_frames"],
        st([c["ms"]["total"] for c in cyc], (0.95,))["p95"]))
    print("    t  speed  authoritative  shadow     oracle     nearest ego-lane shadow track")
    last = -1e9
    for c in cyc:
        if (t0 is not None and c["t"] < t0) or (t1 is not None and c["t"] > t1) or c["t"] - last < step - 1e-6:
            continue
        last = c["t"]
        a = next((x for x in reversed(auth) if x["t"] <= c["t"] + 1e-6), None)  # its latest decision (5 Hz)
        rec = recs.get(round(c["t"], 3))
        o = oracle_levels(rec, c["speed"])[1] if rec else "-"
        lead = min((r for r in c["reports"] if r["lane_relation"] == "ego_lane"
                    and r["relative_position"]["longitudinal_m"] > 0),
                   key=lambda r: r["relative_position"]["longitudinal_m"], default=None)
        desc = ""
        if lead:
            e = lead["experimental"]
            desc = "%s %5.1f m %+5.1f m/s clear %.1f-%.1f m [%s]" % (
                lead["track_id"], lead["relative_position"]["longitudinal_m"],
                lead["relative_velocity"]["longitudinal_mps"], e["clearance_m"]["lo"], e["clearance_m"]["hi"],
                e["clearance_m"]["basis"])
        print("%5.1f  %5.1f  %-13s  %-9s  %-9s  %s" % (c["t"], c["speed"], a["level_auth"] if a else "-",
                                                      c["level_clearance"], o, desc))


def timing(cycles):
    keys = sorted({k for c in cycles for k in c["ms"]})
    return {k: st([c["ms"].get(k) for c in cycles], (0.5, 0.95, 0.99)) for k in keys}


def queue_sim(cycles, period_s=0.1, capacity=2):
    """Frames arrive every period_s; each takes its measured processing time on one worker; a bounded queue drops
    the oldest frame when full. Max queue length, drops and the age of what was processed."""
    queue, drops, max_q, ages = [], 0, 0, []
    busy_until = 0.0
    for k, c in enumerate(cycles):
        arrival = k * period_s
        while queue and busy_until <= arrival:
            a, service = queue.pop(0)
            start = max(busy_until, a)
            busy_until = start + service
            ages.append(busy_until - a)
        queue.append((arrival, c["ms"].get("total", 0.0) / 1e3))
        if len(queue) > capacity:
            queue.pop(0)
            drops += 1
        max_q = max(max_q, len(queue))
    return {"max_queue": max_q, "dropped": drops, "latency_s": st(ages, (0.5, 0.99))}


def replay(runs, out, variants):
    results = {}
    for run_dir in runs:
        run = read_run(run_dir)
        run["collision_t"] = ego_collision_t(run)
        name = run["name"]
        res = {"scenario": run["scenario"].get("variant") or run["scenario"]["family"], "run": run["info"],
               "ego_collision_t": run["collision_t"],
               "events": run["events"], "rtf": round(run["recs"][-1]["t"] / run["info"]["wall_s"], 2)}
        orig = original_cycles(run)
        key = "A" if name.startswith("orig") else "B"
        res[key] = dict(score(run, orig, False), timing_ms=timing(orig),
                        authoritative_levels={l: sum(1 for c in orig if c["level_auth"] == l) for l in LEVELS})
        if name.startswith("corr"):
            todo = [("C", "radar", ""), ("D", "lidar", ""), ("E", "fused", "")]
            todo += [("E-" + v, "fused", v) for v in variants]
            for label, mode, variant in todo:
                tracemalloc.start()
                cyc, info = experimental_cycles(run, mode, variant)
                peak = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
                res[label] = dict(score(run, cyc, True), timing_ms=timing(cyc), transport=info,
                                  python_peak_mb=round(peak / 2 ** 20, 1), queue=queue_sim(cyc),
                                  observation_age_s=st([c["age_s"] for c in cyc], (0.5, 0.99)),
                                  effective_hz=round(len(cyc) / max(1e-9, cyc[-1]["t"] - cyc[0]["t"]), 2) if cyc else None)
                print(name, label, "done", flush=True)
        results[name] = res
        print(name, "done", flush=True)
    results["_process"] = {"max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20, 1)}
    Path(out).write_text(json.dumps(results, indent=1) + "\n")


def _f(v, nd=2):
    return "—" if v is None else ("%.*f" % (nd, v) if isinstance(v, float) else str(v))


def tables(path):
    """Markdown tables from a replay result: accuracy per case, robustness variants, performance."""
    res = json.loads(Path(path).read_text())
    runs = {k: v for k, v in res.items() if not k.startswith("_")}
    cases = sorted({k.split("-", 1)[1] for k in runs})
    rows = ["| Case | Trk | Ego lane ≤60 m | Adjacent ≤30 m | Blind L/R | Behind | False /fr | Dup /fr | ID sw | "
            "Never trk | Confirm p50 s | Lon p95 m | Lat p95 m | Vel p95 m/s | Lane acc | Clear. optim. / cover | "
            "Missed / false HIGH | First HIGH vs act (oracle, trk) |", "|" + "---|" * 18]
    for case in cases:
        for pre, keys in (("orig", ("A",)), ("corr", ("B", "C", "D", "E"))):
            r = runs.get(pre + "-" + case)
            if r is None:
                continue
            for k in keys:
                x = r.get(k)
                if x is None:
                    continue
                z = x["recall_by_zone"]
                g = lambda n: _f(z.get(n, {}).get("recall"))  # noqa: E731
                cl = x.get("clearance", {})
                sf = x["safety"].get("clearance") or x["safety"].get("centre")
                fh = sf["first_high_after_event_s"]
                rows.append("| %s | %s | %s | %s | %s/%s | %s | %s | %s | %d | %d/%d | %s | %s | %s | %s | %s | %s | "
                            "%d / %d (of %d) | %s |" % (
                                case, k, g("ego_lane_ahead"), g("adjacent_near"), g("blind_left"), g("blind_right"),
                                g("behind"), _f(x["false_tracks_per_frame"]),
                                _f(x["duplicates_ungrouped_per_frame"]), x["id_switches"],
                                x["never_tracked_episodes"], x["episodes"], _f(x["confirm_latency_s"].get("p50")),
                                _f(x["pos_err_lon_m"].get("p95")), _f(x["pos_err_lat_m"].get("p95")),
                                _f(x["vel_err_mps"].get("p95")), _f(x["lane_accuracy"]),
                                "%s / %s" % (_f(cl.get("optimistic_share")), _f(cl.get("coverage"))) if cl else "—",
                                sf["missed_high_frames"], sf["false_high_frames"], sf["oracle_high_frames"],
                                "%s, %s" % (_f(fh.get("oracle")), _f(fh.get("tracker"))) if fh else "—"))
    print("\n".join(rows) + "\n")
    rows = ["| Case | Variant | Ego lane ≤60 m | Adjacent ≤30 m | Blind L/R | False /fr | Clear. optim. | "
            "Missed / false HIGH | Receiver drops |", "|" + "---|" * 9]
    for case in cases:
        r = runs.get("corr-" + case, {})
        for k in sorted(x for x in r if x == "E" or x.startswith("E-")):
            x = r[k]
            z = x["recall_by_zone"]
            g = lambda n: _f(z.get(n, {}).get("recall"))  # noqa: E731
            sf = x["safety"]["clearance"]
            rows.append("| %s | %s | %s | %s | %s/%s | %s | %s | %d / %d | %s |" % (
                case, k, g("ego_lane_ahead"), g("adjacent_near"), g("blind_left"), g("blind_right"),
                _f(x["false_tracks_per_frame"]), _f(x["clearance"]["optimistic_share"]), sf["missed_high_frames"],
                sf["false_high_frames"], "%d rejected; %s" % (x["transport"]["rejected_frames"], json.dumps(x["transport"]["receiver"].get("dropped_sections", {})))))
    print("\n".join(rows) + "\n")
    rows = ["| Case | Trk | Total ms p50/p95/p99 | Stages p95 ms | Peak MB | Queue max / drops | Obs. age p99 s | Hz |",
            "|" + "---|" * 8]
    for case in cases:
        for pre, keys in (("orig", ("A",)), ("corr", ("B", "C", "D", "E"))):
            x = runs.get(pre + "-" + case, {})
            for k in keys:
                y = x.get(k)
                if y is None:
                    continue
                t = y["timing_ms"]
                tot = t.get("total", {})
                stages = ", ".join("%s %s" % (n, _f(v.get("p95"))) for n, v in t.items() if n != "total")
                rows.append("| %s | %s | %s/%s/%s | %s | %s | %s | %s | %s |" % (
                    case, k, _f(tot.get("p50")), _f(tot.get("p95")), _f(tot.get("p99")), stages,
                    _f(y.get("python_peak_mb")), "%s / %s" % (y["queue"]["max_queue"], y["queue"]["dropped"])
                    if "queue" in y else "—", _f(y.get("observation_age_s", {}).get("p99")),
                    _f(y.get("effective_hz"))))
    print("\n".join(rows))


def warning_tables(path):
    res = json.loads(Path(path).read_text())
    cases = sorted({k.split("-", 1)[1] for k in res})
    fmt = lambda d, dt: "%d / %d (%.1f / %.1f s) of %d" % (  # noqa: E731
        d["missed_high"], d["false_high"], d["missed_high"] * dt, d["false_high"] * dt, d["oracle_high"])
    rows = ["| Case | Trk | Filtered: missed / false HIGH frames (seconds) of oracle HIGH | Raw before crash | "
            "Raw, all frames | Final second before crash (filtered; raw) | Excluded: post-crash / final second / "
            "insertion artefacts / oracle frames changed |", "|" + "---|" * 7]
    for case in cases:
        for pre, keys in (("orig", "A"), ("corr", "BCDE")):
            r = res.get(pre + "-" + case)
            if r is None:
                continue
            for k in keys:
                x = r.get(k)
                if x is None:
                    continue
                dt = 0.2 if k in "AB" else 0.1
                t, e = x["tiers"], x["excluded"]
                ls = "%s; %s" % (fmt(t["last_second"], dt), fmt(t["last_second_raw"], dt)) \
                    if x["crash_t"] is not None else "no crash"
                rows.append("| %s | %s | %s | %s | %s | %s | %d / %d / %d / %d |" % (
                    case, k, fmt(t["filtered"], dt), fmt(t["raw_precrash"], dt), fmt(t["raw_all"], dt), ls,
                    e["post_crash_frames"], e["pre_crash_second_frames"], e["insertion_artefact_frames"],
                    e["frames_oracle_changed_by_insertions"]))
    print("\n".join(rows) + "\n")
    rows = ["| Case | Trk | Class | Matched frames | Optimistic | Coverage | Optimism p50 / p95 / max m | "
            "Optimistic within 20 m | Worst (m at lon, lat; basis) |", "|" + "---|" * 9]
    for case in cases:
        r = res.get("corr-" + case, {})
        for k in "CDE":
            for cls, d in sorted(r.get(k, {}).get("clearance_by_class", {}).items()):
                o = d["optimism_m"]
                w = d["worst"]
                rows.append("| %s | %s | %s | %d | %s | %s | %s | %d | %s |" % (
                    case, k, cls, d["n"], _f(d["optimistic_share"]), _f(d["coverage"]),
                    "%s / %s / %s" % (_f(o["p50"]), _f(o["p95"]), _f(o["p100"])) if o else "—",
                    d["optimistic_within_20m"],
                    "%.2f at %.1f, %.1f; %s" % (w["m"], w["lon"], w["lat"], w["basis"]) if w else "—"))
    print("\n".join(rows))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("runs", nargs="+")
    c.add_argument("--out", required=True)
    r = sub.add_parser("replay")
    r.add_argument("runs", nargs="+")
    r.add_argument("--out", required=True)
    r.add_argument("--variants", nargs="*", default=["radar_front_outage", "lidar_outage", "lidar_late", "corrupt_1pct"])
    w = sub.add_parser("warnings")
    w.add_argument("runs", nargs="+")
    w.add_argument("--out", required=True)
    dm = sub.add_parser("demo")
    dm.add_argument("run")
    dm.add_argument("--from", dest="t0", type=float)
    dm.add_argument("--to", dest="t1", type=float)
    t = sub.add_parser("tables")
    t.add_argument("results")
    t.add_argument("warnings", nargs="?")
    a = ap.parse_args()
    if a.cmd == "calibrate":
        calibrate(a.runs, a.out)
    elif a.cmd == "demo":
        demo(a.run, a.t0, a.t1)
    elif a.cmd == "warnings":
        warnings_cmd(a.runs, a.out)
    elif a.cmd == "tables":
        tables(a.results)
        if a.warnings:
            print()
            warning_tables(a.warnings)
    else:
        replay(a.runs, a.out, a.variants)


if __name__ == "__main__":
    main()
