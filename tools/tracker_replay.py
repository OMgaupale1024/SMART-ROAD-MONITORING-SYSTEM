"""Phase 7A.3: verify raw sensor-frame recordings and replay them, offline, through a chosen tracker.

    PYTHONPATH=src venv/bin/python tools/tracker_replay.py verify  CAMPAIGN OUT.json
    PYTHONPATH=src venv/bin/python tools/tracker_replay.py replay  CAMPAIGN OUT_DIR [--tracker legacy|p1|p1p2|p1p2p4|legacyp4]
    PYTHONPATH=src venv/bin/python tools/tracker_replay.py compare BASELINE_AUDIT.json CANDIDATE_AUDIT.json [...]
    PYTHONPATH=src venv/bin/python tools/tracker_replay.py diff    CAMPAIGN BEFORE_REPLAY AFTER_REPLAY OUT.json
    PYTHONPATH=src venv/bin/python tools/tracker_replay.py confirm CAMPAIGN OUT.json NAME=REPLAY_DIR [...]
    python3 tools/tracker_replay.py bench CAMPAIGN_OR_BUNDLE OUT.json   # standard library only, for the Pi

verify: per run, the capture's checksum against the campaign log, contiguous sequences, 200 ms timestamps, the frames
the server accepted, the ground-truth join, and a deterministic replay: the frames the server processed, replayed
through the server's own code path (edge.EdgeRun), must give exactly the slot features the server recorded live.
replay: every captured frame through edge.EdgeRun with the tracker swapped in; writes slots (for
perception_audit.py --slots), safety decisions and the tracks. Offline only: the deployed service is never touched.
compare: the §5 criteria of each audit, the first being the baseline. diff: the safety decisions two replays disagree
on, each set against simulator truth. confirm: track-level confirmation latency by true speed. Truth only evaluates;
no tracker ever sees it.
"""
import argparse
import gzip
import hashlib
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
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
    if name == "p1p2p4":
        return tracking.Tracker(initiation="range_rate", estimator="kalman", lifecycle="p4")
    if name == "legacyp4":  # the ablation: P4's lifecycle on the legacy tracker
        return tracking.Tracker(lifecycle="p4")
    raise ValueError("unknown tracker %r" % name)


TRACKERS = ("legacy", "p1", "p1p2", "p1p2p4", "legacyp4")
TRUTH_PAIR_M = 3.0  # a detection this close to a vehicle's true centre is a detection of that vehicle (evaluation)


class TruthOracle:
    """EVALUATION ONLY, never a tracker and never in the pipeline: reports each vehicle the radars detected (a
    detection within TRUTH_PAIR_M of its true centre) at its true centre with its true velocity, aged from its first
    detection, for as long as the trackers may coast an unseen track (as long as it was seen, at most MAX_COAST_S):
    a vehicle the radars stop seeing at close range stays. The safety decisions it produces are what the unchanged
    deterministic rules decide with perfect perception of what the radars saw; trackers are judged against them."""

    def __init__(self, truth):
        self.truth, self.seen, self.confirmed = truth, {}, []

    def update(self, t, detections, pose=None, ego_velocity=None):
        from types import SimpleNamespace
        from roadsense.autonomy.tracking import CONFIRM_S, MAX_COAST_S
        self.confirmed = []
        for v in self.truth.get(round(t, 1), (None, []))[1]:
            (cx, cy), entry = v["centre"], self.seen.get(v["id"])
            near = [d for d in detections if math.hypot(d.world_x - cx, d.world_y - cy) <= TRUTH_PAIR_M]
            def kept(e):
                return e is not None and t - e[1] <= min(MAX_COAST_S, max(CONFIRM_S, e[1] - e[0])) + 1e-6
            if near:
                entry = self.seen[v["id"]] = (entry[0] if kept(entry) else t, t, near[0].source)
            if not kept(entry):
                continue
            self.confirmed.append(SimpleNamespace(
                track_id="TRUTH_%s" % v["id"], position_at=lambda _t, c=(cx, cy): c, velocity=tuple(v["v"]),
                first_seen=entry[0], last_seen=entry[1], source=entry[2], range_rate=None))


