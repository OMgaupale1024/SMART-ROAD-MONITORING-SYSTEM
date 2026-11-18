"""Phase 7A.3 Stage 12: tracking failures traced from raw radar targets to the safety decision (offline, diagnostic).

    PYTHONPATH=src python3 tools/track_failures.py geometry OUT.json CAMPAIGN [CAMPAIGN ...]
    PYTHONPATH=src python3 tools/track_failures.py trace    CAMPAIGN OUT_DIR [--tracker NAME] [--consistent GEOMETRY.json]
    PYTHONPATH=src python3 tools/track_failures.py analyse  CAMPAIGN TRACE_DIR ORACLE_REPLAY_DIR OUT.json

Simulator truth only pairs radar targets with vehicles and scores the results; no tracker reads it.

How Webots builds the traffic (its sumo_supervisor: Objects.py, SumoSupervisor.py): each SUMO vehicle is a Solid at
the SUMO centre (front bumper less half the SUMO length); its model is a child moved back along the heading by
MODEL_BACK_M, and a model's origin is its rear axle, its body box ahead of that (BODY_M, from the *Simple PROTOs).
ground_truth.jsonl records the Solid (as its front bumper); the audits call the Solid's position the vehicle centre.

geometry: each raw target (filtered as the tracker filters it) is paired with the one vehicle whose body axis it lies
near, and two points on that axis (Solid + k * heading) are solved for: the point at the measured range from the
radar (k_r) and the point on the ray of the measured azimuth (k_a). Medians per type and per vehicle show what each
quantity refers to. Then where a detection lands relative to the range point, and the lateral velocity that alone
puts on a vehicle driving straight (noise-free, from truth and the fitted points).
trace: tracker_replay's replay (the same slots, decisions and tracks files) plus, per cycle, every detection with the
vehicle it belongs to and the track that took it. --consistent is an EVALUATION-ONLY counterfactual: each paired
target's azimuth is turned to its range point (measured azimuth plus the truth difference between the two points'
azimuths, so the noise is kept): what the trackers would do if both quantities referred to one point.
analyse: phantom lead vehicles, stale and duplicate tracks, lane assignment, stationary vehicles, radar handover,
uncertainty while coasting and warnings the oracle does not support, each traced to a cause.
"""
import argparse
import gzip
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perception_audit as pa  # noqa: E402
import tracker_replay as tr  # noqa: E402
from roadsense.autonomy import perception  # noqa: E402

MODEL_BACK_M = {"car": 2.85, "autorickshaw": 2.85, "bus": 4.6, "truck": 5.2, "motorcycle": 1.7}
BODY_M = {"car": (-0.9, 4.55), "autorickshaw": (-0.9, 4.55), "bus": (-3.4, 6.35), "truck": (-0.7, 7.0),
          "motorcycle": (0.2, 1.4)}  # body box along the model's axis, from its rear axle
PAIR_ACROSS_M = 2.0  # a target this close across a body axis, and within the body's length +- 1 m, is a candidate
PAIR_SLACK_M = 1.0
SOURCES = ("radar_front", "radar_rear")


def axis(v):
    """(Solid x, y), heading unit vector, (rear, front) of the body along the heading from the Solid, m."""
    back, (rear, front) = MODEL_BACK_M.get(v["type"], 2.85), BODY_M.get(v["type"], BODY_M["car"])
    return v["centre"], (math.cos(v["yaw"]), math.sin(v["yaw"])), (rear - back, front - back)


def body_centre(v):
    (ox, oy), (hx, hy), (rear, front) = axis(v)
    k = (rear + front) / 2
    return ox + k * hx, oy + k * hy


def pair(x, y, vehicles, across_m=PAIR_ACROSS_M, slack_m=PAIR_SLACK_M):
    """(vehicle, k along its axis) of the one vehicle whose body a world point lies near; None if none, or if two
    lie about equally near."""
    hits = []
    for v in vehicles:
        (ox, oy), (hx, hy), (rear, front) = axis(v)
        dx, dy = x - ox, y - oy
        k, across = dx * hx + dy * hy, abs(dy * hx - dx * hy)
        if across <= across_m and rear - slack_m <= k <= front + slack_m:
            hits.append((across + max(rear - k, k - front, 0.0), k, v))
    hits.sort(key=lambda h: h[0])
    if not hits or (len(hits) > 1 and hits[0][0] > 0.5 * hits[1][0]):
        return None
    return hits[0][2], hits[0][1]


def reference_points(m, phi, r, v):
    """(k_r, k_a) on the vehicle's axis from its Solid: the point at range r from the radar m, and the point on the ray
    from m at world angle phi. None where the geometry can't tell: k_r with the axis nearly across the line of sight,
    k_a with it nearly along."""
    (ox, oy), (hx, hy), _ = axis(v)
    dx, dy = ox - m[0], oy - m[1]
    ux, uy = math.cos(phi), math.sin(phi)
    k_r = k_a = None
    if abs(hx * ux + hy * uy) >= 0.5:
        hd = hx * dx + hy * dy
        disc = hd * hd - (dx * dx + dy * dy) + r * r
        if disc >= 0:
            k_r = min((-hd + math.sqrt(disc), -hd - math.sqrt(disc)), key=abs)  # the other root is ~2r away
    s = hx * uy - hy * ux
    if abs(s) >= 0.15:
        k_a = -(dx * uy - dy * ux) / s
    return k_r, k_a


def targets(frame, pose):
    """(radar name, its world (x, y), world angle, distance, range rate) of each target the tracker would use."""
    for name, (_, mx, my, myaw) in perception.RADARS.items():
        radar = frame["radars"][name]
        lo, hi, fov = radar["limits"]
        m = perception.ego_to_world(mx, my, pose)
        for distance, azimuth, speed in radar["targets"]:
            if lo < distance < hi and abs(azimuth) <= fov / 2 - perception.EDGE_MARGIN_RAD:
                yield name, m, pose[2] + myaw - azimuth, distance, speed


