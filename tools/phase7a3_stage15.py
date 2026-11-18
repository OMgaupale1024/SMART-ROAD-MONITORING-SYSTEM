"""Phase 7A.3 Stage 15: the corrected campaign, planned and estimated but NOT recorded. Recording needs the user's
approval of the frozen plan's SHA-256 (docs/evidence/phase7a3/stage15/protocol.md).

    PYTHONPATH=src:tools venv/bin/python tools/phase7a3_stage15.py plan OUT.json
    PYTHONPATH=src:tools venv/bin/python tools/phase7a3_stage15.py estimate PLAN.json OUT.json
    PYTHONPATH=src:tools venv/bin/python tools/phase7a3_stage15.py calibrate OUT.json  # the estimator vs Stage 9
    PYTHONPATH=src:tools venv/bin/python tools/phase7a3_stage15.py run SUBSET PLAN.json --approved SHA256
    PYTHONPATH=src:tools venv/bin/python tools/phase7a3_stage15.py feasibility PLAN.json ESTIMATE.json

Stage 9 fell short of its coverage because scenarios.routes() starts all drawn traffic ahead of the ego. The ego
starts 40 m along a 5 km carriageway, so nothing came from behind, and fast traffic ahead simply pulled away.
Every category here therefore adds planned_traffic (scenarios.planned_traffic): fixed-speed vehicles in the ego's
neighbouring lanes. Some enter at the carriageway's start behind the ego, timed to draw level with it during the run.
Others are placed ahead of it, slower, so it overtakes them.

Vehicles are counted the way tools/perception_audit.py counts its T2 denominator, through the same functions
phase7a3_highspeed.coverage uses. A vehicle is "present" in a slot if it is that slot's true vehicle (its lane,
-100 to 150 m). It is "observable" if a radar has also seen it for CONFIRM_ALLOWANCE_S.

Trajectories come from a SUMO-only dry run of each configuration (sumo_frames): its own network, routes and seed, with
the ego as a SUMO vehicle cruising in lane 2, as the recorded Stage 9 ego mostly did (22.22 m/s through most
expressway runs). No Webots, radar or RoadSense runs and nothing is recorded. Staged events (scenario_plugin) are
not reproduced, so the cutters and stopping leads are missing from the counts. A constant-speed kinematic estimate
was tried first and dropped: on Stage 9 it predicted 182 observable frames above 25 m/s where 1,367 were recorded,
because most real coverage comes from traffic dynamics (vehicles held up behind slower ones, then released near the
ego). calibrate measures the dry run against the recorded Stage 9 evaluation subset.
"""
import argparse
import collections
import fcntl
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import cutin_staging
import perception_audit as pa
import phase7a3_highspeed as s9
import scenario_batch as batch
import scenarios as sc
from roadsense.autonomy import perception

SCHEMA = "roadsense.phase7a3-stage15-plan.v1"
CAMPAIGN = "phase7a3-s15-%s"
TAG = "phase7a3-stage15"
LIMITS_KMH, FAST_MPS, ADEQUACY = s9.LIMITS_KMH, s9.FAST_MPS, s9.ADEQUACY  # Stage 9's, unchanged
EGO_ACCEL_MPS2 = 2.0  # the estimator's ego from rest (the recorded ego reaches 80 km/h in about 11 s)
LANES = (1, 3)  # planned traffic keeps to the neighbouring lanes: in the ego's own it would queue behind it
FAST = {"speeds": (27.0, 36.0), "types": ("car", "car", "car", "motorcycle")}
EGO_KMH = {"overtaken_fast": 80, "similar_speed": 90, "ego_overtakes": 90, "dense": 80, "stationary": 80}
CUTINS = ("gradual", "assertive", "sudden")
ALLOCATION = {  # subset: ((builder, seeds), ...); every configuration is attempted once
    "feasibility": (("overtaken_fast", (201,)), ("similar_speed", (201,)), ("ego_overtakes", (201,)),
                    ("dense", (201,))),
    "development": (("overtaken_fast", (211, 212)), ("similar_speed", (211,)), ("ego_overtakes", (211,)),
                    ("stationary", (211,)), ("gradual", (211,)), ("assertive", (211,)), ("dense", (211,))),
    "evaluation": (("overtaken_fast", tuple(range(301, 309))), ("similar_speed", tuple(range(301, 305))),
                   ("ego_overtakes", tuple(range(301, 305))), ("stationary", tuple(range(301, 305))),
                   ("gradual", tuple(range(301, 305))), ("assertive", tuple(range(301, 305))),
                   ("dense", tuple(range(301, 305)))),
    "stress": (("sudden", tuple(range(301, 305))),),
}
SUBSETS = tuple(ALLOCATION)
MAX_CONSECUTIVE_FAILURES = 3
WALL_LIMIT_S = {"feasibility": 15 * 60, "development": 30 * 60, "evaluation": 90 * 60, "stress": 15 * 60}
FEASIBILITY_SHARE = 0.5  # the other subsets wait for feasibility's recorded coverage to reach half the dry run's


