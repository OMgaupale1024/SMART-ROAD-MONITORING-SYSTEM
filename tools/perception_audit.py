"""Phase 7A.3 offline perception audit: recorded slot features against simulator ground truth.

    PYTHONPATH=src python3 tools/perception_audit.py CAMPAIGN OUT_DIR [--model docs/evidence/phase7a2/model.json]

Read-only. Simulator truth is used only to *evaluate* what RoadSense recorded (which true vehicle each recorded slot
holds, how wrong its kinematics are, what the radars could geometrically have seen); it never becomes an input.
The radars' raw targets and track ids were not recorded, so identity is inferred: a slot whose track age keeps growing
is one track; a slot matched to the same true vehicle whose age restarts is a fragmented track.

Visibility is a geometric model of the two RoadSense radars (mount, range, field of view, occlusion by nearer
vehicle boxes), not a record of Webots' radar output: "not observable" means "outside this model".
"""
import argparse
import collections
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

from roadsense.autonomy import perception
from roadsense.expanded_risk import Predictor

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario_dataset  # noqa: E402  the label's own outcome definition, reused to name the threat vehicle

SLOTS = (("lead", "ego_lane", True), ("follower", "ego_lane", False), ("left", "left_lane", None),
         ("right", "right_lane", None))
RADAR_MODEL = {"radar front": (4.054, 0.0, 0.0, 150.0), "radar rear": (-1.16, 0.0, math.pi, 100.0)}
FOV_RAD, MIN_RANGE_M = 2.6, 1.0
MATCH_M = 4.0  # a recorded slot belongs to the nearest true vehicle centre within this distance
CONFIRM_ALLOWANCE_S = 0.6  # tracker confirmation (0.4 s) plus one cycle
LATERAL_MOTION_MPS = 0.3


def _box_bearings(radar_xy, v):
    (cx, cy), c, s = v["centre"], math.cos(v["yaw"]), math.sin(v["yaw"])
    hl, hw = v["length"] / 2, v["width"] / 2
    return [math.atan2(cy + a * s + b * c - radar_xy[1], cx + a * c - b * s - radar_xy[0])
            for a in (-hl, hl) for b in (-hw, hw)]


def visibility(ego, vehicles):
    """{vehicle id: name of a radar that geometrically sees its centre, or the reason none does}."""
    out = {}
    for v in vehicles:
        reasons = []
        for name, (mx, my, facing, max_range) in RADAR_MODEL.items():
            radar = perception.ego_to_world(mx, my, (ego["x"], ego["y"], ego["yaw"]))
            dx, dy = v["centre"][0] - radar[0], v["centre"][1] - radar[1]
            rng = math.hypot(dx, dy)
            bearing = math.atan2(dy, dx)
            off = (bearing - ego["yaw"] - facing + math.pi) % (2 * math.pi) - math.pi
            if not MIN_RANGE_M < rng < max_range:
                reasons.append("range")
                continue
            if abs(off) > FOV_RAD / 2 - perception.EDGE_MARGIN_RAD:
                reasons.append("fov")
                continue
            blocked = False
            for o in vehicles:
                if o is v or math.hypot(o["centre"][0] - radar[0], o["centre"][1] - radar[1]) >= rng:
                    continue
                angles = [(a - bearing + math.pi) % (2 * math.pi) - math.pi for a in _box_bearings(radar, o)]
                if min(angles) < 0 < max(angles) and max(angles) - min(angles) < math.pi:
                    blocked = True
                    break
            if blocked:
                reasons.append("occluded")
                continue
            out[v["id"]] = name
            break
        else:
            out[v["id"]] = "not_visible:" + ("occluded" if "occluded" in reasons else "fov" if "fov" in reasons
                                             else "range")
    return out