def frames_with_truth(campaign):
    for r in tr.runs(campaign):
        run = campaign / "runs" / r["run_id"]
        perception.use_road(r["profile"])
        truth = pa.truth_frames(run / "ground_truth.jsonl")
        for line in open(run / "sensor_frames.jsonl"):
            f = json.loads(line)
            t, ego = round(f["timestamp"], 3), f["ego"]
            yield r["run_id"], f, t, (ego["position"][0], ego["position"][1], ego["yaw_rad"]), \
                truth.get(round(t, 1), (None, []))[1]


def robust(values):
    if not values:
        return None
    med = statistics.median(values)
    ordered = sorted(values)
    return {"median": round(med, 3), "robust_sigma": round(1.4826 * statistics.median(abs(v - med) for v in values), 3),
            "p05": round(ordered[int(0.05 * (len(ordered) - 1))], 3), "p95": round(ordered[int(0.95 * (len(ordered) - 1))], 3),
            "n": len(values)}


def share(values, test):
    return round(sum(1 for v in values if test(v)) / len(values), 4) if values else None


def ego_axes(yaw):
    return (math.cos(yaw), math.sin(yaw)), (-math.sin(yaw), math.cos(yaw))


def lon_band(d):
    return next((name for top, name in ((10, "00-10 m"), (20, "10-20 m"), (40, "20-40 m"), (80, "40-80 m")) if d < top),
                "80+ m")


def geometry(out, campaigns):
    samples = []  # (run, vehicle, radar, t, pose, mount, world angle, distance, range rate, k_r, k_a)
    for campaign in campaigns:
        for run_id, f, t, pose, vehicles in frames_with_truth(campaign):
            for name, m, phi, r, rr in targets(f, pose):
                p = pair(m[0] + r * math.cos(phi), m[1] + r * math.sin(phi), vehicles)
                if p is not None:
                    samples.append((run_id, p[0], name, t, pose, m, phi, r, rr) + reference_points(m, phi, r, p[0]))
    kinds = defaultdict(lambda: ([], []))
    per_vehicle = defaultdict(lambda: ([], []))
    for s in samples:
        v = s[1]
        for i, k in enumerate(s[9:11]):
            if k is not None:
                kinds[v["type"]][i].append(k)
                per_vehicle[(s[0], v["id"])][i].append(k)
    fit = {kind: {"k_range_m": robust(kr), "k_azimuth_m": robust(ka)} for kind, (kr, ka) in sorted(kinds.items())}
    K = {kind: (f["k_range_m"]["median"], f["k_azimuth_m"]["median"]) for kind, f in fit.items()
         if f["k_range_m"] and f["k_azimuth_m"]}
    vehicles_fit = defaultdict(lambda: ([], []))
    type_of = {(s[0], s[1]["id"]): s[1]["type"] for s in samples}
    for key, (kr, ka) in per_vehicle.items():
        if len(kr) >= 10:
            vehicles_fit[type_of[key]][0].append(statistics.median(kr))
        if len(ka) >= 10:
            vehicles_fit[type_of[key]][1].append(statistics.median(ka))
    # range timing: range residual about each vehicle's own range point against the measured range rate
    own = {key: statistics.median(kr) for key, (kr, _) in per_vehicle.items() if len(kr) >= 10}
    xs, ys = [], []
    for s in samples:
        key = (s[0], s[1]["id"])
        if key in own and s[8] is not None and s[9] is not None:
            (ox, oy), (hx, hy), _ = axis(s[1])
            bx, by = ox + own[key] * hx, oy + own[key] * hy
            xs.append(s[8])
            ys.append(s[7] - math.hypot(bx - s[5][0], by - s[5][1]))
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    # effects: where detections land relative to the range point, measured and predicted (noise-free, fitted points)
    table = defaultdict(lambda: {"measured_out": [], "predicted_out": [], "measured_lane_cross": 0,
                                 "predicted_lane_cross": 0, "n": 0})
    tracks = defaultdict(list)  # (run, vehicle, radar) -> (t, predicted lateral offset, measured, steady)
    for s in samples:
        run_id, v, name, t, pose, m, phi, r, rr = s[:9]
        if v["type"] not in K:
            continue
        (ox, oy), (hx, hy), _ = axis(v)
        kr, ka = K[v["type"]]
        bx, by = ox + kr * hx, oy + kr * hy
        ax_, ay_ = ox + ka * hx, oy + ka * hy
        rb, pa_ = math.hypot(bx - m[0], by - m[1]), math.atan2(ay_ - m[1], ax_ - m[0])
        px, py = m[0] + rb * math.cos(pa_), m[1] + rb * math.sin(pa_)  # predicted, noise-free
        qx, qy = m[0] + r * math.cos(phi), m[1] + r * math.sin(phi)  # measured
        ex, ey = ego_axes(pose[2])
        lat_b = (bx - pose[0]) * ey[0] + (by - pose[1]) * ey[1]
        lon_b = (bx - pose[0]) * ex[0] + (by - pose[1]) * ex[1]
        side = 1.0 if lat_b >= 0 else -1.0
        pred = ((px - bx) * ey[0] + (py - by) * ey[1])
        meas = ((qx - bx) * ey[0] + (qy - by) * ey[1])
        lane = "same lane" if abs(lat_b) < 1.9 else "adjacent" if abs(lat_b) < 5.6 else "two over" if abs(lat_b) < 9.4 else "beyond"
        direction = "same direction" if math.cos(v["yaw"] - pose[2]) > 0 else "oncoming"
        mount_lon = perception.RADARS[name][1]
        key = "%s | %s | %s | %s" % (name, direction, lane, lon_band(abs(lon_b - mount_lon)))
        row = table[key]
        row["measured_out"].append(meas * side)
        row["predicted_out"].append(pred * side)
        row["measured_lane_cross"] += perception.lane_index(qy) != perception.lane_index(by)
        row["predicted_lane_cross"] += perception.lane_index(py) != perception.lane_index(by)
        row["n"] += 1
        steady = abs(v["v"][1]) < 0.1  # the carriageway runs along x: world vy is the speed across it
        tracks[(run_id, v["id"], name)].append((t, pred, meas, steady, side, direction, abs(lon_b - mount_lon)))
    rates = defaultdict(lambda: {"cycle": [], "window": [], "measured_vs_predicted": defaultdict(list)})
    for series in tracks.values():
        series.sort()
        at = {round(x[0], 1): x for x in series}
        for t, pred, meas, steady, side, direction, dist in series:
            if not steady:
                continue
            band = "%s, %s" % (direction, lon_band(dist))
            prev = at.get(round(t - 0.2, 1))
            if prev is not None and prev[3]:
                rate = (pred - prev[1]) / 0.2
                rates[band]["cycle"].append(rate * side)  # positive: away from the ego's axis
                rates[band]["measured_vs_predicted"]["%+.2f" % (round(rate * side * 4) / 4)].append((meas - prev[2]) / 0.2 * side)
            back = at.get(round(t - 1.6, 1))
            if back is not None and back[3]:
                rates[band]["window"].append((pred - back[1]) / 1.6 * side)
    result = {
        "schema": "roadsense.radar-geometry.v1", "campaigns": [c.name for c in campaigns], "truth": "evaluation only",
        "paired_targets": len(samples),
        "model_back_m": MODEL_BACK_M, "body_from_rear_axle_m": BODY_M,
        "fit": {"by_type": fit, "by_type_median": K,
                "per_vehicle_medians": {kind: {"k_range": robust(kr), "k_azimuth": robust(ka)}
                                        for kind, (kr, ka) in sorted(vehicles_fit.items())},
                "range_residual_vs_range_rate_s": round(slope, 4), "range_residual_samples": len(xs)},
        "lateral_offset_from_range_point_m": {
            key: {"n": row["n"], "measured_outward": robust(row["measured_out"]),
                  "predicted_outward": robust(row["predicted_out"]),
                  "lane_changed_measured": round(row["measured_lane_cross"] / row["n"], 4),
                  "lane_changed_predicted": round(row["predicted_lane_cross"] / row["n"], 4)}
            for key, row in sorted(table.items()) if row["n"] >= 20},
        "geometric_lateral_velocity_steady_vehicles_mps": {
            band: {"one_cycle_outward": robust(r["cycle"]), "share_over_0_3_one_cycle": share(r["cycle"], lambda x: abs(x) > 0.3),
                   "window_1_6s_outward": robust(r["window"]), "share_over_0_3_window": share(r["window"], lambda x: abs(x) > 0.3),
                   "measured_median_by_predicted": {p: round(statistics.median(m), 3) for p, m in
                                                    sorted(r["measured_vs_predicted"].items(), key=lambda kv: float(kv[0]))
                                                    if len(m) >= 30}}
            for band, r in sorted(rates.items())}}
    Path(out).write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result["fit"], indent=1))