def ego_at(t, cruise):
    """The estimator's planned ego, in metres along edge "0": from rest at EGO_ACCEL_MPS2 to its cruise speed."""
    t_acc = cruise / EGO_ACCEL_MPS2
    return 40.0 + 0.5 * EGO_ACCEL_MPS2 * min(t, t_acc) ** 2 + cruise * max(0.0, t - t_acc)


def from_behind(rng, n, arrive, cruise, speeds=FAST["speeds"], types=FAST["types"], lanes=LANES, platoon=1):
    """n vehicles (platoons of `platoon`, 1.2 s apart) entering at the start of the ego's carriageway behind it, at
    fixed speeds, timed to draw level with the planned ego between arrive[0] and arrive[1] s."""
    drawn = []
    for _ in range(n):
        kind = rng.choice(types)
        speed = round(min(rng.uniform(*speeds), FAST_MPS.get(kind, math.inf)), 1)
        a = rng.uniform(*arrive)
        drawn.append([max(0.0, a - ego_at(a, cruise) / speed), rng.choice(lanes), kind, speed, a])
    out, free = [], {}
    for depart, lane, kind, speed, a in sorted(drawn):  # SUMO inserts at the edge start one vehicle at a time
        if depart < free.get(lane, 0.0):  # held back for the one before it: a little faster, the same arrival
            depart = free[lane]
            speed = round(ego_at(a, cruise) / (a - depart), 1)
            if speed > min(speeds[1] + 1.0, FAST_MPS.get(kind, math.inf)):
                continue  # it could no longer draw level in time: left out
        out += [{"type": kind, "lane": lane, "depart_s": round(depart + 1.2 * k, 1), "pos_m": 0.0, "speed_mps": speed}
                for k in range(platoon)]
        free[lane] = depart + 1.2 * (platoon - 1) + 1.5
    return out


def ahead_of(rng, n, arrive, cruise, speeds, types, lanes=LANES):
    """n slower vehicles placed ahead of the ego at the start, at fixed speeds, so the planned ego draws level with
    each between arrive[0] and arrive[1] s (never nearer its start than 110 m, the drawn traffic's clearance)."""
    out = []
    for _ in range(n):
        speed, a = round(rng.uniform(*speeds), 1), rng.uniform(*arrive)
        out.append({"type": rng.choice(types), "lane": rng.choice(lanes), "depart_s": 0.0,
                    "pos_m": round(min(4900.0, max(110.0, ego_at(a, cruise) - speed * a)), 1), "speed_mps": speed})
    return out


def finish(p, builder, seed, traffic):
    limit = LIMITS_KMH[seed % 2 == 0]
    p.pop("configuration_id", None)
    p.update(variant="s15_%s_%d" % (builder, limit), speed_limit_kmh=limit, vtype_max_speed_mps=FAST_MPS,
             planned_traffic=traffic)
    p["configuration_id"] = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:20]
    return p