def runs(campaign):
    for line in open(campaign / "campaign.jsonl"):
        r = json.loads(line)
        if r.get("ok") and r.get("run_id"):
            yield r


def read_frames(path):
    return [line.rstrip(b"\n") for line in open(path, "rb")]


def replay_run(lines, tracker=None, only=None, truth=None):
    """[(sequence, state)] for the captured frames (only those sequences, if given), as the server computes them.
    tracker "oracle" is the evaluation-only TruthOracle on the run's truth."""
    from roadsense.web import edge
    run, out = None, []
    for line in lines:
        frame = edge.SensorFrame.model_validate_json(line)
        if only is not None and frame.simulation.sequence not in only:
            continue
        if run is None:
            run = edge.EdgeRun(frame.ego.vehicle_id)
            if tracker == "oracle":
                run.pipeline.tracker = TruthOracle(truth)
            elif tracker is not None:
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
            truth = None
            if tracker == "oracle":
                import perception_audit as pa
                truth = pa.truth_frames(campaign / "runs" / r["run_id"] / "ground_truth.jsonl")
            for seq, state in replay_run(lines, tracker=tracker, truth=truth):
                t = state["timestamp"]
                key = {"run_id": r["run_id"], "sequence": seq, "sim_s": t}
                slots.write(json.dumps(dict(key, features=history.features(state, None, 0, None)),
                                       separators=(",", ":")) + "\n")
                decisions.write(json.dumps(dict(key, decision=history.decision_values(state)),
                                           separators=(",", ":")) + "\n")
                tracks.write(json.dumps(dict(key, tracks=state.get("tracks")), separators=(",", ":")) + "\n")
    (out_dir / "replay.json").write_text(json.dumps({"tracker": tracker, "campaign": campaign.name}, indent=1) + "\n")
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


def compare(paths):
    """The §5 criteria of each audit, the first one the baseline (T10's reference)."""
    audits = [json.load(open(p))["criteria"] for p in paths]
    base, rows = audits[0], []
    for name, text, key, limit in CRITERIA:
        rows.append({"criterion": name, "what": text, "limit": limit, "values": [c[key] for c in audits],
                     "pass": [c[key] is not None and c[key] <= limit for c in audits]})
    for key, name in (("lon_mae", "T10 lon MAE"), ("lat_mae", "T10 lat MAE")):
        rows.append({"criterion": name, "what": "no worse than the baseline + 10%", "limit": round(base[key] * 1.1, 3),
                     "values": [c[key] for c in audits], "pass": [c[key] <= base[key] * 1.1 for c in audits]})
    print(json.dumps(rows, indent=1))
    return rows


def truth_outcomes(run_dir):
    """{tick: forward outcome} of a run from simulator truth, initialization speeds corrected (P7)."""
    import scenario_dataset as ds
    _, frames, _, _ = ds.sc.read_ground_truth(run_dir / "ground_truth.jsonl")
    corrected, _ = ds.correct_initialization(frames)
    return {ds.tick(f["t"]): ds.outcomes(f) for f in corrected if not f.get("missing_physical")}