def body_truth(K):
    """pa.truth_frames with each vehicle's centre moved to its body centre (the fitted range point), EVALUATION ONLY."""
    original = pa.truth_frames

    def frames(path):
        out = original(path)
        for _, vehicles in out.values():
            for v in vehicles:
                k = K[v["type"]][0]
                v["centre"] = (v["centre"][0] + k * math.cos(v["yaw"]), v["centre"][1] + k * math.sin(v["yaw"]))
        return out
    return frames


def consistent_frame(f, pose, vehicles, K):
    """EVALUATION ONLY: each paired target's azimuth turned to its range point; unpaired ones are left as they are."""
    for name, (_, mx, my, myaw) in perception.RADARS.items():
        radar = f["radars"][name]
        lo, hi, fov = radar["limits"]
        m = perception.ego_to_world(mx, my, pose)
        fixed = []
        for distance, azimuth, speed in radar["targets"]:
            if lo < distance < hi and abs(azimuth) <= fov / 2 - perception.EDGE_MARGIN_RAD:
                phi = pose[2] + myaw - azimuth
                p = pair(m[0] + distance * math.cos(phi), m[1] + distance * math.sin(phi), vehicles)
                if p is not None and p[0]["type"] in K:
                    (ox, oy), (hx, hy), _ = axis(p[0])
                    kr, ka = K[p[0]["type"]]
                    to_b = math.atan2(oy + kr * hy - m[1], ox + kr * hx - m[0])
                    to_a = math.atan2(oy + ka * hy - m[1], ox + ka * hx - m[0])
                    azimuth -= math.remainder(to_b - to_a, math.tau)
            fixed.append([distance, azimuth, speed])
        radar["targets"] = fixed
    return f


def slot_tracks(reports):
    """{slot: track id} chosen as history.features chooses: nearest along the road, then the lowest id."""
    out = {}
    for slot, lane, ahead in pa.SLOTS:
        best = None
        for r in reports:
            lon = r["relative_position"]["longitudinal_m"]
            if r["lane_relation"] == lane and (ahead is None or (lon >= 0) == ahead):
                key = (abs(lon), r["track_id"])
                if best is None or key < best[0]:
                    best = (key, r["track_id"])
        out[slot] = best and best[1]
    return out


