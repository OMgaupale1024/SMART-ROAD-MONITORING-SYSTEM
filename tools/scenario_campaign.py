"""Bounded Phase 6D campaign: seven smoke runs, then at most 70 total; resumable without overwrites."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import time

import scenario_batch as batch
import scenario_dataset as dataset
import scenarios as sc

MAX_RUNS = 70
MAX_BYTES = 2_000_000_000


def config(family, seed):
    p = sc.draw(family, seed)
    p["configuration_id"] = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:20]
    return p


def plan():
    configs = [config(family, seed) for seed in range(11, 20) for family in sc.FAMILIES]
    assignments = dataset.freeze_plan(configs)
    tasks = [{"family": p["family"], "seed": p["seed"], "repetition": 0,
              "configuration_id": p["configuration_id"]} for p in configs]
    tasks += [dict(t, repetition=1) for t in tasks[:7]]
    assert len(tasks) == MAX_RUNS
    return {"schema": "roadsense.campaign.v1", "max_runs": MAX_RUNS, "max_bytes": MAX_BYTES,
            "configurations": configs, "tasks": tasks, "assignments": assignments,
            "split_policy": "family-stratified groups; exact configurations and transitive near configurations together; frozen before outcomes"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--smoke", action="store_true", help="first seven planned tasks only")
    args = parser.parse_args()
    if Path(args.campaign).name != args.campaign or args.campaign in (".", ".."):
        parser.error("campaign must be a simple directory name")
    campaign = batch.campaign_root() / args.campaign
    campaign.mkdir(parents=True, exist_ok=True)
    plan_path = campaign / "plan.json"
    if not plan_path.exists():
        plan_path.write_text(json.dumps(plan(), sort_keys=True, indent=2) + "\n")
    # Retain source used for regeneration as well as per-run hashes; no credentials/config directories.
    snapshot_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((batch.ROOT / "tools").glob("scenario*.py")))).hexdigest()[:20]
    snapshot = campaign / "source" / snapshot_hash
    if not snapshot.exists():
        for p in list((batch.ROOT / "tools").glob("scenario*.py")) + list((batch.ROOT / "src/roadsense").rglob("*.py")) + [batch.ROOT / "simulation/webots/controllers/roadsense_ego/roadsense_ego.py"]:
            target = snapshot / p.relative_to(batch.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
    frozen = json.loads(plan_path.read_text())
    if frozen != plan():
        raise ValueError("campaign plan differs from source; use a new campaign")
    # A lock prevents another runner from reusing the fixed ports or exceeding this campaign's limit.
    import fcntl
    with open(campaign / "runner.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prior = [json.loads(l) for l in (campaign / "campaign.jsonl").read_text().splitlines()] if (campaign / "campaign.jsonl").exists() else []
        completed = {(r["family"], r["seed"], r["repetition"]) for r in prior}
        # A prepared directory without a campaign line is an interrupted attempt; count and preserve it.
        existing = list((campaign / "runs").glob("*/scenario.json"))
        for path in existing:
            p = json.loads(path.read_text())
            key = (p["family"], p["seed"], p["repetition"])
            if key not in completed:
                batch.append(campaign, {"run_id": p["run_id"], "family": p["family"], "seed": p["seed"],
                                        "repetition": p["repetition"], "ok": False, "error": "interrupted attempt"})
                completed.add(key)
        tasks = frozen["tasks"][:7] if args.smoke else frozen["tasks"]
        start = time.monotonic()
        for task in tasks:
            key = (task["family"], task["seed"], task["repetition"])
            if key in completed:
                continue
            if len(list((campaign / "runs").glob("*/scenario.json"))) >= MAX_RUNS:
                raise RuntimeError("70-attempt limit reached")
            # shortcut: budget checked between runs, add monitoring if a run approaches the 50 MB headroom.
            if batch.size(campaign) >= MAX_BYTES - 50_000_000 or shutil.disk_usage(campaign).free < 1_000_000_000:
                raise RuntimeError("campaign storage budget or free-space guard reached")
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            try:
                record = batch.run_one(campaign, task["family"], task["seed"], "highway_lht", repetition=task["repetition"])
            except Exception as error:
                candidates = [p for p in (campaign / "runs").glob("*/scenario.json")
                              if (json.loads(p.read_text())["family"], json.loads(p.read_text())["seed"],
                                  json.loads(p.read_text())["repetition"]) == key]
                rid = candidates[-1].parent.name if candidates else "unprepared-%s-%s-%s" % key
                record = dict(task, run_id=rid, ok=False, error=str(error))
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            record["resources"] = {"child_user_cpu_s": round(after.ru_utime - before.ru_utime, 3),
                                   "child_system_cpu_s": round(after.ru_stime - before.ru_stime, 3),
                                   "cumulative_peak_child_rss_bytes": after.ru_maxrss}
            batch.append(campaign, record)
            print(json.dumps({k: record.get(k) for k in ("run_id", "family", "seed", "repetition", "ok", "wall_s", "samples", "error")}), flush=True)
            if not record["ok"]:
                raise RuntimeError("campaign paused after failed run; inspect logs before proceeding")
        (campaign / "execution_status.json").write_text(json.dumps({"completed_tasks": len(completed) + sum(
            1 for t in tasks if (t["family"], t["seed"], t["repetition"]) not in completed),
            "planned_tasks": len(frozen["tasks"]), "last_invocation_wall_s": round(time.monotonic() - start, 2)}, indent=2) + "\n")


if __name__ == "__main__":
    main()
