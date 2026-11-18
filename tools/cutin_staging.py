"""Phase 7A.3 Stage 2: cut-in staging v2, with physically varied lateral motion, speed, distance and warning time.

In Phase 7A.2 every cutter was inserted close beside the ego (2-12 m) and moved across 0.6-3 s later, so the warning
a sensor could give was mostly shorter than the 3 s horizon (docs/phase7a3-plan.md §3.6). v2 draws what a driver
would see instead: how long the cutter is in view in its own lane before it moves (observe), the gap and speed
difference when it starts, and how quickly it crosses (the sumo_supervisor lane-change time, whose sinusoidal
profile and 2.5 m/s lateral-speed limit Webots applies to the physical vehicle). The cutter is inserted where it
reaches the drawn gap after the drawn observation time. "sudden" keeps the intrinsically hard cut-ins, for stress
sets only. Generation only: the scenario plugin stages v2 events exactly as v1 ones (gap_m, settle_s).

    PYTHONPATH=src python3 tools/cutin_staging.py draw gradual 1      # one configuration, printed
    PYTHONPATH=src python3 tools/cutin_staging.py measure RUN_DIR     # staged versus observed timing of a run
    PYTHONPATH=src venv/bin/python tools/cutin_staging.py check --campaign NAME --seeds 1-2   # a few isolated runs
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios as sc  # noqa: E402

SCHEMA = "roadsense.cut-in-staging.v2"
LANE_M = 3.75
MAX_LATERAL_MPS = 2.5  # sumo_supervisor's default maximum lateral speed
SPAWN_MAX_M = 110.0  # inserted well inside the front radar's 150 m range
KINDS = {  # observe s, gap at the start of the manoeuvre m, speed difference km/h, lane-change time s, types
    "gradual": {"observe_s": (3.0, 6.0), "act_gap_m": (10.0, 30.0), "delta_kmh": (-15.0, 3.0),
                "lane_change_s": (3.0, 5.0), "types": ("car", "autorickshaw", "motorcycle", "truck")},
    "assertive": {"observe_s": (1.5, 3.0), "act_gap_m": (5.0, 18.0), "delta_kmh": (-20.0, 0.0),
                  "lane_change_s": (2.0, 3.0), "types": ("car", "autorickshaw", "motorcycle")},
    "sudden": {"observe_s": (0.4, 1.0), "act_gap_m": (2.0, 10.0), "delta_kmh": (-30.0, -8.0),
               "lane_change_s": (1.0, 1.5), "types": ("motorcycle", "autorickshaw"), "stress_only": True},
}


def lateral_entry_s(lane_change_s, width_m, step_s=0.01):
    """Seconds from the start of the lateral move until the cutter's near edge crosses into the ego lane, with the
    supervisor's sinusoidal profile capped at MAX_LATERAL_MPS."""
    y = t = 0.0
    edge = LANE_M / 2 - width_m / 2  # lateral travel until the near edge reaches the lane line
    while y < edge:
        t += step_s
        target = LANE_M * (0.5 - 0.5 * math.cos(math.pi * min(1.0, t / lane_change_s)))
        y = min(target, y + MAX_LATERAL_MPS * step_s)
    return round(t, 2)


def draw(kind, seed, profile="highway_lht"):
    spec = KINDS[kind]
    rng = random.Random("phase7a3-cutin-v2:%s:%d" % (kind, seed))
    p = sc.draw("cut_in", seed, profile)
    observe, act_gap = rng.uniform(*spec["observe_s"]), rng.uniform(*spec["act_gap_m"])
    delta = rng.uniform(*spec["delta_kmh"])
    lane_change = rng.uniform(*spec["lane_change_s"])
    vtype = rng.choice(spec["types"])
    spawn_gap = min(SPAWN_MAX_M, max(act_gap, act_gap - delta / 3.6 * observe))  # closing: inserted farther ahead
    ego_kmh = rng.choice((40, 55, 70, 80))
    width = {"motorcycle": 0.9, "autorickshaw": 1.4, "car": 1.8, "truck": 2.5}[vtype]
    p.update(variant="cutin_v2_" + kind, duration_s=round(rng.uniform(95, 115), 1), ego_speed_kmh=ego_kmh,
             speed_limit_kmh=rng.choice((60, 80, 100)), density_per_km_lane=round(rng.uniform(3, 12), 1),
             lane_change_delay_s=round(lane_change, 2), potholes=[])
    at = round(rng.uniform(250, 500), 1)
    p["event"] = {"kind": "cut_in", "at_m": at, "gap_m": round(spawn_gap, 1), "side": rng.choice(("left", "right")),
                  "speed_delta_kmh": round(delta, 1), "settle_s": round(observe, 2), "vehicle_type": vtype,
                  "lane_change_mode": 0, "request_s": 6.0, "release_after_s": 8.0,
                  "staging": {"schema": SCHEMA, "kind": kind, "observe_s": round(observe, 2),
                              "act_gap_m": round(act_gap, 1), "lane_change_s": round(lane_change, 2),
                              "design_entry_s": lateral_entry_s(lane_change, width),
                              "design_warning_s": round(observe + lateral_entry_s(lane_change, width), 2),
                              "stress_only": spec.get("stress_only", False)}}
    p["configuration_id"] = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:20]
    return p