def build(builder, seed):
    rng = random.Random("phase7a3-s15:%s:%d" % (builder, seed))
    if builder in CUTINS:
        p = cutin_staging.draw(builder, seed)  # the staging's own ego speed and cut-in
    elif builder == "stationary":  # a lead braking to a standstill and holding, as in Stage 9
        p = sc.draw("sudden_brake", seed)
        p["event"].update(to_kmh=0, hold_s=max(6.0, p["event"]["hold_s"]))
    else:
        p = sc.draw("normal", seed)
    if builder in EGO_KMH:
        p["ego_speed_kmh"] = EGO_KMH[builder]
    cruise, end = p["ego_speed_kmh"] / 3.6, p["duration_s"] - 25.0
    if builder == "overtaken_fast":  # approaching fast from behind and overtaking: rear-to-front transitions
        traffic = from_behind(rng, 14, (15.0, end), cruise)
    elif builder == "similar_speed":  # a little faster from behind, a little slower ahead: long spells alongside
        traffic = from_behind(rng, 6, (15.0, end - 30.0), cruise, (cruise + 0.8, cruise + 2.5), ("car", "car", "motorcycle")) \
            + ahead_of(rng, 3, (30.0, end), cruise, (cruise - 2.5, cruise - 0.8), ("car", "truck", "car"))
    elif builder == "ego_overtakes":  # slower traffic ahead: front-to-rear transitions
        traffic = ahead_of(rng, 10, (15.0, end), cruise, (11.0, 19.0), ("truck", "bus", "autorickshaw", "car")) \
            + from_behind(rng, 4, (15.0, end), cruise)
    elif builder == "dense":  # dense, closely spaced: platoons of three, 1.2 s apart, among heavy drawn traffic
        p["density_per_km_lane"] = round(rng.uniform(18, 24), 1)
        traffic = from_behind(rng, 5, (15.0, end), cruise, platoon=3)
    elif builder == "stationary":
        traffic = from_behind(rng, 6, (15.0, end), cruise)
    else:  # cut-ins: fast traffic from behind, in the lane away from the cutter's (median side is "right")
        traffic = from_behind(rng, 8, (15.0, end), cruise, lanes=(1 if p["event"]["side"] == "right" else 3,))
    return finish(p, builder, seed, traffic)


def plan():
    rows = [{"subset": subset, "builder": builder, "params": build(builder, seed)}
            for subset, groups in ALLOCATION.items() for builder, seeds in groups for seed in seeds]
    return {"schema": SCHEMA, "campaign": CAMPAIGN, "tag": TAG, "limits_kmh": LIMITS_KMH, "fast_mps": FAST_MPS,
            "attempt_limit": len(rows), "max_consecutive_failures": MAX_CONSECUTIVE_FAILURES,
            "wall_limit_s": WALL_LIMIT_S, "adequacy": ADEQUACY, "feasibility_share": FEASIBILITY_SHARE,
            "configurations": rows}


EGO_ROUTE = ('  <vType id="rs_estimate_ego" length="4.9" width="1.9" accel="%.1f" decel="4.5" sigma="0" maxSpeed="%.2f" '
             'speedFactor="1" speedDev="0" lcStrategic="-1" lcCooperative="0" lcSpeedGain="0" lcKeepRight="0"/>\n'
             '  <vehicle id="rs_estimate_ego" type="rs_estimate_ego" route="0" depart="0" departLane="2" '
             'departPos="%.1f" departSpeed="0"/>\n')
SUMO_TO_WEBOTS = (-5000.0, -8.75)  # roadsense_highway.wbt: edge "0" starts at x = -5000, the road is at y = 8.75


