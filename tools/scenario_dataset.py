"""Phase 6D: strict physical-truth joins, future forward-risk labels and grouped export. No training."""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

import scenarios as sc
from roadsense.web import history

SCHEMA = "roadsense.forward-risk.v1"
HORIZON_S = 3.0
PERIOD_S = 0.2
# Inputs are observable now; prediction outputs, recorder quality and irrelevant task inputs stay out.
INPUTS = tuple(n for n, _, _ in history.FEATURE_COLUMNS if n in (
    "ego_speed_mps", "ego_acceleration_mps2", "ego_steering_rad",
    "left_lane_driving", "right_lane_driving", "track_count") or n.startswith(("lead_", "left_", "right_")))
INPUTS = tuple(n for n in INPUTS if n not in ("derived_min_ttc_s", "derived_conflicts"))


def audit_features():
    result = {}
    for n, unit, description in history.FEATURE_COLUMNS:
        if n.startswith("derived_"):
            category = "heuristic_safety_output"
        elif n in ("sequence_gap_before", "history_dropped_before", "sim_dt_s"):
            category = "recording_metadata"
        elif n in ("ego_lane", "ego_lane_change"):
            category = "privileged_controller_metadata"
        elif n.startswith("hazard"):
            category = "simulator_assisted_camera_recognition"
        elif n == "ego_acceleration_mps2" or n.endswith(("age_s", "unseen_s")):
            category = "historical_inference_available"
        else:
            category = "inference_available_in_prototype"
        result[n] = {"category": category, "selected": n in INPUTS, "unit": unit, "description": description}
    return result


CORRECTED_SCHEMA = "roadsense.forward-risk.v1+init-speed-fix"  # Phase 7A.3 P7: v1 with initialization speeds fixed
STILL_MPS, MOVING_MPS, AGREE_MPS = 0.5, 2.0, 3.0


def correct_initialization(frames):
    """Frames with initialization-frame speed artifacts fixed, and the list of fixes; the input is not modified.

    A vehicle's physical speed is its Webots node velocity, and a node SUMO has just placed (an inserted event vehicle,
    a node reused for another vehicle) reports 0 on its first frame. A first-frame speed is replaced only when two
    independent sources say the vehicle moves: its own displacement to the next frame and SUMO's logical speed, both
    above MOVING_MPS and within AGREE_MPS of each other. Stationary vehicles, slow vehicles and every later frame keep
    their recorded speed; near misses that do not rest on the artifact are unchanged."""
    out, fixes, previous = [], [], set()
    for i, frame in enumerate(frames):
        nxt = {v["id"]: v for v in frames[i + 1]["vehicles"]} if i + 1 < len(frames) else {}
        dt = frames[i + 1]["t"] - frame["t"] if i + 1 < len(frames) else 0
        logical = {v["id"]: v.get("speed") for v in frame.get("sumo_vehicles") or ()}
        vehicles = []
        for v in frame["vehicles"]:
            n = nxt.get(v["id"])
            if v["id"] not in previous and n is not None and 0 < dt <= 0.4 and abs(v["speed"]) < STILL_MPS:
                moved = ((n["x"] - v["x"]) * math.cos(v["yaw"]) + (n["y"] - v["y"]) * math.sin(v["yaw"])) / dt
                sumo = logical.get(v["id"])
                if sumo is not None and moved > MOVING_MPS and sumo > MOVING_MPS and abs(moved - sumo) < AGREE_MPS:
                    fixes.append({"t": frame["t"], "vehicle": v["id"], "type": v.get("type"), "recorded_mps": v["speed"],
                                  "displacement_mps": round(moved, 3), "sumo_mps": sumo})
                    v = dict(v, speed=round(moved, 3))
            vehicles.append(v)
        previous = {v["id"] for v in frame["vehicles"]}
        out.append(dict(frame, vehicles=vehicles))
    return out, fixes


