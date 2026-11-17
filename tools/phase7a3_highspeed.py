"""Phase 7A.3 Stage 9: a bounded recording campaign for high-speed tracking evidence
(docs/evidence/phase7a3/stage9/protocol.md, frozen before any recording).

    PYTHONPATH=src venv/bin/python tools/phase7a3_highspeed.py plan OUT.json   # the configuration list, from code
    PYTHONPATH=src venv/bin/python tools/phase7a3_highspeed.py run PLAN.json   # record each configuration once
    PYTHONPATH=src venv/bin/python tools/phase7a3_highspeed.py coverage CAMPAIGN SLOTS AUDIT   # after recording

Three subsets, each its own campaign directory: diagnostic (may be inspected and used for tuning), evaluation
(untouched until the protocol's single analysis) and stress (sudden cut-ins, reported separately). Each attempt is
scenario_batch.run_one with capture on: its own Webots (batch, fast, no rendering) on SUMO port 8901 and its own
RoadSense server on port 8011, never the open Webots session or the development server. `run` refuses a plan that
differs from the one this code generates and a source tree with uncommitted changes. Every configuration is
attempted once; a failure is recorded and kept, never retried. The campaign stops early after
MAX_CONSECUTIVE_FAILURES failed attempts in a row or WALL_LIMIT_S.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import cutin_staging
import scenario_batch as batch
import scenarios as sc

SCHEMA = "roadsense.phase7a3-stage9-plan.v1"
CAMPAIGN = "phase7a3-s9-%s-20261117"
TAG = "phase7a3-stage9"
LIMITS_KMH = (100, 120)  # expressway limits; odd seeds 100 km/h, even seeds 120 km/h
# vType top speeds raised so traffic can exceed 25 m/s: a car to 130 km/h, a motorcycle to 110 km/h. Trucks (22),
# buses (20) and autorickshaws (13.9 m/s) keep theirs: the slow vehicles of the mix.
FAST_MPS = {"car": 36.1, "motorcycle": 30.6}
ALLOCATION = {  # subset: (builder, seeds)
    "diagnostic": (("expressway", (11, 12)), ("gradual", (11,)), ("assertive", (11,)), ("sudden", (11,)),
                   ("stationary", (11,))),
    "evaluation": (("expressway", tuple(range(101, 115))), ("gradual", tuple(range(101, 107))),
                   ("assertive", tuple(range(101, 107))), ("stationary", tuple(range(101, 105)))),
    "stress": (("sudden", tuple(range(101, 105))),),
}
MAX_CONSECUTIVE_FAILURES = 3
WALL_LIMIT_S = 90 * 60
# protocol.md adequacy, fixed before recording: observable frames within 100 m above 25 m/s, the distinct vehicles
# and runs behind them, and observable frames within 100 m above 30 m/s
ADEQUACY = {"frames_25": 2000, "vehicles_25": 100, "runs_25": 10, "frames_30": 200}


def finish(p, variant, seed):
    limit = LIMITS_KMH[seed % 2 == 0]
    p.pop("configuration_id", None)
    p.update(variant="%s_%d" % (variant, limit), speed_limit_kmh=limit, vtype_max_speed_mps=FAST_MPS)
    p["configuration_id"] = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:20]
    return p


def build(builder, seed):
    if builder == "expressway":  # traffic only: fast, slow, oncoming, overtaking and neighbouring-lane vehicles
        p = sc.draw("normal", seed)
        p["ego_speed_kmh"] = 80
        return finish(p, "expressway", seed)
    if builder == "stationary":  # a lead that brakes to a standstill and stays there
        p = sc.draw("sudden_brake", seed)
        p["event"].update(to_kmh=0, hold_s=max(6.0, p["event"]["hold_s"]))
        p["ego_speed_kmh"] = 80
        return finish(p, "stationary_lead", seed)
    return finish(cutin_staging.draw(builder, seed), "cutin_v2_" + builder, seed)  # gradual, assertive, sudden


def plan():
    rows = [{"subset": subset, "builder": builder, "params": build(builder, seed)}
            for subset, groups in ALLOCATION.items() for builder, seeds in groups for seed in seeds]
    return {"schema": SCHEMA, "campaign": CAMPAIGN, "tag": TAG, "limits_kmh": LIMITS_KMH, "fast_mps": FAST_MPS,
            "attempt_limit": len(rows), "max_consecutive_failures": MAX_CONSECUTIVE_FAILURES,
            "wall_limit_s": WALL_LIMIT_S, "configurations": rows}


def checksums(campaign):
    names = ("scenario.json", "ground_truth.jsonl", "sensor_frames.jsonl")
    lines = ["%s  %s\n" % (hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(campaign))
             for p in sorted(campaign.glob("runs/*/*")) if p.name in names]
    (campaign / "SHA256SUMS").write_text("".join(lines))
    return len(lines)


def run(plan_path):
    frozen = json.loads(Path(plan_path).read_text())
    if frozen != json.loads(json.dumps(plan())):
        sys.exit("the plan differs from the one this code generates: %s" % plan_path)
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=batch.ROOT):
        sys.exit("the source tree has uncommitted changes: record from a clean checkout of the frozen revision")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=batch.ROOT, text=True).strip()
    plan_sha = hashlib.sha256(Path(plan_path).read_bytes()).hexdigest()
    started, failures = time.monotonic(), 0
    for subset in ALLOCATION:
        campaign = batch.campaign_root() / (CAMPAIGN % subset)
        campaign.mkdir(parents=True, exist_ok=True)
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
                if failures >= MAX_CONSECUTIVE_FAILURES or time.monotonic() - started > WALL_LIMIT_S:
                    print(json.dumps({"stopped": "%d failures in a row" % failures if failures else "wall limit"}))
                    return
                try:
                    record = batch.run_one(campaign, p["family"], p["seed"], p["profile"], tag=TAG, parameters=p,
                                           capture=True)
                except Exception as error:  # recorded and kept, never retried
                    record = {"configuration_id": p["configuration_id"], "family": p["family"], "seed": p["seed"],
                              "ok": False, "error": str(error)}
                record.update(variant=p["variant"], subset=subset, builder=row["builder"], plan_sha256=plan_sha)
                batch.append(campaign, record)
                attempted.add(p["configuration_id"])
                failures = 0 if record.get("ok") else failures + 1
                print(json.dumps({k: record.get(k) for k in ("run_id", "variant", "subset", "ok", "samples",
                                                             "wall_s", "error")}), flush=True)
            print(subset, "checksummed files:", checksums(campaign), flush=True)


def coverage(campaign, slots, audit):
    """Observable frames exactly as tools/perception_audit.py counts them (T2's denominator: the true vehicle of a
    slot, seen by a radar for CONFIRM_ALLOWANCE_S), with the distinct vehicles and runs behind them, per 5 m/s true
    speed band, within 100 m and at any range. Refuses to report if its frame counts differ from the audit's."""
    import collections
    import perception_audit as pa
    from roadsense.autonomy import perception
    times = collections.defaultdict(list)
    for line in open(slots):
        r = json.loads(line)
        times[r["run_id"]].append(r["sim_s"])
    log = campaign / "campaign.jsonl"
    builders = {r.get("run_id"): r.get("builder") for r in map(json.loads, log.read_text().splitlines())} \
        if log.exists() else {}
    cells = {scope: collections.defaultdict(collections.Counter) for scope in ("within_100m", "any_range")}
    for run_dir in sorted((campaign / "runs").iterdir()):
        scenario = json.loads((run_dir / "scenario.json").read_text())
        run_id = scenario["run_id"]
        if run_id not in times or not (run_dir / "ground_truth.jsonl").exists():
            continue
        perception.use_road(scenario["profile"])
        truth, visible_since = pa.truth_frames(run_dir / "ground_truth.jsonl"), {}
        for sim_s in times[run_id]:  # the audit's loop, without the tracker's slots
            if round(sim_s, 1) not in truth:
                continue
            ego, vehicles = truth[round(sim_s, 1)]
            near = [v for v in vehicles if abs(v["centre"][0] - ego["x"]) < 160]
            rel, vis = pa.relative(ego, near), pa.visibility(ego, near)
            for vid in rel:
                if vis[vid].startswith("radar"):
                    visible_since.setdefault(vid, sim_s)
                else:
                    visible_since.pop(vid, None)
            for _slot, lane, ahead in pa.SLOTS:
                want = pa.truth_slot(rel, lane, ahead)
                if want is None or not vis[want].startswith("radar") or \
                        sim_s - visible_since.get(want, sim_s) < pa.CONFIRM_ALLOWANCE_S:
                    continue
                r = rel[want]
                band = "%02d-%02d m/s" % (5 * int(r["speed"] // 5), 5 * int(r["speed"] // 5) + 5)
                for scope in ("any_range", "within_100m") if abs(r["lon"]) < 100 else ("any_range",):
                    cells[scope][band][(run_id, want)] += 1  # frames per (run, true vehicle)
    seen = json.loads(Path(audit).read_text())["missed_after_confirmation_allowance_by"]
    for scope, key in (("within_100m", "speed_100m"), ("any_range", "speed")):
        if {b: sum(c.values()) for b, c in cells[scope].items()} != \
                {b: v["observable_frames"] for b, v in seen[key].items()}:
            sys.exit("coverage disagrees with the audit's observable frames (%s): not reported" % scope)

    def count(frames):
        return {"observable_frames": sum(frames.values()), "vehicles": len(frames),
                "runs": len({run for run, _ in frames})}

    def above(scope, speed, runs=None):
        frames = sum((c for b, c in cells[scope].items() if int(b[:2]) >= speed), collections.Counter())
        return count(frames if runs is None else collections.Counter({k: n for k, n in frames.items() if k[0] in runs}))
    near25, near30 = above("within_100m", 25), above("within_100m", 30)
    return {"schema": "roadsense.stage9-coverage.v1", "observable": "tools/perception_audit.py, cross-checked",
            "bands": {scope: {b: count(c) for b, c in sorted(cells[scope].items())} for scope in cells},
            "above_25_mps": {s: above(s, 25) for s in cells}, "above_30_mps": {s: above(s, 30) for s in cells},
            "above_25_mps_within_100m_by_builder": {
                b: above("within_100m", 25, {r for r, x in builders.items() if x == b})
                for b in sorted({x for x in builders.values() if x})},
            "adequacy_thresholds": ADEQUACY,
            "adequate": near25["observable_frames"] >= ADEQUACY["frames_25"] and near25["vehicles"] >=
            ADEQUACY["vehicles_25"] and near25["runs"] >= ADEQUACY["runs_25"] and
            near30["observable_frames"] >= ADEQUACY["frames_30"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        sub.add_parser(name).add_argument("path", type=Path)
    cov = sub.add_parser("coverage")
    for arg in ("campaign", "slots", "audit"):
        cov.add_argument(arg, type=Path)
    args = ap.parse_args()
    if args.command == "plan":
        if args.path.exists():
            sys.exit("exists, never overwritten: %s" % args.path)
        args.path.parent.mkdir(parents=True, exist_ok=True)
        args.path.write_text(json.dumps(plan(), indent=1, sort_keys=True) + "\n")
        print(args.path)
    elif args.command == "coverage":
        print(json.dumps(coverage(args.campaign, args.slots, args.audit), indent=1))
    else:
        run(args.path)


if __name__ == "__main__":
    main()