def trace(campaign, out_dir, tracker, consistent=None):
    from roadsense.web import edge, history
    K = {k: tuple(v) for k, v in json.load(open(consistent))["fit"]["by_type_median"].items()} if consistent else None
    out_dir.mkdir(parents=True)
    with open(out_dir / "slots.jsonl", "w") as slots, open(out_dir / "decisions.jsonl", "w") as decisions, \
            gzip.open(out_dir / "tracks.jsonl.gz", "wt") as tracks_out, gzip.open(out_dir / "cycles.jsonl.gz", "wt") as cycles:
        for r in tr.runs(campaign):
            perception.use_road(r["profile"])
            truth = pa.truth_frames(campaign / "runs" / r["run_id"] / "ground_truth.jsonl")
            run, serial = None, [0]
            for line in tr.read_frames(campaign / "runs" / r["run_id"] / "sensor_frames.jsonl"):
                f = json.loads(line)
                t = round(f["timestamp"], 3)
                pose = (f["ego"]["position"][0], f["ego"]["position"][1], f["ego"]["yaw_rad"])
                vehicles = truth.get(round(t, 1), (None, []))[1]
                if K:
                    f = consistent_frame(f, pose, vehicles, K)
                    line = json.dumps(f)  # floats round-trip exactly
                frame = edge.SensorFrame.model_validate_json(line)
                if run is None:
                    run = edge.EdgeRun(frame.ego.vehicle_id)
                    run.pipeline.tracker = tr.make_tracker(tracker)
                state = run.update(frame)
                seq = frame.simulation.sequence
                key = {"run_id": r["run_id"], "sequence": seq, "sim_s": state["timestamp"]}
                slots.write(json.dumps(dict(key, features=history.features(state, None, 0, None)), separators=(",", ":")) + "\n")
                decisions.write(json.dumps(dict(key, decision=history.decision_values(state)), separators=(",", ":")) + "\n")
                tracks_out.write(json.dumps(dict(key, tracks=state.get("tracks")), separators=(",", ":")) + "\n")
                if run.processed is None:  # an end-of-run marker: the tracker did not run
                    continue
                dets = []
                for name in perception.RADARS:
                    radar = f["radars"][name]
                    dets.extend(perception.radar_detections(name, radar["targets"], radar["limits"], t, pose))
                index = {(d.world_x, d.world_y): j for j, d in enumerate(dets)}
                lanes = {rep["track_id"]: rep["lane_relation"] for rep in state["tracks"]}
                rows = []
                for track in run.pipeline.tracker.tracks:
                    n = getattr(track, "_trace", None)
                    if n is None:
                        serial[0] += 1
                        n = track._trace = serial[0]
                    j = index.get(tuple(track.history[-1][1:])) if abs(track.last_seen - t) < 1e-9 else None
                    x, y = track.position_at(t)
                    vel = track.velocity
                    cov = None
                    if hasattr(track, "predicted"):
                        p = track.predicted(t)[1]
                        cov = [round(p[0][0], 4), round(p[0][1], 4), round(p[1][1], 4)]
                    rows.append([n, track.track_id, round(x, 3), round(y, 3), vel and round(vel[0], 3),
                                 vel and round(vel[1], 3), round(track.first_seen, 3), round(track.last_seen, 3),
                                 -1 if j is None else j, lanes.get(track.track_id), cov, getattr(track, "status", None)])
                det_rows = []
                for d in dets:
                    p = pair(d.world_x, d.world_y, vehicles)
                    det_rows.append([SOURCES.index(d.source), round(d.world_x, 3), round(d.world_y, 3),
                                     p and p[0]["id"], p and round(p[1], 2)])
                dv = history.decision_values(state) or {}
                cycles.write(json.dumps({"run": r["run_id"], "seq": seq, "t": t,
                                         "ego": [round(pose[0], 3), round(pose[1], 3), round(pose[2], 5),
                                                 round(f["ego"]["speed_mps"], 3)],
                                         "dets": det_rows, "tracks": rows, "slots": slot_tracks(state["tracks"]),
                                         "decision": [dv.get("risk"), dv.get("action"), dv.get("threat_id"),
                                                      dv.get("threat_ttc_s")]}, separators=(",", ":")) + "\n")
    (out_dir / "replay.json").write_text(json.dumps({"tracker": tracker, "campaign": campaign.name,
                                                     "consistent_geometry": bool(K)}, indent=1) + "\n")
    print(out_dir)


def expiries(campaign, out, tracker):
    """Every track a P4 tracker expired, with its reason and zone, set against truth (evaluation only): the true
    vehicle under the track's last predicted position, whether that vehicle was still observable, and whether a
    radar detected it again within REDETECT_S, which makes the expiry premature."""
    from roadsense.web import edge
    rows = []
    for r in tr.runs(campaign):
        perception.use_road(r["profile"])
        truth = pa.truth_frames(campaign / "runs" / r["run_id"] / "ground_truth.jsonl")
        run, seen = None, defaultdict(list)
        for line in tr.read_frames(campaign / "runs" / r["run_id"] / "sensor_frames.jsonl"):
            frame = edge.SensorFrame.model_validate_json(line)
            if run is None:
                run = edge.EdgeRun(frame.ego.vehicle_id)
                run.pipeline.tracker = tr.make_tracker(tracker)
            run.update(frame)
            if run.processed is None:
                continue
            f = json.loads(line)
            t = round(f["timestamp"], 3)
            pose = (f["ego"]["position"][0], f["ego"]["position"][1], f["ego"]["yaw_rad"])
            ego, vehicles = truth.get(round(t, 1), (None, []))
            for name in perception.RADARS:
                radar = f["radars"][name]
                for d in perception.radar_detections(name, radar["targets"], radar["limits"], t, pose):
                    p = pair(d.world_x, d.world_y, vehicles)
                    if p:
                        seen[p[0]["id"]].append(t)
            vis = pa.visibility(ego, vehicles) if ego else {}
            for track, reason in run.pipeline.tracker.expired:
                x, y = track.position_at(t)
                p = pair(x, y, vehicles, across_m=2.5, slack_m=2.5)
                v = p and p[0]
                lon, lat = perception.world_to_ego(x, y, pose)
                rows.append({"run": r["run_id"], "t": t, "track_id": track.track_id, "reason": reason,
                             "zone": getattr(track, "zone", None), "lon": round(lon, 1), "lat": round(lat, 1),
                             "unseen_s": round(t - track.last_seen, 2), "age_s": round(t - track.first_seen, 1),
                             "speed": round(math.hypot(*track.velocity), 1), "vehicle": v and v["id"],
                             "type": v and v["type"], "visible": v and vis.get(v["id"]), "id_track": id(track)})
        for row in rows:
            if row["run"] == r["run_id"] and row["vehicle"]:
                row["redetected_s"] = next((round(u - row["t"], 2) for u in seen[row["vehicle"]] if 0 < u - row["t"] <= REDETECT_S + 1e-9), None)
    for row in rows:
        row.pop("id_track")
    summary = Counter()
    for row in rows:
        kind = ("no vehicle" if not row["vehicle"] else
                "premature (re-detected within %.0f s)" % REDETECT_S if row.get("redetected_s") is not None else
                "vehicle not re-detected")
        summary["%s | %s" % (row["reason"], kind)] += 1
        summary["%s | %s | zone %s" % (row["reason"], kind, row["zone"])] += 1
    result = {"schema": "roadsense.p4-expiries.v1", "tracker": tracker, "campaign": campaign.name,
              "redetect_s": REDETECT_S, "expiries": len(rows), "summary": dict(sorted(summary.items())),
              "rows": rows, "truth": "evaluation only"}
    out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result["summary"], indent=1))