def diff(campaign, before, after, out, oracle=None):
    """Safety decisions that differ between two replays of the same frames, each set against simulator truth: does a
    forward near miss follow within 3 s (scenario_dataset's label, initialization speeds corrected), is the frame at
    or after the run's first collision proxy, and which true vehicle is each reported threat, with its true relative
    lateral speed; with an oracle replay, the decision the rules make on the truth of what the radars saw, and which
    side of the change agrees with it. Evaluation only: no tracker sees the truth."""
    import perception_audit as pa
    import scenario_dataset as ds
    from roadsense.autonomy import perception
    from roadsense.autonomy.risk import LEVELS
    a, b = ({(r["run_id"], r["sequence"]): r for r in map(json.loads, open(d / "decisions.jsonl"))}
            for d in (before, after))
    if set(a) != set(b):
        raise ValueError("the two replays cover different frames")

    def verdict(r):
        return "%s/%s" % (r["decision"]["risk"], r["decision"]["action"])
    profiles = {r["run_id"]: r["profile"] for r in runs(campaign)}
    reference = {(r["run_id"], r["sequence"]): r for r in map(json.loads, open(oracle / "decisions.jsonl"))} \
        if oracle else {}
    cases, truth = [], {}
    for run_id, seq in sorted(k for k in a if verdict(a[k]) != verdict(b[k])):
        run_dir = campaign / "runs" / run_id
        if run_id not in truth:
            outcomes = truth_outcomes(run_dir)
            first = min((k for k, o in outcomes.items() if o["collision_proxy"]), default=None)
            truth[run_id] = outcomes, first, pa.truth_frames(run_dir / "ground_truth.jsonl")
        outcomes, first, frames = truth[run_id]
        perception.use_road(profiles[run_id])
        x, y = a[(run_id, seq)], b[(run_id, seq)]
        tick = ds.tick(x["sim_s"])
        label = ds.future_label(outcomes, tick)
        ego, vehicles = frames.get(round(x["sim_s"], 1), (None, []))
        rel = pa.relative(ego, vehicles) if ego else {}

        def threat(d):
            if d["threat_lon_m"] is None:
                return None
            vid = pa.match(rel, d["threat_lon_m"], d["threat_lat_m"])
            return {"lon_m": d["threat_lon_m"], "lat_m": d["threat_lat_m"], "rel_lat_mps": d["threat_rel_lat_mps"],
                    "ttc_s": d["threat_ttc_s"], "truth_vehicle": vid, "truth_type": rel[vid]["type"] if vid else None,
                    "truth_rel_lat_mps": rel[vid]["vlat"] if vid else None}
        up = LEVELS.index(y["decision"]["risk"]) - LEVELS.index(x["decision"]["risk"])
        cases.append({"run_id": run_id, "sequence": seq, "sim_s": x["sim_s"], "before": verdict(x), "after": verdict(y),
                      "direction": "raised" if up > 0 else "lowered" if up < 0 else "action only",
                      "forward_near_miss_within_3s": None if label is None else bool(label["forward_risk_3s"]),
                      "at_or_after_first_collision_proxy": first is not None and tick >= first,
                      "before_threat": threat(x["decision"]), "after_threat": threat(y["decision"])})
        if reference:
            o = reference[(run_id, seq)]
            cases[-1].update(oracle=verdict(o), agrees_with_oracle=(
                "before" if verdict(o) == verdict(x) else "after" if verdict(o) == verdict(y) else
                "after closer" if abs(LEVELS.index(o["decision"]["risk"]) - LEVELS.index(y["decision"]["risk"])) <
                abs(LEVELS.index(o["decision"]["risk"]) - LEVELS.index(x["decision"]["risk"])) else
                "before closer" if abs(LEVELS.index(o["decision"]["risk"]) - LEVELS.index(y["decision"]["risk"])) >
                abs(LEVELS.index(o["decision"]["risk"]) - LEVELS.index(x["decision"]["risk"])) else "neither"))
    count = Counter("%s; forward near miss within 3 s: %s" % (c["direction"], c["forward_near_miss_within_3s"])
                    for c in cases)
    if reference:
        count.update("%s; oracle agrees with: %s" % (c["direction"], c["agrees_with_oracle"]) for c in cases)
    result = {"schema": "roadsense.decision-diff.v2", "before": json.load(open(before / "replay.json"))["tracker"],
              "after": json.load(open(after / "replay.json"))["tracker"], "frames": len(a), "changed": len(cases),
              "summary": dict(sorted(count.items())), "truth": "evaluation only", "cases": cases}
    out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: result[k] for k in ("before", "after", "frames", "changed", "summary")}))