def outcomes(frame):
    """Oriented intended-footprint proxy and forward/rear separation from physical Webots poses."""
    ego = frame["ego"]
    front, rear, half = sc.EGO_FRONT_M, sc.EGO_REAR_M, sc.EGO_HALF_WIDTH_M
    out = {"t": frame["t"], "collision_proxy": False, "forward_overlap_proxy": False,
           "lead_gap_m": None, "lead_ttc_s": None, "lead_headway_s": None,
           "follow_gap_m": None, "follow_ttc_s": None, "follow_headway_s": None}
    ego_box = [(-rear, -half), (front, -half), (front, half), (-rear, half)]
    for v in frame["vehicles"]:
        yaw = v["yaw"] - ego["yaw"]
        x, y = sc._ego_frame(ego, v["x"], v["y"])
        c, s = math.cos(yaw), math.sin(yaw)
        corners = [(x + dx * c - dy * s, y + dx * s + dy * c)
                   for dx, dy in ((0, -v["width"] / 2), (0, v["width"] / 2),
                                  (-v["length"], v["width"] / 2), (-v["length"], -v["width"] / 2))]
        lo, hi = min(p[0] for p in corners), max(p[0] for p in corners)
        low_lat, high_lat = min(p[1] for p in corners), max(p[1] for p in corners)
        overlap = all(max(a) >= min(b) and max(b) >= min(a)
                      for ax, ay in ((1, 0), (0, 1), (c, s), (-s, c))
                      for a, b in [([px * ax + py * ay for px, py in ego_box],
                                    [px * ax + py * ay for px, py in corners])])
        out["collision_proxy"] |= overlap
        centre = (lo + hi) / 2
        forward_overlap = overlap and centre >= (front - rear) / 2
        out["forward_overlap_proxy"] |= forward_overlap
        if forward_overlap:
            out.update(lead_gap_m=0.0, lead_ttc_s=0.0,
                       lead_headway_s=0.0 if ego["speed"] > 2 else None)
        if low_lat >= half or high_lat <= -half:
            continue
        side = "lead" if lo > front else "follow" if hi < -rear else None
        if side is None:
            continue
        gap = lo - front if side == "lead" else -rear - hi
        if out[side + "_gap_m"] is not None and gap >= out[side + "_gap_m"]:
            continue
        along = v["speed"] * c
        closing = ego["speed"] - along if side == "lead" else along - ego["speed"]
        speed = ego["speed"] if side == "lead" else v["speed"]
        out[side + "_gap_m"] = round(gap, 4)
        out[side + "_ttc_s"] = round(gap / closing, 4) if closing > 0.1 else None
        out[side + "_headway_s"] = round(gap / speed, 4) if speed > 2 else None
    out["forward_near_miss"] = out["forward_overlap_proxy"] or (
        ego["speed"] > 2 and ((out["lead_gap_m"] is not None and out["lead_gap_m"] < 2) or
                              (out["lead_ttc_s"] is not None and out["lead_ttc_s"] < 1.5)))
    out["tailgated"] = out["follow_headway_s"] is not None and out["follow_headway_s"] < 1
    out["unsafe_headway"] = out["lead_headway_s"] is not None and out["lead_headway_s"] < 1
    return out


def tick(t):
    n = round(t / PERIOD_S)
    if abs(t - n * PERIOD_S) > 0.002:
        raise ValueError("timestamp off 200 ms grid: %s" % t)
    return n


def future_label(truth, at):
    future = [truth.get(i) for i in range(at + 1, at + round(HORIZON_S / PERIOD_S) + 1)]
    if any(o is None for o in future):
        return None
    def minimum(key):
        values = [o[key] for o in future if o[key] is not None]
        return min(values) if values else None
    return {"forward_risk_3s": int(any(o["forward_near_miss"] for o in future)),
            "future_min_lead_gap_m": minimum("lead_gap_m"), "future_min_lead_ttc_s": minimum("lead_ttc_s"),
            "future_forward_overlap_proxy": any(o["forward_overlap_proxy"] for o in future)}


def related(a, b):
    """Conservative near-configuration grouping, including across different seeds (units match scenario.json)."""
    if a["family"] != b["family"] or a["profile"] != b["profile"]:
        return False
    if abs(a["speed_limit_kmh"] - b["speed_limit_kmh"]) > 10 or abs(a["density_per_km_lane"] - b["density_per_km_lane"]) > 3:
        return False
    if sum(abs(a["mix"].get(k, 0) - b["mix"].get(k, 0)) for k in sc.VTYPES) > 0.25:
        return False
    if len(a["potholes"]) != len(b["potholes"]):
        return False
    for x, y in zip(a["potholes"], b["potholes"]):
        if x["lane"] != y["lane"] or x["severity"] != y["severity"] or abs(x["at_m"] - y["at_m"]) > 300:
            return False
    ea, eb = a["event"], b["event"]
    if not ea or not eb:
        return ea == eb
    if ea["kind"] != eb["kind"]:
        return False
    limits = {"at_m": 300, "gap_m": 10, "decel_mps2": 2, "to_kmh": 10, "hold_s": 2,
              "speed_delta_kmh": 5, "time_gap_s": 0.2, "duration_s": 5}
    return all(abs(ea[k] - eb[k]) <= limits[k] if k in limits else ea[k] == eb[k] for k in ea)