REDETECT_S = 1.0
LEVELS = ("SAFE", "CAUTION", "HIGH", "CRITICAL")
FRONT_REACH_M = 150.0 + perception.RADARS["radar front"][1]  # the front radar's 150 m from its mount, ego frame
TRUTH_WINDOW_M = 145.0  # ground_truth.jsonl keeps vehicles within 150 m of the ego (along x); beyond is unknown


def covered(lon, lat):
    """'front' or 'rear' if an ego-frame point is inside that radar's range and field of view, else why not."""
    for name, (source, mx, my, myaw) in perception.RADARS.items():
        dx, dy = lon - mx, lat - my
        r = math.hypot(dx, dy)
        off = abs(math.remainder(math.atan2(dy, dx) - myaw, math.tau))
        if 1.0 < r < (150.0 if source == "radar_front" else 100.0) and off <= 1.3 - perception.EDGE_MARGIN_RAD:
            return source
    return "blind zone alongside" if abs(lon) < 15 and abs(lat) < 12 else "beyond range"


def pct(values, q):
    return None if not values else round(sorted(values)[min(len(values) - 1, int(q * len(values)))], 3)


def band(value, edges):
    return next(("<%g" % e for e in edges if value < e), ">=%g" % edges[-1])


def analyse(campaign, trace_dir, oracle_dir, out):
    """Every failure category, per frame and per track, traced to its cause (truth only scores; see the docstring)."""
    import itertools
    oracle = {(r["run_id"], r["sequence"]): r["decision"] for r in map(json.loads, open(oracle_dir / "decisions.jsonl"))}
    profiles = {r["run_id"]: r["profile"] for r in tr.runs(campaign)}
    n = Counter()  # plain counters
    lead = Counter()
    stale_runs, coast = [], Counter()
    dup = Counter()
    lanes = Counter()
    stationary = defaultdict(Counter)
    radial = Counter()
    handover = Counter()
    uncertainty = defaultdict(list)
    unsupported = Counter()
    missed = Counter()
    onsets = {"CAUTION": [], "HIGH": [], "CRITICAL": []}
    onset_missed = Counter()
    examples = defaultdict(list)
    frames_out = []

    def example(kind, item):
        if len(examples[kind]) < 3:
            examples[kind].append(item)

    for run_id, cycles in itertools.groupby(map(json.loads, gzip.open(trace_dir / "cycles.jsonl.gz", "rt")),
                                            key=lambda c: c["run"]):
        perception.use_road(profiles[run_id])
        truth = pa.truth_frames(campaign / "runs" / run_id / "ground_truth.jsonl")
        owner_of, seen_owners, born_unpaired, alive_prev = {}, defaultdict(Counter), set(), {}
        stale_since = {}
        vehicle_dets = defaultdict(list)  # vehicle -> (t, source, track serial)
        risks = []  # (t, tracker level, oracle level)
        for c in cycles:
            t = c["t"]
            if round(t, 1) not in truth:
                n["cycles without truth"] += 1
                continue
            n["cycles"] += 1
            ego_t, vehicles = truth[round(t, 1)]
            vmap = {v["id"]: v for v in vehicles}
            rel = pa.relative(ego_t, vehicles)
            vis = pa.visibility(ego_t, vehicles)
            pose = tuple(c["ego"][:3])
            ex, ey = ego_axes(pose[2])
            ego_v = ego_t["v"]
            dets = c["dets"]

            def ego_frame(x, y):
                return (x - pose[0]) * ex[0] + (y - pose[1]) * ex[1], (x - pose[0]) * ey[0] + (y - pose[1]) * ey[1]

            rows = {}
            for row in c["tracks"]:
                serial, tid, x, y, vx, vy, first, last, j, lane, cov, status = row
                if j >= 0 and dets[j][3]:
                    owner_of[serial] = dets[j][3]
                    seen_owners[serial][dets[j][3]] += 1
                    v = next((v for v in vehicles if v["id"] == dets[j][3]), None)
                    vehicle_dets[dets[j][3]].append((t, dets[j][0], serial, tid is not None, v is not None and
                                                     math.cos(v["yaw"] - pose[2]) < 0))
                if abs(first - t) < 1e-9 and (j < 0 or not dets[j][3]):
                    born_unpaired.add(serial)
                owner = owner_of.get(serial)
                on = owner in vmap and pair(x, y, [vmap[owner]], across_m=2.0, slack_m=2.0) is not None
                lon, lat = ego_frame(x, y)
                rows[serial] = dict(serial=serial, tid=tid, x=x, y=y, v=(vx, vy), j=j, lane=lane, cov=cov,
                                    owner=owner, on=on, lon=lon, lat=lat, unseen=round(t - last, 3), age=t - first)
            confirmed = [r for r in rows.values() if r["tid"]]
            by_tid = {r["tid"]: r for r in confirmed}
            # tracks that died since the last cycle: how long were they stale?
            for serial, (was_confirmed, stale_t) in alive_prev.items():
                if serial not in rows and was_confirmed:
                    stale_runs.append(round(t - 0.2 - stale_t, 2) if stale_t is not None else 0.0)
            # --- stale and coasting confirmed tracks
            for r in confirmed:
                if r["j"] < 0:
                    where = covered(r["lon"], r["lat"])
                    coast["coasting frames"] += 1
                    coast["coasting, predicted %s" % where] += 1
                    stale = r["owner"] not in vmap or not r["on"]
                    if stale:
                        coast["stale (off its vehicle or vehicle gone)"] += 1
                        stale_since.setdefault(r["serial"], t)
                    else:
                        stale_since.pop(r["serial"], None)
                else:
                    stale_since.pop(r["serial"], None)
                    if r["owner"] is None and dets[r["j"]][3] is None:
                        coast["measured by an unpaired target"] += 1
            alive_prev = {s: (rows[s]["tid"] is not None, stale_since.get(s)) for s in rows}
            # --- duplicates: two confirmed tracks on one vehicle
            holders = defaultdict(list)
            for r in confirmed:
                if r["owner"] in vmap and r["on"]:
                    holders[r["owner"]].append(r)
            for vid, rs in holders.items():
                if len(rs) > 1:
                    dup["frames x vehicles with duplicate tracks"] += 1
                    dup["extra tracks"] += len(rs) - 1
                    kinds = sorted("measured" if r["j"] >= 0 else "coasting" for r in rs)
                    dup["pair: " + " + ".join(kinds)] += 1
                    example("duplicate", {"run": run_id, "t": t, "vehicle": vid,
                                          "tracks": [[r["tid"], round(r["lon"], 1), round(r["lat"], 1), r["unseen"]] for r in rs]})
            # --- lane assignment of measured confirmed tracks
            for r in confirmed:
                if r["j"] < 0 or r["owner"] not in rel:
                    continue
                true_rel = rel[r["owner"]]["relation"]
                if r["lane"] not in ("ego_lane", "left_lane", "right_lane") and true_rel not in ("ego_lane", "left_lane", "right_lane"):
                    continue
                lanes["compared"] += 1
                if r["lane"] == true_rel:
                    continue
                q = rel[r["owner"]]
                d_lon, d_lat = ego_frame(dets[r["j"]][1], dets[r["j"]][2])
                y_world = vmap[r["owner"]]["centre"][1]
                boundary = min(abs(y_world - (perception.CARRIAGEWAY_Y[0] + i * perception.LANE_WIDTH_M)) for i in range(5))
                cause = ("lane change in progress" if abs(q["vlat"]) > 0.3 else
                         "within 0.5 m of a lane line" if boundary < 0.5 else
                         "detection displaced > 0.75 m" if abs(d_lat - q["lat"]) > 0.75 else
                         "track lags its detection > 0.75 m" if abs(r["lat"] - d_lat) > 0.75 else "other")
                lanes["%s -> %s" % (true_rel, r["lane"])] += 1
                lanes["cause: " + cause] += 1
                lanes["source %s, cause: %s" % (SOURCES[dets[r["j"]][0]], cause)] += 1
                example("lane: " + cause, {"run": run_id, "t": t, "track": r["tid"], "true": true_rel, "reported": r["lane"],
                                           "true_lat": round(q["lat"], 2), "detection_lat": round(d_lat, 2),
                                           "track_lat": round(r["lat"], 2), "true_vlat": round(q["vlat"], 2)})
            # --- the lead slot
            want = pa.truth_slot(rel, "ego_lane", True)
            lead_tid = c["slots"]["lead"]
            if lead_tid:
                r = by_tid[lead_tid]
                lead["present"] += 1
                measured = r["j"] >= 0
                det_owner = dets[r["j"]][3] if measured else None
                if (det_owner if measured else (r["owner"] if r["on"] else None)) == want and want is not None:
                    kind = "correct"
                elif r["lon"] >= TRUTH_WINDOW_M and want is None:
                    kind = ("beyond the truth window: measured, a real radar target" if measured else
                            "coasting beyond the front radar's reach" if r["lon"] >= FRONT_REACH_M else
                            "coasting near the range limit")
                elif measured and det_owner is None:
                    kind = "measured by an unpaired target"
                elif measured:
                    q = rel[det_owner]
                    kind = ("wrong vehicle: a farther ego-lane vehicle (slot selection)" if q["relation"] == "ego_lane" else
                            "lane assignment: %s vehicle%s" % (q["relation"], ", changing lane" if abs(q["vlat"]) > 0.3 else ""))
                elif r["owner"] not in vmap:
                    kind = "coasting: its vehicle left the simulation"
                elif not r["on"]:
                    kind = "coasting, stale: %s" % ("its vehicle changed lane" if rel[r["owner"]]["relation"] != "ego_lane"
                                                    else "drifted off its vehicle")
                else:
                    kind = "coasting on a vehicle that is not the lead (%s)" % rel[r["owner"]]["relation"]
                if r["owner"] in holders and len(holders[r["owner"]]) > 1:
                    lead["of which a duplicate track"] += 1
                lead[kind] += 1
                if kind != "correct":
                    example("lead: " + kind, {"run": run_id, "t": t, "track": lead_tid, "lon": round(r["lon"], 1),
                                              "lat": round(r["lat"], 2), "unseen_s": r["unseen"], "born_unpaired": r["serial"] in born_unpaired,
                                              "owners": dict(seen_owners[r["serial"]]), "true_lead": want,
                                              "decision": c["decision"][:2]})
            # --- stationary vehicles and detection against radial speed
            for vid, q in rel.items():
                if not vis[vid].startswith("radar") or abs(q["lon"]) > 100:
                    continue
                detected = any(d[3] == vid for d in dets)
                tracked = any(r["owner"] == vid and r["on"] for r in confirmed)
                v = vmap[vid]
                speed = math.hypot(*v["v"])
                bx, by = body_centre(v)
                src = "radar front" if vis[vid] == "radar front" else "radar rear"
                mx, my = perception.ego_to_world(*perception.RADARS[src][1:3], pose)
                L = math.hypot(bx - mx, by - my) or 1.0
                vr = ((v["v"][0] - ego_v[0]) * (bx - mx) + (v["v"][1] - ego_v[1]) * (by - my)) / L
                radial["%s | rel. radial %s m/s | detected" % (src, band(abs(vr), (0.05, 0.2, 0.5, 1, 3)))] += detected
                radial["%s | rel. radial %s m/s | frames" % (src, band(abs(vr), (0.05, 0.2, 0.5, 1, 3)))] += 1
                if speed < 0.5:
                    key = "ego %s, %s m" % ("stopped" if ego_t["speed"] < 0.5 else "moving", band(abs(q["lon"]), (25, 50, 100)))
                    stationary[key].update(frames=1, detected=detected, tracked=tracked)
            # --- coasting uncertainty: error to the vehicle's body centre against the filter's covariance
            for r in confirmed:
                if r["owner"] not in vmap or not r["on"]:
                    continue
                bx, by = body_centre(vmap[r["owner"]])
                e = (r["x"] - bx, r["y"] - by)
                key = "unseen %s s" % band(r["unseen"], (0.01, 0.5, 1.0, 2.0, 3.01))
                uncertainty[key + " | error m"].append(math.hypot(*e))
                if r["cov"]:
                    a, b, d = r["cov"]
                    det = a * d - b * b
                    if det > 0:
                        uncertainty[key + " | d2"].append((d * e[0] ** 2 - 2 * b * e[0] * e[1] + a * e[1] ** 2) / det)
                        uncertainty[key + " | sigma m"].append(math.sqrt(max(a, 0) + max(d, 0)))
            # --- decisions against the oracle
            key = (run_id, c["seq"])
            o = oracle.get(key)
            if o is None:
                continue
            mine, theirs = c["decision"][0], o["risk"]
            risks.append((t, LEVELS.index(mine), LEVELS.index(theirs)))
            if LEVELS.index(mine) > LEVELS.index(theirs) and mine in ("HIGH", "CRITICAL"):
                r = by_tid.get(c["decision"][2])
                if r is None:
                    cause = "threat not a vehicle track"
                else:
                    q = rel.get(r["owner"]) if r["on"] or r["j"] >= 0 else None
                    own_vlat = None
                    if q is not None and r["v"][0] is not None:
                        vv = ((r["v"][0] - ego_v[0]) * ey[0] + (r["v"][1] - ego_v[1]) * ey[1])
                        own_vlat = vv
                    if r["owner"] is None or (r["j"] >= 0 and dets[r["j"]][3] is None):
                        cause = "no true vehicle behind the track"
                    elif q is None:
                        cause = "stale coasting track (off its vehicle)"
                    elif r["j"] < 0 and abs(own_vlat - q["vlat"]) > 0.3:
                        cause = "coasting with a wrong lateral speed (%s)" % covered(r["lon"], r["lat"])
                    elif abs(r["lat"] - q["lat"]) > 0.5:
                        cause = "lateral position error > 0.5 m"
                    elif own_vlat is not None and abs(own_vlat - q["vlat"]) > 0.3:
                        cause = "lateral speed error > 0.3 m/s"
                    elif o["threat_ttc_s"] is not None and abs(o["threat_ttc_s"] - (1.5 if mine == "CRITICAL" else 3.0)) < 0.5:
                        cause = "oracle within 0.5 s of the same threshold"
                    else:
                        cause = "other (longitudinal speed or position)"
                    if q is not None and own_vlat is not None:
                        side = 1.0 if q["lat"] >= 0 else -1.0
                        unsupported["%s: lateral speed error toward the ego lane" % mine] += (own_vlat - q["vlat"]) * side < -0.3
                        unsupported["%s: lateral position error toward the ego lane" % mine] += (r["lat"] - q["lat"]) * side < -0.5
                    if r["owner"] in vmap:
                        unsupported["%s: threat vehicle type %s" % (mine, vmap[r["owner"]]["type"])] += 1
                unsupported["%s frames" % mine] += 1
                unsupported["%s: %s" % (mine, cause)] += 1
                frames_out.append([run_id, c["seq"], round(t, 2), mine, theirs, cause, r and r["owner"],
                                   r and vmap.get(r["owner"], {}).get("type"), r and r["unseen"]])
                example("unsupported %s: %s" % (mine, cause), {
                    "run": run_id, "t": t, "track": c["decision"][2], "oracle": theirs, "ttc": c["decision"][3],
                    "oracle_ttc": o["threat_ttc_s"], "track_lon_lat": r and [round(r["lon"], 1), round(r["lat"], 2)],
                    "true_lon_lat": r and r["owner"] in rel and [round(rel[r["owner"]]["lon"], 1), round(rel[r["owner"]]["lat"], 2)],
                    "unseen_s": r and r["unseen"], "owner": r and r["owner"]})
            if theirs != "SAFE" and mine == "SAFE":
                tracked = o.get("threat_id") and o["threat_id"].startswith("TRUTH_") and any(
                    r["owner"] == o["threat_id"][6:] and r["on"] for r in confirmed)
                missed["%s missed (tracker SAFE)" % theirs] += 1
                missed["%s missed: oracle's vehicle %s" % (theirs, "tracked, no conflict predicted" if tracked else "not tracked")] += 1
                vt = vmap.get((o.get("threat_id") or "")[6:])
                missed["%s missed: oracle's vehicle type %s" % (theirs, vt["type"] if vt else "?")] += 1
        # --- radar handover per vehicle
        for vid, ds in vehicle_dets.items():
            for (t1, s1, k1, confirmed1, oncoming), (t2, s2, k2, _, _) in zip(ds, ds[1:]):
                if s1 == s2:
                    continue
                gap = band(t2 - t1, (0.61, 1.51, 3.01))
                way = "%s, %s, %s" % ("front->rear" if s1 == 0 else "rear->front", "oncoming" if oncoming else "same direction",
                                      "confirmed track" if confirmed1 else "unconfirmed track")
                outcome = "kept its track" if k1 == k2 else "new track"
                handover["%s | gap %s s | %s" % (way, gap, outcome)] += 1
                handover["%s | %s" % (way, outcome)] += 1
        # --- warning onset per oracle episode
        for level in ("CAUTION", "HIGH", "CRITICAL"):
            li = LEVELS.index(level)
            episodes, start = [], None
            for i, (t, mine, theirs) in enumerate(risks):
                if theirs >= li and start is None:
                    start = i
                if (theirs < li or i == len(risks) - 1) and start is not None:
                    episodes.append((start, i if theirs < li else i + 1))
                    start = None
            for s, e in episodes:
                t0 = risks[s][0]
                hits = [t for t, mine, _ in risks if t0 - 2.0 - 1e-9 <= t <= risks[e - 1][0] + 1e-9 and mine >= li]
                if hits:
                    onsets[level].append(round(hits[0] - t0, 2))
                else:
                    onset_missed[level] += 1
    rates = {k[:-len(" | frames")]: round(radial[k[:-len(" | frames")] + " | detected"] / v, 4)
             for k, v in sorted(radial.items()) if k.endswith(" | frames") and v}
    result = {
        "schema": "roadsense.track-failures.v1", "tracker": json.load(open(trace_dir / "replay.json")), "truth": "evaluation only",
        "counts": dict(n), "lead_slot": dict(lead.most_common()),
        "coasting": dict(coast.most_common()),
        "stale_before_removal_s": {"tracks": len(stale_runs), "stale": sum(1 for s in stale_runs if s > 0),
                                   "p50_of_stale": pct([s for s in stale_runs if s > 0], 0.5),
                                   "p90_of_stale": pct([s for s in stale_runs if s > 0], 0.9),
                                   "max": max(stale_runs, default=None)},
        "duplicates": dict(dup.most_common()), "lane_assignment": dict(lanes.most_common()),
        "stationary_vehicles": {k: {"frames": s["frames"], "detected": round(s["detected"] / s["frames"], 3),
                                    "tracked": round(s["tracked"] / s["frames"], 3)} for k, s in sorted(stationary.items())},
        "detection_by_relative_radial_speed": {k: {"frames": radial[k + " | frames"], "detected": v} for k, v in rates.items()},
        "handover": dict(sorted(handover.items())),
        "uncertainty_while_coasting": {k: {"n": len(v), "p50": pct(v, 0.5), "p90": pct(v, 0.9),
                                           **({"within_99pct_gate": share(v, lambda d2: d2 <= 9.21)} if k.endswith("d2") else {})}
                                       for k, v in sorted(uncertainty.items())},
        "unsupported_high_critical": dict(unsupported.most_common()), "missed_warnings": dict(missed.most_common()),
        "unsupported_frames": frames_out,
        "warning_onset_s": {level: {"oracle_episodes": len(v) + onset_missed[level], "never_warned": onset_missed[level],
                                    "p10": pct(v, 0.1), "p50": pct(v, 0.5), "p90": pct(v, 0.9)} for level, v in onsets.items()},
        "examples": examples}
    Path(out).write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: result[k] for k in ("lead_slot", "duplicates", "unsupported_high_critical")}, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("geometry")
    p.add_argument("out", type=Path)
    p.add_argument("campaigns", type=Path, nargs="+")
    p = sub.add_parser("trace")
    p.add_argument("campaign", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("--tracker", default="legacy")
    p.add_argument("--consistent", type=Path)
    p = sub.add_parser("oracle", help="the TruthOracle replay with vehicles at their body centres (evaluation only)")
    p.add_argument("campaign", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("geometry", type=Path)
    p = sub.add_parser("audit", help="perception_audit.py with vehicles at their body centres (supplementary)")
    p.add_argument("campaign", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("geometry", type=Path)
    p.add_argument("--slots", type=Path, required=True)
    p = sub.add_parser("expiries", help="P4 expiries against truth (evaluation only)")
    p.add_argument("campaign", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--tracker", default="p1p2p4")
    p = sub.add_parser("analyse")
    p.add_argument("campaign", type=Path)
    p.add_argument("trace_dir", type=Path)
    p.add_argument("oracle", type=Path)
    p.add_argument("out", type=Path)
    args = ap.parse_args()
    if args.command == "geometry":
        geometry(args.out, args.campaigns)
    elif args.command == "trace":
        trace(args.campaign, args.out_dir, args.tracker, args.consistent)
    elif args.command == "expiries":
        expiries(args.campaign, args.out, args.tracker)
    elif args.command in ("oracle", "audit"):
        K = {k: tuple(v) for k, v in json.load(open(args.geometry))["fit"]["by_type_median"].items()}
        pa.truth_frames = body_truth(K)
        if args.command == "oracle":
            tr.replay(args.campaign, args.out_dir, "oracle")
        else:
            pa.main([str(args.campaign), str(args.out_dir), "--slots", str(args.slots)])
    else:
        analyse(args.campaign, args.trace_dir, args.oracle, args.out)


if __name__ == "__main__":
    main()