def sumo_frames(params, dt=0.2):
    """{t: (ego, vehicles)} in perception_audit.truth_frames' form from a SUMO-only dry run of the scenario: its own
    network, routes and seed, the ego a SUMO vehicle cruising in lane 2 without changing lanes. No Webots, radar or
    RoadSense runs, nothing listens on a port and nothing is recorded. Staged events (plugin) are not reproduced."""
    if params["profile"] != "highway_lht":
        raise ValueError("the dry run models highway_lht only")
    planned = sorted(params.get("planned_traffic") or (), key=lambda v: v["depart_s"])
    size = {name: (sc.vehicle_length(name), float(re.search(r'width="([\d.]+)"', spec).group(1)))
            for name, spec in sc.VTYPES.items()}
    size.update({"planned%d" % i: size[v["type"]] for i, v in enumerate(planned)})
    routes = sc.routes(params)
    first = routes.index("  <vehicle ")
    routes = routes[:first] + EGO_ROUTE % (EGO_ACCEL_MPS2, params.get("ego_speed_kmh", 80) / 3.6,
                                           40.0 + sc.EGO_FRONT_M) + routes[first:]
    frames = {}
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        (work / "net.xml").write_text(sc.network((batch.NET_DIR / "sumo.net.xml").read_text(), params))
        (work / "rou.xml").write_text(routes)
        subprocess.run(["sumo", "-n", str(work / "net.xml"), "-r", str(work / "rou.xml"), "--step-length", str(dt),
                        "--seed", str(params["sumo_seed"]), "--end", str(params["duration_s"]),
                        "--fcd-output", str(work / "fcd.xml"), "--no-step-log", "true", "--no-warnings", "true"],
                       check=True, capture_output=True)
        for _, step in ET.iterparse(work / "fcd.xml"):
            if step.tag != "timestep":
                continue
            ego, vehicles = None, []
            for v in step.iter("vehicle"):
                yaw, speed = math.radians(90.0 - float(v.get("angle"))), float(v.get("speed"))
                x, y = float(v.get("x")) + SUMO_TO_WEBOTS[0], float(v.get("y")) + SUMO_TO_WEBOTS[1]  # the front
                if v.get("id") == "rs_estimate_ego":
                    ego = {"x": x - sc.EGO_FRONT_M, "y": sc.EGO_START_Y, "yaw": 0.0, "v": (speed, 0.0)}
                    continue
                length, width = size[v.get("type")]
                vehicles.append({"id": v.get("id"), "type": v.get("type"), "length": length, "width": width,
                                 "yaw": yaw, "centre": (x - length / 2 * math.cos(yaw), y - length / 2 * math.sin(yaw)),
                                 "v": (speed * math.cos(yaw), speed * math.sin(yaw))})
            if ego is not None and float(step.get("time")) > 0:
                frames[round(float(step.get("time")), 1)] = (ego, vehicles)
            step.clear()
    return frames