def freeze_plan(configs):
    parents = list(range(len(configs)))
    def root(i):
        while parents[i] != i:
            i = parents[i]
        return i
    for i, a in enumerate(configs):
        for j, b in enumerate(configs[:i]):
            if a["configuration_id"] == b["configuration_id"] or related(a, b):
                parents[root(i)] = root(j)
    groups = {}
    for i, p in enumerate(configs):
        groups.setdefault(root(i), []).append(p)
    group_ids = {k: min(p["configuration_id"] for p in ps) for k, ps in groups.items()}
    assignments = {}
    # Family-stratified allocation, fixed before observing outcomes. Stress is a separate unseen configuration.
    for family in sc.FAMILIES:
        keys = sorted([k for k, ps in groups.items() if ps[0]["family"] == family],
                      key=lambda k: hashlib.sha256(("phase6d-v1:" + group_ids[k]).encode()).hexdigest())
        for rank, k in enumerate(keys):
            split = "stress" if rank == 0 else "test" if rank == 1 else "validation" if rank == 2 else "train"
            assignments[k] = split
    return {p["configuration_id"]: {"group": group_ids[root(i)], "split": assignments[root(i)]}
            for i, p in enumerate(configs)}


def episodes(values, key):
    count, last_positive = 0, None
    for o in values:
        if o[key]:
            if last_positive is None or o["t"] - last_positive > 1.0 + 0.002:
                count += 1
            last_positive = o["t"]
    return count