def judge(oracle, replays, out):
    """Each replay's safety decisions against the oracle's (TruthOracle: the same rules on the truth of what the
    radars saw): frames agreeing, frames rated below it (under-warning) and above it (over-warning), by its level."""
    from roadsense.autonomy.risk import LEVELS
    reference = {(r["run_id"], r["sequence"]): r["decision"] for r in map(json.loads, open(oracle / "decisions.jsonl"))}
    result = {"schema": "roadsense.decision-judgement.v1", "reference": "oracle (evaluation only)",
              "frames": len(reference), "oracle_levels": dict(Counter(d["risk"] for d in reference.values()))}
    for name, d in replays.items():
        counts = Counter()
        for r in map(json.loads, open(d / "decisions.jsonl")):
            o, x = reference[(r["run_id"], r["sequence"])], r["decision"]
            gap = LEVELS.index(x["risk"]) - LEVELS.index(o["risk"])
            how = "agree" if gap == 0 else "under" if gap < 0 else "over"
            counts[how] += 1
            counts["%s when oracle %s" % (how, o["risk"])] += 1
            if how != "agree" and "SAFE" in (x["risk"], o["risk"]):
                counts["%s: warning %s" % (how, "missing" if gap < 0 else "without oracle warning")] += 1
        result[name] = dict(sorted(counts.items()))
    out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


SPEED_BANDS = (0, 5, 10, 15, 20, 25, 30)
EPISODE_GAP_S = 1.0  # a vehicle undetected for longer starts a new detection episode
CONFIRMABLE_S = 1.0  # episodes detected over at least this span are long enough to confirm in


