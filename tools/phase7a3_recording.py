"""Phase 7A.3 Stage 1: re-run the 30 Phase 7A.2 test and stress configurations once, recording raw sensor frames.

    PYTHONPATH=src venv/bin/python tools/phase7a3_recording.py [--campaign phase7a3-recording-20261117]

Each attempt is scenario_batch.run_one with capture on: its own Webots (batch, fast, no rendering) on SUMO port 8901
and its own RoadSense server on port 8011, never the open Webots session or the development server. A run's
directory holds sensor_frames.jsonl (every frame the ego sent, byte for byte), ground_truth.jsonl (simulator truth,
evaluation only) and the server's history (tracker-derived slot features and decisions). These configurations were
already evaluated in Phase 7A.2: this is diagnostic regression data, not an untouched test set. One attempt per
configuration: a failure is recorded and kept, never retried here.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import sys

import scenario_batch as batch

SOURCE_CAMPAIGN = "phase7a2-20261117"
PLAN_SHA256 = "712bde92f3c369065182608e0038e84cd2e7612943a0110905851cf3c8a3cdb6"  # docs/evidence/phase7a2/frozen-splits.json
SPLITS = ("test", "stress")
TAG = "phase7a3-recording"


def configurations():
    path = batch.campaign_root() / SOURCE_CAMPAIGN / "plan.json"
    if hashlib.sha256(path.read_bytes()).hexdigest() != PLAN_SHA256:
        sys.exit("the Phase 7A.2 plan changed: %s" % path)
    plan = json.loads(path.read_text())
    chosen = [(plan["assignments"][p["configuration_id"]]["split"], p) for p in plan["configurations"]
              if plan["assignments"][p["configuration_id"]]["split"] in SPLITS]
    if len(chosen) != 30:
        sys.exit("expected 30 test and stress configurations, found %d" % len(chosen))
    return chosen


def checksums(campaign):
    names = ("scenario.json", "ground_truth.jsonl", "sensor_frames.jsonl")
    lines = ["%s  %s\n" % (hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(campaign))
             for p in sorted(campaign.glob("runs/*/*")) if p.name in names]
    (campaign / "SHA256SUMS").write_text("".join(lines))
    return len(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--campaign", default="phase7a3-recording-20261117")
    args = ap.parse_args()
    if Path(args.campaign).name != args.campaign or args.campaign in (".", ".."):
        ap.error("campaign must be a simple directory name")
    campaign = batch.campaign_root() / args.campaign
    campaign.mkdir(parents=True, exist_ok=True)
    with (campaign / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log = campaign / "campaign.jsonl"
        attempted = {json.loads(line)["configuration_id"] for line in log.read_text().splitlines()} if log.exists() else set()
        for split, p in configurations():
            if p["configuration_id"] in attempted:
                continue
            try:
                record = batch.run_one(campaign, p["family"], p["seed"], p["profile"], tag=TAG, parameters=p, capture=True)
            except Exception as error:  # recorded and kept; the next configuration still runs
                record = {"configuration_id": p["configuration_id"], "family": p["family"], "seed": p["seed"],
                          "ok": False, "error": str(error)}
            record.update(variant=p["variant"], source_split=split, source_campaign=SOURCE_CAMPAIGN)
            batch.append(campaign, record)
            attempted.add(p["configuration_id"])
            print(json.dumps({k: record.get(k) for k in ("run_id", "variant", "source_split", "ok", "samples",
                                                         "sensor_frames", "error")}), flush=True)
        print("checksummed files:", checksums(campaign))


if __name__ == "__main__":
    main()