def truth_frames(path):
    """{round(t, 1): (ego, vehicles)} with vehicle centres and finite-difference world velocities."""
    frames = []
    for line in open(path):
        r = json.loads(line)
        if r["record"] == "frame":
            frames.append(r)
    for f in frames:
        for v in f["vehicles"]:
            c, s = math.cos(v["yaw"]), math.sin(v["yaw"])
            v["centre"] = (v["x"] - v["length"] / 2 * c, v["y"] - v["length"] / 2 * s)
    out = {}
    for i, f in enumerate(frames):
        prev, nxt = frames[max(0, i - 1)], frames[min(len(frames) - 1, i + 1)]
        dt = nxt["t"] - prev["t"]
        ego = dict(f["ego"])
        ego["v"] = ((nxt["ego"]["x"] - prev["ego"]["x"]) / dt, (nxt["ego"]["y"] - prev["ego"]["y"]) / dt) if dt else (0, 0)
        p = {v["id"]: v for v in prev["vehicles"]}
        n = {v["id"]: v for v in nxt["vehicles"]}
        for v in f["vehicles"]:
            a, b = p.get(v["id"], v), n.get(v["id"], v)
            span = (nxt["t"] if b is not v else f["t"]) - (prev["t"] if a is not v else f["t"])
            v["v"] = ((b["centre"][0] - a["centre"][0]) / span, (b["centre"][1] - a["centre"][1]) / span) if span else (
                v["speed"] * math.cos(v["yaw"]), v["speed"] * math.sin(v["yaw"]))
        out[round(f["t"], 1)] = (ego, f["vehicles"])
    return out


def relative(ego, vehicles):
    """Ego-frame centre position and relative velocity of each true vehicle, with its true lane relation."""
    pose = (ego["x"], ego["y"], ego["yaw"])
    rel = {}
    for v in vehicles:
        lon, lat = perception.world_to_ego(*v["centre"], pose)
        vlon, vlat = perception.world_to_ego(v["v"][0] - ego["v"][0], v["v"][1] - ego["v"][1], (0, 0, ego["yaw"]))
        rel[v["id"]] = {"lon": lon, "lat": lat, "vlon": vlon, "vlat": vlat, "type": v["type"],
                        "length": v["length"], "width": v["width"],
                        "speed": math.hypot(*v["v"]), "relation": perception.lane_relation(v["centre"][1], ego["y"])}
    return rel


def truth_slot(rel, lane, ahead):
    best = None
    for vid, r in rel.items():
        if r["relation"] != lane or (ahead is not None and (r["lon"] >= 0) != ahead):
            continue
        if not (-100 < r["lon"] < 150):
            continue
        if best is None or abs(r["lon"]) < abs(rel[best]["lon"]):
            best = vid
    return best


def match(rel, lon, lat):
    best = min(rel, key=lambda vid: math.hypot(rel[vid]["lon"] - lon, rel[vid]["lat"] - lat), default=None)
    if best is None or math.hypot(rel[best]["lon"] - lon, rel[best]["lat"] - lat) > MATCH_M:
        return None
    return best


def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 3) if values else None


