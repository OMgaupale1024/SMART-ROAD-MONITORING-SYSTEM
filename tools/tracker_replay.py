"""Phase 7A.3: verify raw sensor-frame recordings and replay them, offline, through a chosen tracker.

    PYTHONPATH=src venv/bin/python tools/tracker_replay.py verify  CAMPAIGN OUT.json
    PYTHONPATH=src venv/bin/python tools/tracker_replay.py replay  CAMPAIGN OUT_DIR [--tracker legacy|p1|p1p2]
    PYTHONPATH=src venv/bin/python tools/tracker_replay.py compare BASELINE_AUDIT.json CANDIDATE_AUDIT.json
    python3 tools/tracker_replay.py bench CAMPAIGN_OR_BUNDLE OUT.json   # standard library only, for the Pi

verify: per run, the capture's checksum against the campaign log, contiguous sequences, 200 ms timestamps, the frames
the server accepted, the ground-truth join, and a deterministic replay: the frames the server processed, replayed
through the server's own code path (edge.EdgeRun), must give exactly the slot features the server recorded live.
replay: every captured frame through edge.EdgeRun with the tracker swapped in; writes slots (for
perception_audit.py --slots), safety decisions and the tracks. Offline only: the deployed service is never touched.
"""
import argparse
import gzip
import hashlib
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

GAP_KEYS = ("sequence_gap_before", "history_dropped_before", "sim_dt_s")  # history bookkeeping, not tracker output


def make_tracker(name):
    from roadsense.autonomy import tracking
    if name == "legacy":
        return tracking.Tracker()
    if name == "p1":
        return tracking.Tracker(initiation="range_rate")
    if name == "p1p2":
        return tracking.Tracker(initiation="range_rate", estimator="kalman")
    raise ValueError("unknown tracker %r" % name)


TRACKERS = ("legacy", "p1", "p1p2")


def runs(campaign):
    for line in open(campaign / "campaign.jsonl"):
        r = json.loads(line)
        if r.get("ok") and r.get("run_id"):
            yield r


def read_frames(path):
    return [line.rstrip(b"\n") for line in open(path, "rb")]


def replay_run(lines, tracker=None, only=None):
    """[(sequence, state)] for the captured frames (only those sequences, if given), as the server computes them."""
    from roadsense.web import edge
    run, out = None, []
    for line in lines:
        frame = edge.SensorFrame.model_validate_json(line)
        if only is not None and frame.simulation.sequence not in only:
            continue
        if run is None:
            run = edge.EdgeRun(frame.ego.vehicle_id)
            if tracker is not None:
                run.pipeline.tracker = make_tracker(tracker)
        out.append((frame.simulation.sequence, run.update(frame)))
    return out