def confirm(campaign, out, replays):
    """Track-level confirmation per tracker. A detection episode is a vehicle detected by a radar (a detection within
    3 m of its true centre) until it goes undetected for more than EPISODE_GAP_S; its latency is the time from the
    first detection to the first frame in which a confirmed track the audit matches to the vehicle is reported, by
    the vehicle's true world speed at the first detection. Truth selects and labels episodes only."""
    import perception_audit as pa
    from roadsense.autonomy import perception
    reported = {}
    for name, d in replays.items():
        for r in map(json.loads, gzip.open(d / "tracks.jsonl.gz", "rt")):
            reported[(name, r["run_id"], round(r["sim_s"], 1))] = r["tracks"] or []
    episodes = []
    for r in runs(campaign):
        perception.use_road(r["profile"])
        run_dir = campaign / "runs" / r["run_id"]
        truth = pa.truth_frames(run_dir / "ground_truth.jsonl")
        current = {}
        for f in map(json.loads, open(run_dir / "sensor_frames.jsonl")):
            t, ego = round(f["timestamp"], 1), f["ego"]
            if t not in truth:
                continue
            pose = (ego["position"][0], ego["position"][1], ego["yaw_rad"])
            true_ego, vehicles = truth[t]
            found = [(d.world_x, d.world_y) for radar, data in f["radars"].items()
                     for d in perception.radar_detections(radar, data["targets"], data["limits"], t, pose)]
            for v in vehicles:
                cx, cy = v["centre"]
                if any(math.hypot(x - cx, y - cy) <= 3.0 for x, y in found):
                    e = current.get(v["id"])
                    if e is None or t - e["last_detected"] > EPISODE_GAP_S + 1e-6:
                        e = current[v["id"]] = {
                            "run_id": r["run_id"], "vehicle": v["id"], "type": v.get("type"), "start": t,
                            "last_detected": t, "speed_mps": round(math.hypot(*v["v"]), 2),
                            "range_m": round(math.hypot(cx - true_ego["x"], cy - true_ego["y"]), 1), "latency_s": {}}
                        episodes.append(e)
                    e["last_detected"] = t
            rel = None
            for v in vehicles:
                e = current.get(v["id"])
                if e is None or t - e["last_detected"] > EPISODE_GAP_S + 1e-6 or len(e["latency_s"]) == len(replays):
                    continue
                for name in replays:
                    tracks = reported.get((name, r["run_id"], t))
                    if name in e["latency_s"] or not tracks:
                        continue
                    rel = rel or pa.relative(true_ego, vehicles)
                    if any(pa.match(rel, k["relative_position"]["longitudinal_m"],
                                    k["relative_position"]["lateral_m"]) == v["id"] for k in tracks):
                        e["latency_s"][name] = round(t - e["start"], 1)

    def band(speed):
        low = max(b for b in SPEED_BANDS if b <= speed)
        return "%02d-%02d m/s" % (low, low + 5) if low < SPEED_BANDS[-1] else "%02d+ m/s" % low
    table = defaultdict(lambda: defaultdict(list))
    for e in episodes:
        if e["last_detected"] - e["start"] >= CONFIRMABLE_S - 1e-6:
            for name in replays:
                table[band(e["speed_mps"])][name].append(e["latency_s"].get(name))
    summary = {}
    for b, by in sorted(table.items()):
        summary[b] = {"episodes": len(next(iter(by.values()))),
                      "independent_runs": len({e["run_id"] for e in episodes
                                               if band(e["speed_mps"]) == b and e["last_detected"] - e["start"] >= CONFIRMABLE_S - 1e-6})}
        for name, values in by.items():
            done = sorted(v for v in values if v is not None)
            summary[b][name] = {"within_0_6_s": round(sum(v <= 0.6 + 1e-9 for v in done) / len(values), 4),
                                "within_1_0_s": round(sum(v <= 1.0 + 1e-9 for v in done) / len(values), 4),
                                "never": sum(v is None for v in values),
                                "p50_s": done[len(done) // 2] if done else None, "max_s": done[-1] if done else None}
    fast = [e for e in episodes if e["speed_mps"] >= 25 and e["last_detected"] - e["start"] >= CONFIRMABLE_S - 1e-6]
    result = {"schema": "roadsense.track-confirmation.v1", "trackers": list(replays), "episode_gap_s": EPISODE_GAP_S,
              "confirmable_span_s": CONFIRMABLE_S, "by_true_speed": summary, "episodes_at_or_above_25_mps": fast,
              "short_episodes_excluded": sum(e["last_detected"] - e["start"] < CONFIRMABLE_S - 1e-6 for e in episodes),
              "truth": "evaluation only"}
    out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


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
    for name, extra in (("verify", "out"), ("replay", "out"), ("compare", None), ("bench", "out"), ("diff", None),
                        ("confirm", "out"), ("judge", None)):
        p = sub.add_parser(name)
        if name == "compare":
            p.add_argument("audits", type=Path, nargs="+")
            continue
        if name == "diff":
            for arg in ("campaign", "before", "after", "out"):
                p.add_argument(arg, type=Path)
            p.add_argument("--oracle", type=Path, help="an oracle replay to judge each change against")
            continue
        if name == "judge":
            p.add_argument("oracle", type=Path)
            p.add_argument("out", type=Path)
            p.add_argument("replays", nargs="+", help="NAME=REPLAY_DIR")
            continue
        if name == "confirm":
            p.add_argument("campaign", type=Path)
            p.add_argument("out", type=Path)
            p.add_argument("replays", nargs="+", help="NAME=REPLAY_DIR")
            continue
        p.add_argument("campaign", type=Path)
        p.add_argument(extra, type=Path)
        if name == "replay":
            p.add_argument("--tracker", choices=TRACKERS + ("oracle",), default="legacy",
                           help="oracle: the evaluation-only TruthOracle, a reference, never a tracker")
    args = ap.parse_args()
    if getattr(args, "out", None) is not None and args.out.exists():
        sys.exit("output exists: %s (never overwritten)" % args.out)
    if args.command == "verify":
        verify(args.campaign, args.out)
    elif args.command == "replay":
        replay(args.campaign, args.out, args.tracker)
    elif args.command == "compare":
        compare(args.audits)
    elif args.command == "diff":
        diff(args.campaign, args.before, args.after, args.out, args.oracle)
    elif args.command == "judge":
        judge(args.oracle, {k: Path(v) for k, v in (x.split("=", 1) for x in args.replays)}, args.out)
    elif args.command == "confirm":
        confirm(args.campaign, args.out, {k: Path(v) for k, v in (x.split("=", 1) for x in args.replays)})
    else:
        bench(args.campaign, args.out)


if __name__ == "__main__":
    main()