def count(frames_by_run):
    """Slot frames present and observable (the audit's T2 denominator) by true speed band, the distinct vehicles and
    runs behind them, stationary leads, spells alongside (|lon| < 5 m, a neighbouring lane) and radar handovers."""
    obs = {scope: collections.defaultdict(collections.Counter) for scope in ("within_100m", "any_range")}
    present = collections.defaultdict(collections.Counter)
    by_range, stationary, transitions = collections.Counter(), collections.Counter(), collections.Counter()
    alongside = collections.Counter()
    for run_id, frames in frames_by_run.items():
        visible_since, last_radar = {}, {}
        for t in sorted(frames):  # the audit's loop (phase7a3_highspeed.coverage)
            ego, vehicles = frames[t]
            near = [v for v in vehicles if abs(v["centre"][0] - ego["x"]) < 160]
            rel, vis = pa.relative(ego, near), pa.visibility(ego, near)
            for vid, r in rel.items():
                if vis[vid].startswith("radar"):
                    visible_since.setdefault(vid, t)
                    if last_radar.get(vid, vis[vid]) != vis[vid]:
                        transitions["%s -> %s" % (last_radar[vid], vis[vid])] += 1
                    last_radar[vid] = vis[vid]
                else:
                    visible_since.pop(vid, None)
                if r["relation"] in ("left_lane", "right_lane") and abs(r["lon"]) < 5:
                    alongside[(run_id, vid)] += 1
            for slot, lane, ahead in pa.SLOTS:
                want = pa.truth_slot(rel, lane, ahead)
                if want is None:
                    continue
                r = rel[want]
                band = "%02d-%02d m/s" % (5 * int(r["speed"] // 5), 5 * int(r["speed"] // 5) + 5)
                present[band][(run_id, want)] += 1
                seen = vis[want].startswith("radar") and t - visible_since.get(want, t) >= pa.CONFIRM_ALLOWANCE_S
                if slot == "lead" and r["speed"] < 0.5:
                    stationary["present"] += 1
                    stationary["observable"] += seen
                if not seen:
                    continue
                for scope in ("any_range", "within_100m") if abs(r["lon"]) < 100 else ("any_range",):
                    obs[scope][band][(run_id, want)] += 1
                by_range["%03d-%03d m" % (25 * int(abs(r["lon"]) // 25), 25 * int(abs(r["lon"]) // 25) + 25)] += 1

    def summary(cells):
        return {"frames": sum(cells.values()), "vehicles": len(cells), "runs": len({run for run, _ in cells})}

    def above(cells, speed):
        return summary(sum((c for b, c in cells.items() if int(b[:2]) >= speed), collections.Counter()))
    return {"observable_within_100m": above(obs["within_100m"], 0), "present_all_speeds": above(present, 0),
            "observable_above_25_within_100m": above(obs["within_100m"], 25),
            "observable_above_30_within_100m": above(obs["within_100m"], 30),
            "present_above_25": above(present, 25),
            "observable": {scope: {b: summary(c) for b, c in sorted(cells.items())} for scope, cells in obs.items()},
            "present": {b: summary(c) for b, c in sorted(present.items())},
            "observable_by_range": dict(sorted(by_range.items())), "stationary_lead_frames": dict(stationary),
            "radar_transitions": dict(transitions), "alongside": summary(alongside)}


def estimate(rows):
    """count() of the dry-run frames per subset and per builder."""
    frames = {r["params"]["configuration_id"]: sumo_frames(r["params"]) for r in rows}
    out = {}
    for key in [s for s in SUBSETS if any(r["subset"] == s for r in rows)] + sorted({r["builder"] for r in rows}):
        chosen = [r["params"]["configuration_id"] for r in rows if key in (r["subset"], r["builder"])]
        out[key] = dict(count({c: frames[c] for c in chosen}), configurations=len(chosen))
    return out


def observed(campaign):
    """count() of a recorded campaign's truth frames (evaluation only; no tracker involved)."""
    frames_by_run = {}
    for run_dir in sorted((campaign / "runs").iterdir()):
        if (run_dir / "ground_truth.jsonl").exists():
            scenario = json.loads((run_dir / "scenario.json").read_text())
            perception.use_road(scenario["profile"])
            frames_by_run[scenario["run_id"]] = pa.truth_frames(run_dir / "ground_truth.jsonl")
    return count(frames_by_run)


def ratio(seen, est):
    keys = ("observable_above_25_within_100m", "observable_above_30_within_100m", "present_above_25")
    return {k: {m: round(seen[k][m] / est[k][m], 3) if est[k][m] else None for m in ("frames", "vehicles", "runs")}
            for k in keys}


def calibrate():
    """The estimator on the Stage 9 evaluation configurations beside that subset's recorded truth, and the recorded
    count checked against Stage 9's own coverage report (the audit's)."""
    rows = [r for r in s9.plan()["configurations"] if r["subset"] == "evaluation"]
    est = count({r["params"]["configuration_id"]: sumo_frames(r["params"]) for r in rows})
    seen = observed(batch.campaign_root() / (s9.CAMPAIGN % "evaluation"))
    report = json.loads((batch.ROOT / "docs/evidence/phase7a3/stage9/evaluation/coverage.json").read_text())
    agrees = {k: seen["observable_above_%s_within_100m" % n] == {"frames": v["observable_frames"],
                                                                "vehicles": v["vehicles"], "runs": v["runs"]}
              for n, k, v in (("25", "above_25_mps", report["above_25_mps"]["within_100m"]),
                              ("30", "above_30_mps", report["above_30_mps"]["within_100m"]))}
    return {"schema": "roadsense.stage15-calibration.v1", "recorded_count_equals_stage9_coverage": agrees,
            "stage9_evaluation_estimate": est, "stage9_evaluation_recorded_truth": seen,
            "recorded_over_estimate": ratio(seen, est)}


def run(subset, plan_path, approved):
    """Record one subset of the frozen plan as Stage 9 did (phase7a3_highspeed.run), only with the approved plan's
    SHA-256, from a clean tree, and after feasibility (for the other subsets) has passed."""
    data = Path(plan_path).read_bytes()
    plan_sha = hashlib.sha256(data).hexdigest()
    if plan_sha != approved:
        sys.exit("not approved: --approved must be the SHA-256 of the frozen plan the user approved")
    frozen = json.loads(data)
    if frozen != json.loads(json.dumps(plan())):
        sys.exit("the plan differs from the one this code generates: %s" % plan_path)
    if subset != "feasibility":
        verdict = batch.campaign_root() / (CAMPAIGN % "feasibility") / "feasibility-verdict.json"
        if not verdict.exists() or not json.loads(verdict.read_text()).get("pass"):
            sys.exit("the feasibility check has not passed (%s)" % verdict)
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=batch.ROOT):
        sys.exit("the source tree has uncommitted changes: record from a clean checkout of the frozen revision")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=batch.ROOT, text=True).strip()
    campaign = batch.campaign_root() / (CAMPAIGN % subset)
    campaign.mkdir(parents=True, exist_ok=True)
    started, failures = time.monotonic(), 0
    with (campaign / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (campaign / "frozen.json").write_text(json.dumps({"plan_sha256": plan_sha, "source_revision": revision,
                                                          "subset": subset}, indent=1) + "\n")
        log = campaign / "campaign.jsonl"
        attempted = {json.loads(line)["configuration_id"] for line in log.read_text().splitlines()} \
            if log.exists() else set()
        for row in (r for r in frozen["configurations"] if r["subset"] == subset):
            p = row["params"]
            if p["configuration_id"] in attempted:
                continue
            if failures >= MAX_CONSECUTIVE_FAILURES or time.monotonic() - started > WALL_LIMIT_S[subset]:
                print(json.dumps({"stopped": "%d failures in a row" % failures if failures else "wall limit"}))
                break
            try:
                record = batch.run_one(campaign, p["family"], p["seed"], p["profile"], tag=TAG, parameters=p,
                                       capture=True)
            except Exception as error:  # recorded and kept, never retried
                record = {"configuration_id": p["configuration_id"], "family": p["family"], "seed": p["seed"],
                          "ok": False, "error": str(error)}
            record.update(variant=p["variant"], subset=subset, builder=row["builder"], plan_sha256=plan_sha)
            batch.append(campaign, record)
            failures = 0 if record.get("ok") else failures + 1
            print(json.dumps({k: record.get(k) for k in ("run_id", "variant", "ok", "wall_s", "error")}), flush=True)
    print(subset, "checksummed files:", s9.checksums(campaign), flush=True)


def feasibility(plan_path, estimate_path):
    """The gate after the feasibility subset: every attempt recorded, and its observed frames and vehicles above
    25 m/s within 100 m at least FEASIBILITY_SHARE of the dry run's. Written beside that campaign, never in the
    source tree."""
    campaign = batch.campaign_root() / (CAMPAIGN % "feasibility")
    log = [json.loads(line) for line in (campaign / "campaign.jsonl").read_text().splitlines()]
    planned = [r for r in json.loads(Path(plan_path).read_text())["configurations"] if r["subset"] == "feasibility"]
    est = json.loads(Path(estimate_path).read_text())["estimate"]["feasibility"]
    seen = observed(campaign)
    checks = {"every configuration recorded": len([r for r in log if r.get("ok")]) == len(planned)}
    key = "observable_above_25_within_100m"
    for m in ("frames", "vehicles"):
        need = FEASIBILITY_SHARE * est[key][m]
        checks["%s %s >= %.1f" % (key, m, need)] = seen[key][m] >= need
    verdict = {"pass": all(checks.values()), "checks": checks, "observed": seen}
    out = campaign / "feasibility-verdict.json"
    if out.exists():
        sys.exit("exists, never overwritten: %s" % out)
    out.write_text(json.dumps(verdict, indent=1, sort_keys=True) + "\n")
    print(out, "pass" if verdict["pass"] else "FAIL")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("plan").add_argument("out", type=Path)
    p = sub.add_parser("estimate")
    p.add_argument("plan", type=Path)
    p.add_argument("out", type=Path)
    sub.add_parser("calibrate").add_argument("out", type=Path)
    p = sub.add_parser("run")
    p.add_argument("subset", choices=SUBSETS)
    p.add_argument("plan", type=Path)
    p.add_argument("--approved", required=True)
    p = sub.add_parser("feasibility")
    for name in ("plan", "estimate"):
        p.add_argument(name, type=Path)
    args = ap.parse_args()
    perception.use_road("highway_lht")
    if args.command == "run":
        return run(args.subset, args.plan, args.approved)
    if args.command == "feasibility":
        return feasibility(args.plan, args.estimate)
    if args.out.exists():
        sys.exit("exists, never overwritten: %s" % args.out)
    if args.command == "plan":
        result = plan()
    elif args.command == "estimate":
        rows = json.loads(args.plan.read_text())["configurations"]
        result = {"schema": "roadsense.stage15-estimate.v1", "estimate": estimate(rows), "adequacy": ADEQUACY}
    else:
        result = calibrate()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(args.out)


if __name__ == "__main__":
    main()