def summary(values):
    return {"n": len(values), "mean": round(statistics.fmean(values), 3) if values else None,
            "mae": round(statistics.fmean(abs(v) for v in values), 3) if values else None,
            "p50_abs": pct([abs(v) for v in values], 0.5), "p95_abs": pct([abs(v) for v in values], 0.95)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("campaign", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--model", type=Path, default=Path("docs/evidence/phase7a2/model.json"))
    ap.add_argument("--failures", type=Path, default=Path("docs/evidence/phase7a2/fresh-failure-cases.json"))
    args = ap.parse_args(argv)
    if args.out.exists():
        sys.exit("output directory exists: %s (never overwritten)" % args.out)
    predictor = Predictor.load(args.model)

    supervised = {}  # (run_id, sequence) -> split, label, score
    for line in open(args.campaign / "features-final/samples.jsonl"):
        r = json.loads(line)
        score = predictor.predict(r["features"], r["feature_schema"])["risk_score"]
        supervised[(r["run_id"], r["sequence"])] = (r["split"], r["labels"]["forward_risk_3s"], score)
    db = sqlite3.connect("file:%s?mode=ro" % (args.campaign / "history/roadsense-history.sqlite3"), uri=True)
    recorded = collections.defaultdict(list)
    for run_id, seq, sim_s, feats in db.execute(
            "SELECT g.run_id, s.sequence, s.sim_s, s.features FROM samples s JOIN segments g USING (segment_id) "
            "ORDER BY g.run_id, s.sim_s"):
        recorded[run_id].append((seq, sim_s, json.loads(feats)))

    slot_outcomes = collections.Counter()
    miss_by = {k: collections.Counter() for k in ("speed", "type", "range", "slot")}
    seen_by = {k: collections.Counter() for k in ("speed", "type", "range", "slot")}
    errors = collections.defaultdict(list)  # kinematic errors on correctly matched slots
    switches = fragments = continuous_frames = 0
    lag = []  # truth lateral-motion onset -> recorded |v_lat| > threshold, same vehicle, seconds
    cutins, frame_trace, artifacts = [], {}, []
    runs_dir = args.campaign / "runs"
    for run_dir in sorted(runs_dir.iterdir()):
        scenario = json.load(open(run_dir / "scenario.json"))
        run_id = scenario["run_id"]
        perception.use_road(scenario["profile"])
        truth = truth_frames(run_dir / "ground_truth.jsonl")
        prev_slot = {}
        last_track = {}  # true vehicle -> (t, recorded track age) when last held by any slot
        visible_since = {}
        lateral_onset, cued = {}, set()
        timeline = []
        for seq, sim_s, f in recorded[run_id]:
            key = round(sim_s, 1)
            if key not in truth:
                continue
            ego, vehicles = truth[key]
            near = [v for v in vehicles if abs(v["centre"][0] - ego["x"]) < 160]
            rel = relative(ego, near)
            vis = visibility(ego, near)
            for vid in rel:
                if vis[vid].startswith("radar"):
                    visible_since.setdefault(vid, sim_s)
                else:
                    visible_since.pop(vid, None)
            rec_ids = {}
            frame = {"t": sim_s, "seq": seq, "slots": {}}
            for slot, lane, ahead in SLOTS:
                want = truth_slot(rel, lane, ahead)
                present = f.get(slot + "_present") == 1
                got = match(rel, f[slot + "_lon_m"], f[slot + "_lat_m"]) if present else None
                rec_ids[slot] = got
                if want is not None:
                    r = rel[want]
                    long_visible = vis[want].startswith("radar") and sim_s - visible_since.get(want, sim_s) >= CONFIRM_ALLOWANCE_S
                    bands = {"speed": "%02d-%02d m/s" % (5 * int(r["speed"] // 5), 5 * int(r["speed"] // 5) + 5),
                             "type": r["type"], "range": "%03d-%03d m" % (25 * int(abs(r["lon"]) // 25), 25 * int(abs(r["lon"]) // 25) + 25),
                             "slot": slot}
                if want is None and not present:
                    outcome = "empty_ok"
                elif want is None:
                    outcome = "spurious" if got is None else "lane_misassigned"
                elif not present:
                    other = want in [rec_ids.get(s) for s, _, _ in SLOTS if s != slot]
                    if not vis[want].startswith("radar"):
                        outcome = "missed_" + vis[want].split(":")[1]
                    elif not long_visible:
                        outcome = "missed_confirming"
                    else:
                        outcome = "missed_tracking"
                    if long_visible:
                        for k, b in bands.items():
                            miss_by[k][b] += 1
                elif got == want:
                    outcome = "correct"
                    r = rel[want]
                    errors["lon_m"].append(f[slot + "_lon_m"] - r["lon"])
                    errors["lat_m"].append(f[slot + "_lat_m"] - r["lat"])
                    if f[slot + "_v_lon_mps"] is not None:
                        errors["v_lon_mps"].append(f[slot + "_v_lon_mps"] - r["vlon"])
                        moving = abs(r["vlat"]) > LATERAL_MOTION_MPS
                        errors["v_lat_mps_" + ("lateral_motion" if moving else "steady")].append(
                            f[slot + "_v_lat_mps"] - r["vlat"])
                else:
                    outcome = "wrong_vehicle" if got is not None else "mislocalized"
                if want is not None and vis[want].startswith("radar") and long_visible:
                    for k, b in bands.items():
                        seen_by[k][b] += 1
                slot_outcomes[(slot, outcome)] += 1
                # identity: same slot, consecutive frame
                p = prev_slot.get(slot)
                if present and p and p["present"] and abs(sim_s - p["t"] - 0.2) < 0.05 and got and p["got"]:
                    age, page = f[slot + "_age_s"], p["age"]
                    if age is not None and page is not None:
                        continuous_frames += 1
                        if abs(age - page - 0.2) < 0.05 and got != p["got"]:
                            switches += 1  # one track (its age runs on) now on another true vehicle
                if present and got and f[slot + "_age_s"] is not None:
                    lt = last_track.get(got)
                    age = f[slot + "_age_s"]
                    if lt and 0 < sim_s - lt[0] <= 3.0 and age < lt[1] + (sim_s - lt[0]) - 0.3:
                        fragments += 1  # the same true vehicle, within the coast window, under a younger track
                    last_track[got] = (sim_s, age)
                prev_slot[slot] = {"present": present, "t": sim_s, "got": got, "age": f[slot + "_age_s"]}
                frame["slots"][slot] = {"rec": got, "truth": want, "outcome": outcome, "age": f[slot + "_age_s"],
                                        "v_lat": f[slot + "_v_lat_mps"], "lon": f[slot + "_lon_m"]}
            # lateral cue latency per true vehicle
            for vid, r in rel.items():
                if abs(r["vlat"]) > LATERAL_MOTION_MPS and r["relation"] in ("left_lane", "right_lane", "ego_lane"):
                    lateral_onset.setdefault(vid, sim_s)
                elif vid in lateral_onset and vid not in cued and sim_s - lateral_onset[vid] > 3:
                    del lateral_onset[vid]
                if vid in lateral_onset and vid not in cued:
                    for slot, rid in rec_ids.items():
                        if rid == vid and f[slot + "_v_lat_mps"] is not None and abs(f[slot + "_v_lat_mps"]) > LATERAL_MOTION_MPS \
                                and (f[slot + "_v_lat_mps"] > 0) == (r["vlat"] > 0):
                            lag.append(round(sim_s - lateral_onset[vid], 2))
                            cued.add(vid)
            frame["vis"] = {vid: vis[vid] for vid in rel if vid.startswith("rs_")}
            frame["rel"] = {vid: {k: round(v, 2) if isinstance(v, float) else v for k, v in rel[vid].items()}
                            for vid in rel if vid.startswith("rs_") or abs(rel[vid]["lon"]) < 80}
            s = supervised.get((run_id, seq))
            frame["split"], frame["label"], frame["score"] = (s or (None, None, None))
            timeline.append(frame)
        frame_trace[run_id] = timeline
        cutins += cutin_timelines(run_id, scenario, run_dir, timeline, predictor.threshold)
        gt_frames = [r for r in map(json.loads, open(run_dir / "ground_truth.jsonl")) if r["record"] == "frame"]
        for t, vid in insertion_artifacts(gt_frames):
            positives = [fr for fr in timeline if t - 3.0 - 1e-6 <= fr["t"] < t - 1e-6 and fr["label"] == 1]
            artifacts.append({"run_id": run_id, "split": next((fr["split"] for fr in timeline if fr["split"]), None), "t": t,
                              "vehicle": vid, "positive_labels_within_3s_before": len(positives)})

    missed = json.load(open(args.failures))["missed_pre_onset_incidents"]
    traces = [trace_incident(m, frame_trace.get(m["run_id"], []), predictor.threshold, runs_dir) for m in missed]
    args.out.mkdir(parents=True)
    by = {k: {b: {"missed": miss_by[k][b], "observable_frames": seen_by[k][b] + miss_by[k][b],
                  "miss_rate": round(miss_by[k][b] / (seen_by[k][b] + miss_by[k][b]), 4)}
              for b in sorted(set(seen_by[k]) | set(miss_by[k]))} for k in miss_by}
    result = {
        "schema": "roadsense.perception-audit.v1", "analysis_only": True,
        "visibility": "geometric model of the RoadSense front/rear radars; raw radar targets were not recorded",
        "frames": sum(len(t) for t in frame_trace.values()), "runs": len(frame_trace),
        "slot_outcomes": {"%s/%s" % k: n for k, n in sorted(slot_outcomes.items())},
        "missed_after_confirmation_allowance_by": by,
        "identity": {"continuous_matched_slot_frames": continuous_frames, "identity_switches": switches,
                     "fragmentations_within_3s": fragments},
        "kinematic_error_correct_slots": {k: summary(v) for k, v in sorted(errors.items())},
        "lateral_cue_latency_s": {"vehicles": len(lag), "p50": pct(lag, .5), "p90": pct(lag, .9), "max": max(lag, default=None)},
        "cut_ins": cutins, "missed_incident_traces": traces, "insertion_speed_artifacts": artifacts}
    (args.out / "perception-audit.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    with open(args.out / "frame-traces.jsonl", "w") as out:
        for run_id, timeline in sorted(frame_trace.items()):
            for frame in timeline:
                out.write(json.dumps(dict(frame, run_id=run_id), separators=(",", ":")) + "\n")
    print(args.out / "perception-audit.json")


def first(timeline, test, start=None):
    for fr in timeline:
        if (start is None or fr["t"] >= start) and test(fr):
            return fr["t"]
    return None


def cutin_timelines(run_id, scenario, run_dir, timeline, threshold):
    """Staged cut-ins: when the cutter existed, was geometrically visible, tracked, showed lateral motion (true and
    recorded), entered the ego lane (true and recorded) and when the model warned."""
    out = []
    events = [json.loads(l) for l in open(run_dir / "ground_truth.jsonl") if '"record":"event"' in l.replace(" ", "")]
    for e in (scenario.get("event"), scenario.get("secondary_event")):
        if not e or e.get("kind") != "cut_in":
            continue
        inserted = [ev for ev in events if ev.get("what") == "inserted" and ev.get("vehicle", "").startswith("rs_cutter")]
        if not inserted:
            out.append({"run_id": run_id, "staged": False})
            continue
        vid = inserted[0]["vehicle"]
        t0 = inserted[0]["t"]
        act = next((ev["t"] for ev in events if ev.get("what") == "act" and ev.get("vehicle") == vid), None)
        def rec_slot(fr):
            return [s for s, v in fr["slots"].items() if v["rec"] == vid]
        gt_frames = [r for r in map(json.loads, open(run_dir / "ground_truth.jsonl")) if r["record"] == "frame"]
        near_miss = next((t for t, hit, _, _ in near_miss_hits(gt_frames) if hit == vid), None)
        row = {"run_id": run_id, "staged": True, "first_true_near_miss_s": near_miss, "vehicle_type": e.get("vehicle_type", "car"), "lane_change_mode": e.get("lane_change_mode"),
               "inserted_s": t0, "act_s": act,
               "first_visible_s": first(timeline, lambda fr: str(fr["vis"].get(vid, "")).startswith("radar"), t0),
               "first_tracked_s": first(timeline, lambda fr: bool(rec_slot(fr)), t0),
               "truth_lateral_onset_s": first(timeline, lambda fr: vid in fr["rel"] and abs(fr["rel"][vid]["vlat"]) > LATERAL_MOTION_MPS, act or t0),
               "recorded_lateral_cue_s": first(timeline, lambda fr: any(fr["slots"][s]["v_lat"] is not None and abs(fr["slots"][s]["v_lat"]) > LATERAL_MOTION_MPS for s in rec_slot(fr)), act or t0),
               "truth_lane_entry_s": first(timeline, lambda fr: vid in fr["rel"] and fr["rel"][vid]["relation"] == "ego_lane" and fr["rel"][vid]["lon"] > 0, t0),
               "recorded_lead_s": first(timeline, lambda fr: fr["slots"]["lead"]["rec"] == vid, t0),
               "first_positive_label_s": first(timeline, lambda fr: fr["label"] == 1, t0 - 3),
               "model_warning_s": first(timeline, lambda fr: fr["score"] is not None and fr["score"] >= threshold, t0 - 3)}
        out.append(row)
    return out


def near_miss_hits(gt_frames):
    """[(t, vehicle id, its recorded speed, first frame of that vehicle)] for every true forward near miss, using
    scenario_dataset.outcomes one vehicle at a time (the label's own definition)."""
    hits, first_seen = [], {}
    for f in gt_frames:
        for v in f["vehicles"]:
            first_seen.setdefault(v["id"], f["t"])
            if scenario_dataset.outcomes({"t": f["t"], "ego": f["ego"], "vehicles": [v]})["forward_near_miss"]:
                hits.append((f["t"], v["id"], v["speed"], abs(first_seen[v["id"]] - f["t"]) < 1e-6))
    return hits


def insertion_artifacts(gt_frames):
    """Near-miss episodes made only of a staged vehicle's first frame, while its recorded speed is still 0: the
    TTC uses that 0 and is spurious (the vehicle was inserted at the staged speed). [(t, vehicle id)]."""
    hits = near_miss_hits(gt_frames)
    by_vehicle = collections.defaultdict(list)
    for t, vid, speed, first in hits:
        by_vehicle[vid].append((t, speed, first))
    out = []
    for vid, rows in by_vehicle.items():
        if vid.startswith("rs_") and rows[0][2] and rows[0][1] == 0.0 and not any(
                0 < t - rows[0][0] <= 3.0 for t, _, _ in rows[1:]):
            out.append((rows[0][0], vid))
    return out


def trace_incident(m, timeline, threshold, runs_dir):
    """The 15 pre-onset frames of a missed incident, stage by stage: did the threat vehicle exist, was it
    geometrically visible, tracked in some slot, were its recorded lateral kinematics right, what did the model
    score. The threat is the vehicle of the first true near miss after onset (onset is the first positive label)."""
    onset = m["onset_s"]
    window = [fr for fr in timeline if onset - 3.0 - 1e-6 <= fr["t"] < onset - 1e-6]
    run_dir = next(runs_dir.glob(m["run_id"]), None)
    gt = [json.loads(l) for l in open(run_dir / "ground_truth.jsonl")] if run_dir else []
    gt = [f for f in gt if f["record"] == "frame"]
    hits = [h for h in near_miss_hits([f for f in gt if onset - 1e-6 <= f["t"] <= onset + 3.2])]
    threat = hits[0][1] if hits else None
    artifact = (threat, onset) in [(vid, t) for t, vid in insertion_artifacts(gt)] if threat else False
    inserted = None
    if threat and threat.startswith("rs_") and run_dir:
        inserted = next((json.loads(l)["t"] for l in open(run_dir / "ground_truth.jsonl")
                         if '"inserted"' in l and '"%s"' % threat in l), None)
    frames, stages = [], collections.Counter()
    for fr in window:
        r = fr["rel"].get(threat) if threat else None
        slots = [s for s, v in fr["slots"].items() if v["rec"] == threat] if threat else []
        if threat is None:
            stage = "no_true_forward_near_miss_found"
        elif inserted is not None and fr["t"] < inserted - 1e-6:
            stage = "threat_not_yet_inserted"
        elif r is None:
            stage = "threat_beyond_60m"
        elif not str(fr["vis"].get(threat, "radar")).startswith("radar"):
            stage = "not_observable_" + fr["vis"][threat].split(":")[1]
        elif not slots:
            stage = "observable_not_in_any_slot"
        else:
            v_lat = fr["slots"][slots[0]]["v_lat"]
            if abs(r["vlat"]) > LATERAL_MOTION_MPS and (v_lat is None or abs(v_lat) < LATERAL_MOTION_MPS):
                stage = "tracked_lateral_motion_not_estimated"
            else:
                stage = "tracked_kinematics_ok_model_below_threshold" if (fr["score"] or 0) < threshold else "warned"
        stages[stage] += 1
        frames.append({"t": fr["t"], "stage": stage, "slots": slots,
                       "truth": r and {k: r[k] for k in ("lon", "lat", "vlon", "vlat", "relation", "type")},
                       "recorded_v_lat": slots and fr["slots"][slots[0]]["v_lat"], "score": fr["score"]})
    seen = next((fr["rel"][threat]["type"] for fr in timeline if threat in fr["rel"]), None)
    return {"run_id": m["run_id"], "split": m["split"], "family": m["family"], "onset_s": onset, "threat": threat,
            "threat_type": seen, "threat_inserted_s": inserted, "insertion_speed_artifact": artifact,
            "near_miss_frames_after_onset": len(hits),
            "stage_frames": dict(stages), "max_score": max((fr["score"] or 0 for fr in window), default=None),
            "frames": frames}


if __name__ == "__main__":
    main()