def export(campaign, out):
    if out.exists():
        raise FileExistsError("export never overwrites: %s" % out)
    plan = json.loads((campaign / "plan.json").read_text())
    assignments = plan["assignments"]
    records = [json.loads(l) for l in (campaign / "campaign.jsonl").read_text().splitlines()]
    ids = [r["run_id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate execution identity")
    conn = sqlite3.connect("file:%s?mode=ro" % (campaign / "history/roadsense-history.sqlite3"), uri=True)
    out.mkdir(parents=True)
    counts, exclusions, missing, split_samples = Counter(), Counter(), Counter(), Counter()
    families, run_reports = {}, []
    files = {}
    with open(out / "samples.jsonl", "w") as dest:
        for r in records:
            rid = r["run_id"]
            directory = campaign / "runs" / rid
            if not r["ok"]:
                lost_rows = conn.execute("SELECT count(*) FROM samples s JOIN segments g USING(segment_id) WHERE g.run_id=?", (rid,)).fetchone()[0]
                counts["recorded"] += lost_rows
                exclusions["failed_run_or_recording_loss"] += lost_rows
                run_reports.append({"run_id": rid, "failed": True, "error": r.get("error"), "episodes": {}, "frames_with": {}})
                continue
            p = json.loads((directory / "scenario.json").read_text())
            header, frames, events, end = sc.read_ground_truth(directory / "ground_truth.jsonl")
            if not header or header.get("schema") != "roadsense.groundtruth.v2" or header["run_id"] != rid or header["scenario"] != p or not end:
                raise ValueError("invalid run truth: " + rid)
            truth = {}
            for frame in frames:
                t = tick(frame["t"])
                if t in truth:
                    raise ValueError("duplicate truth timestamp in " + rid)
                if frame.get("missing_physical"):
                    continue
                truth[t] = outcomes(frame)
            rows = conn.execute("SELECT s.sequence,s.sim_s,s.features,s.labels FROM samples s JOIN segments g USING(segment_id) "
                                "WHERE g.run_id=? ORDER BY s.sim_s,s.sequence", (rid,)).fetchall()
            first_proxy = next((t for t in sorted(truth) if truth[t]["collision_proxy"]), math.inf)
            stats = Counter()
            split = assignments[p["configuration_id"]]
            seen = set()
            for seq, sim, features, baseline in rows:
                t = tick(sim)
                if t in seen:
                    raise ValueError("multiple samples at a timestamp in " + rid)
                seen.add(t)
                counts["recorded"] += 1
                if not r["ok"] or r.get("gaps") or r["server"]["missed"] or r["server"]["rejected"] or any(r["history_status"]["lost"].values()) or r["server"]["accepted"] != len(rows) + 1:
                    exclusions["failed_run_or_recording_loss"] += 1
                    continue
                if t not in truth:
                    exclusions["unmatched_truth"] += 1
                    continue
                if t >= first_proxy:
                    exclusions["at_or_after_first_overlap_proxy"] += 1
                    continue
                label = future_label(truth, t)
                if label is None:
                    exclusions["incomplete_future_window"] += 1
                    continue
                f = json.loads(features)
                selected = {k: f[k] for k in INPUTS}
                missing.update(k for k, value in selected.items() if value is None)
                row = {"schema": SCHEMA, "run_id": rid, "configuration_id": p["configuration_id"],
                       "group": split["group"], "split": split["split"], "family": p["family"],
                       "sequence": seq, "sim_s": sim, "features": selected, "labels": label,
                       "baseline": json.loads(baseline), "current_forward_risk": truth[t]["forward_near_miss"]}
                dest.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
                positive = label["forward_risk_3s"]
                stats["positive" if positive else "negative"] += 1
                split_samples[split["split"]] += 1
            fam = families.setdefault(p["family"], Counter())
            fam.update(stats)
            observations = [truth[k] for k in sorted(truth)]
            ev = {k: episodes(observations, k) for k in ("forward_near_miss", "collision_proxy", "tailgated", "unsafe_headway")}
            frame_counts = {k: sum(o[k] for o in observations) for k in ev}
            report = {"run_id": rid, "configuration_id": p["configuration_id"], **split, "samples": len(rows),
                      "truth_frames": len(frames), "valid_truth_frames": len(truth), "labels": dict(stats),
                      "episodes": ev, "frames_with": frame_counts, "termination": end["reason"]}
            run_reports.append(report)
    conn.close()
    configs = {p["configuration_id"]: p for p in plan["configurations"]}
    for name in ("samples.jsonl",):
        files[name] = {"bytes": (out / name).stat().st_size, "sha256": hashlib.sha256((out / name).read_bytes()).hexdigest()}
    valid = sum(split_samples.values())
    report = {"schema": SCHEMA, "horizon_s": HORIZON_S, "features": list(INPUTS), "feature_audit": audit_features(),
              "attempted_runs": len(records), "successful_runs": sum(r["ok"] for r in records),
              "configurations": len(configs), "seeds": sorted({p["seed"] for p in configs.values()}),
              "samples": dict(counts), "supervised_samples": valid, "exclusions": dict(exclusions),
              "families": {k: dict(v) for k, v in families.items()}, "split_samples": dict(split_samples),
              "split_groups": dict(Counter(v["split"] for v in {v["group"]: v for v in assignments.values()}.values())),
              "missing_feature_rates": {k: missing[k] / valid if valid else None for k in INPUTS},
              "episodes": dict(sum((Counter(r["episodes"]) for r in run_reports), Counter())),
              "frames_with": dict(sum((Counter(r["frames_with"]) for r in run_reports), Counter())),
              "recording_quality": {"accepted": sum(r.get("server", {}).get("accepted", 0) for r in records),
                                    "missed": sum(r.get("server", {}).get("missed", 0) for r in records),
                                    "rejected": sum(r.get("server", {}).get("rejected", 0) for r in records)},
              "resources": [r.get("resources") for r in records],
              "sim_s": round(sum(r.get("sim_s") or 0 for r in records), 2),
              "wall_s": round(sum(r.get("wall_s") or 0 for r in records), 2),
              "runs": run_reports, "files": files, "plan_sha256": hashlib.sha256((campaign / "plan.json").read_bytes()).hexdigest(),
              "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "input_schema": "roadsense.forward-inputs.v1", "privileged_inference_features": False, "test_frozen_before_simulation": True}
    (out / "manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    inputs = [campaign / "plan.json", campaign / "campaign.jsonl", campaign / "history/roadsense-history.sqlite3"]
    inputs += sorted((campaign / "source").rglob("*.py"))
    inputs += [p for p in sorted((campaign / "runs").rglob("*")) if p.is_file() and not p.is_symlink() and p.suffix in (".json", ".jsonl", ".wbt", ".xml", ".sumocfg")]
    checks = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + str(p.relative_to(campaign)) for p in inputs]
    checks += [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + os.path.relpath(p, campaign) for p in sorted(out.iterdir())]
    (out / "SHA256SUMS").write_text("\n".join(checks) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    result = export(args.campaign, args.out)
    print(json.dumps({k: result[k] for k in ("attempted_runs", "supervised_samples", "families", "exclusions", "split_groups")}))


if __name__ == "__main__":
    main()