def verify(campaign, out):
    from roadsense.autonomy import perception
    from roadsense.web import history
    db = sqlite3.connect("file:%s?mode=ro" % (campaign / "history/roadsense-history.sqlite3"), uri=True)
    report, totals = {}, {"runs": 0, "frames": 0, "replayed_samples": 0, "mismatched_samples": 0, "problems": 0}
    for r in runs(campaign):
        run_dir = campaign / "runs" / r["run_id"]
        path = run_dir / "sensor_frames.jsonl"
        problems = []
        if not path.exists():
            report[r["run_id"]] = {"problems": ["no capture"]}
            totals["problems"] += 1
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != (r.get("sensor_frames") or {}).get("sha256"):
            problems.append("checksum differs from campaign.jsonl")
        lines = read_frames(path)
        frames = [json.loads(line) for line in lines]
        seqs = [f["simulation"]["sequence"] for f in frames]
        times = [f["timestamp"] for f in frames]
        if seqs != list(range(1, len(seqs) + 1)):
            problems.append("sequences not contiguous from 1")
        steps = [round(b - a, 3) for a, b in zip(times, times[1:])]
        if any(abs(s - 0.2) > 0.002 for s in steps):
            problems.append("timestamp steps other than 0.2 s: %s" % sorted(set(steps))[:5])
        if {f["simulation"]["run_id"] for f in frames} != {r["run_id"]}:
            problems.append("run id differs inside the capture")
        if r["server"]["accepted"] not in (len(frames), len(frames) + 1):  # + the end-of-run marker
            problems.append("server accepted %d of %d captured frames" % (r["server"]["accepted"], len(frames)))
        gt = {round(json.loads(x)["t"], 2) for x in open(run_dir / "ground_truth.jsonl") if '"record":"frame"' in x}
        joined = sum(1 for t in times if round(t, 2) in gt)
        live = {seq: json.loads(feats) for seq, feats in db.execute(
            "SELECT s.sequence, s.features FROM samples s JOIN segments g USING (segment_id) WHERE g.run_id = ?",
            (r["run_id"],))}
        perception.use_road(r["profile"])
        replayed = replay_run(lines, only=set(live))
        again = replay_run(lines, only=set(live))
        mismatched, example = 0, None
        for (seq, state), (_, state2) in zip(replayed, again):
            got = history.features(state, None, 0, None)
            if got != history.features(state2, None, 0, None):
                problems.append("replay not repeatable at sequence %d" % seq)
                break
            want = live[seq]
            diff = [k for k in want if k not in GAP_KEYS and want[k] != got[k]]
            if diff:
                mismatched += 1
                example = example or {"sequence": seq, "fields": diff[:6]}
        if mismatched:
            problems.append("%d replayed samples differ from the live features" % mismatched)
        report[r["run_id"]] = {"frames": len(frames), "samples_live": len(live), "replayed": len(replayed),
                               "mismatched": mismatched, "example": example, "ground_truth_joined": joined,
                               "duration_s": times[-1] - times[0] if times else None, "problems": problems}
        totals["runs"] += 1
        totals["frames"] += len(frames)
        totals["replayed_samples"] += len(replayed)
        totals["mismatched_samples"] += mismatched
        totals["problems"] += bool(problems)
    db.close()
    result = {"schema": "roadsense.recording-verification.v1", "totals": totals, "runs": report}
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(json.dumps(totals))


def replay(campaign, out_dir, tracker):
    from roadsense.autonomy import perception
    from roadsense.web import history
    out_dir.mkdir(parents=True)
    with open(out_dir / "slots.jsonl", "w") as slots, open(out_dir / "decisions.jsonl", "w") as decisions, \
            gzip.open(out_dir / "tracks.jsonl.gz", "wt") as tracks:
        for r in runs(campaign):
            perception.use_road(r["profile"])
            lines = read_frames(campaign / "runs" / r["run_id"] / "sensor_frames.jsonl")
            for seq, state in replay_run(lines, tracker=tracker):
                t = state["timestamp"]
                key = {"run_id": r["run_id"], "sequence": seq, "sim_s": t}
                slots.write(json.dumps(dict(key, features=history.features(state, None, 0, None)),
                                       separators=(",", ":")) + "\n")
                decisions.write(json.dumps(dict(key, decision=history.decision_values(state)),
                                           separators=(",", ":")) + "\n")
                tracks.write(json.dumps(dict(key, tracks=state.get("tracks")), separators=(",", ":")) + "\n")
    (out_dir / "replay.json").write_text(json.dumps({"tracker": tracker, "campaign": str(campaign)}, indent=1) + "\n")
    print(out_dir)


# Tracking acceptance criteria fixed in docs/phase7a3-plan.md §5 before any implementation: (name, how, limit).
CRITERIA = (
    ("T2", "max missed_tracking rate within 100 m, any speed band", "max_speed_100m", 0.03),
    ("T3", "max missed_tracking rate within 100 m, any vehicle type", "max_type_100m", 0.03),
    ("T4", "max missed_tracking rate 100-150 m (report; radar-limited)", "max_range_far", 0.10),
    ("T5", "identity switches + fragmentations per 1,000 matched slot frames", "identity_per_1000", 1.0),
    ("T6", "wrong_vehicle + spurious share of non-empty adjacent-slot frames (worse side)", "adjacent_wrong", 0.05),
    ("T7a", "v_lat MAE during lateral motion (m/s)", "vlat_motion_mae", 0.30),
    ("T7b", "v_lat p95 during lateral motion (m/s)", "vlat_motion_p95", 1.00),
    ("T8a", "false lateral motion on steady vehicles, tracks < 1.5 s", "false_lateral_young", 0.10),
    ("T8b", "false lateral motion on steady vehicles, all tracks", "false_lateral_all", 0.04),
    ("T9a", "lateral cue latency p50 (s)", "cue_p50", 0.4),
    ("T9b", "lateral cue latency p90 (s)", "cue_p90", 0.8),
)