def measure(run_dir):
    """Staged and observed cut-in timing from a run's ground truth (evaluation only)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import scenario_dataset as ds
    header, frames, events, end = sc.read_ground_truth(Path(run_dir) / "ground_truth.jsonl")
    staging = (header["scenario"].get("event") or {}).get("staging")
    when = {e["what"]: e["t"] for e in events}
    cutter = next((e.get("vehicle") for e in events if e["what"] == "inserted"), None)
    out = {"run_id": header["run_id"], "staging": staging, "events": when, "cutter": cutter}
    if cutter is None:
        return out
    onset = entry = near = None
    previous = None
    corrected, _ = ds.correct_initialization(frames)
    for f in corrected:
        v = next((v for v in f["vehicles"] if v["id"] == cutter), None)
        if v is None:
            previous = None
            continue
        lat = abs(sc._ego_frame(f["ego"], v["x"], v["y"])[1])
        if previous is not None and f["t"] >= when.get("act", math.inf):
            if onset is None and abs(lat - previous[1]) / (f["t"] - previous[0]) > 0.3:
                onset = f["t"]
            if entry is None and lat - v["width"] / 2 < LANE_M / 2:
                entry = f["t"]
        previous = (f["t"], lat)
        if near is None and ds.outcomes(dict(f, vehicles=[v]))["forward_near_miss"]:
            near = f["t"]
    inserted = when.get("inserted")
    out.update(lateral_onset_s=onset, lane_entry_s=entry, first_near_miss_s=near,
               observed={"inserted_to_act": round(when["act"] - inserted, 2) if "act" in when else None,
                         "onset_to_entry": round(entry - onset, 2) if entry and onset else None,
                         "inserted_to_entry": round(entry - inserted, 2) if entry else None,
                         "inserted_to_near_miss": round(near - inserted, 2) if near else None})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draw")
    d.add_argument("kind", choices=sorted(KINDS))
    d.add_argument("seed", type=int)
    m = sub.add_parser("measure")
    m.add_argument("run_dir", type=Path)
    c = sub.add_parser("check", help="stage each kind in isolated Webots runs (with frame capture) and measure them")
    c.add_argument("--campaign", required=True)
    c.add_argument("--seeds", default="1-2")
    args = ap.parse_args()
    if args.command != "check":
        print(json.dumps(draw(args.kind, args.seed) if args.command == "draw" else measure(args.run_dir), indent=1))
        return
    import scenario_batch as batch
    if Path(args.campaign).name != args.campaign or args.campaign in (".", ".."):
        ap.error("campaign must be a simple directory name")
    campaign = batch.campaign_root() / args.campaign
    campaign.mkdir(parents=True, exist_ok=True)
    for seed in batch.seeds(args.seeds):
        for kind in KINDS:
            p = draw(kind, seed)
            record = batch.run_one(campaign, p["family"], p["seed"], p["profile"], tag="cutin-v2-check",
                                   parameters=p, capture=True)
            record["staging_check"] = measure(campaign / "runs" / record["run_id"]) if record.get("ok") else None
            batch.append(campaign, record)
            print(json.dumps({"kind": kind, "seed": seed, "ok": record.get("ok"), "error": record.get("error"),
                              "check": record["staging_check"]}), flush=True)


if __name__ == "__main__":
    main()