def compare(baseline, candidate):
    a, b = json.load(open(baseline))["criteria"], json.load(open(candidate))["criteria"]
    rows = []
    for name, text, key, limit in CRITERIA:
        rows.append({"criterion": name, "what": text, "baseline": a[key], "candidate": b[key], "limit": limit,
                     "pass": b[key] is not None and b[key] <= limit})
    for key, name in (("lon_mae", "T10 lon MAE"), ("lat_mae", "T10 lat MAE")):
        rows.append({"criterion": name, "what": "no worse than baseline + 10%", "baseline": a[key], "candidate": b[key],
                     "limit": round(a[key] * 1.1, 3), "pass": b[key] <= a[key] * 1.1})
    print(json.dumps(rows, indent=1))
    return rows


def bench(source, out):
    """Tracker.update cost per radar cycle on captured frames, each tracker, then (a separate pass, as tracing slows
    everything) the most memory the tracker's own objects hold; standard library only."""
    import tracemalloc
    sys.modules.update(pydantic=None, fastapi=None)
    from roadsense.autonomy import perception, tracking
    paths = sorted(Path(source).glob("**/sensor_frames.jsonl"))
    traced = (tracemalloc.Filter(True, tracking.__file__),)

    def cycles(path):
        for f in map(json.loads, open(path)):
            t, ego = round(f["timestamp"], 3), f["ego"]
            pose = (*ego["position"][:2], ego["yaw_rad"])
            detections = []
            for radar, data in f["radars"].items():
                detections += perception.radar_detections(radar, data["targets"], data["limits"], t, pose)
            yield t, detections, pose, ego["velocity"]
    result = {}
    for name in TRACKERS:
        cost, held, most = [], 0, 0
        for path in paths:
            tracker = make_tracker(name)
            for t, detections, pose, velocity in cycles(path):
                start = time.perf_counter_ns()
                tracker.update(t, detections, pose=pose, ego_velocity=velocity)
                cost.append((time.perf_counter_ns() - start) / 1000)
                most = max(most, len(tracker.tracks))
        tracemalloc.start()
        for path in paths:
            tracker = make_tracker(name)
            for k, (t, detections, pose, velocity) in enumerate(cycles(path)):
                tracker.update(t, detections, pose=pose, ego_velocity=velocity)
                if k % 25 == 0:
                    snapshot = tracemalloc.take_snapshot().filter_traces(traced)
                    held = max(held, sum(stat.size for stat in snapshot.statistics("filename")))
            del tracker
        tracemalloc.stop()
        cost.sort()
        result[name] = {"frames": len(cost), "files": len(paths), "mean_us": round(statistics.fmean(cost), 2),
                        **{"p%d_us" % q: round(cost[min(len(cost) - 1, int(q / 100 * len(cost)))], 2)
                           for q in (50, 95, 99)}, "max_us": round(cost[-1], 2),
                        "most_tracks": most, "tracker_memory_peak_kib": round(held / 1024, 1)}
    import platform
    result.update(platform=platform.platform(), python=platform.python_version(), offline=True)
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(json.dumps(result))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name, extra in (("verify", "out"), ("replay", "out"), ("compare", None), ("bench", "out")):
        p = sub.add_parser(name)
        if name == "compare":
            p.add_argument("baseline", type=Path)
            p.add_argument("candidate", type=Path)
            continue
        p.add_argument("campaign", type=Path)
        p.add_argument(extra, type=Path)
        if name == "replay":
            p.add_argument("--tracker", choices=TRACKERS, default="legacy")
    args = ap.parse_args()
    if getattr(args, "out", None) is not None and args.out.exists():
        sys.exit("output exists: %s (never overwritten)" % args.out)
    if args.command == "verify":
        verify(args.campaign, args.out)
    elif args.command == "replay":
        replay(args.campaign, args.out, args.tracker)
    elif args.command == "compare":
        compare(args.baseline, args.candidate)
    else:
        bench(args.campaign, args.out)


if __name__ == "__main__":
    main()
