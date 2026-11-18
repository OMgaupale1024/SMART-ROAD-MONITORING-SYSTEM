"""EXPERIMENTAL, NOT SAFETY-AUTHORITATIVE: offline demonstration of the frozen Phase 7A.2 boosted model.

The model failed its predictive acceptance (docs/evidence/phase7a2/RESULTS.md). This replays *recorded* sensor
feature frames through the standard-library causal features and the frozen JSON model, to measure latency, memory,
CPU and numerical parity on a target such as the Raspberry Pi. It is not connected to the RoadSense server, the
safety pipeline, the dashboard or any vehicle, and its warnings are never acted on.

    # on the Mac: a self-contained bundle (model, the two modules it needs, recorded frames, Mac reference outputs)
    PYTHONPATH=src python3 tools/pi_offline_demo.py prepare CAMPAIGN BUNDLE_DIR [--splits test,stress,ordinary]
    # on the target, from inside the copied bundle, no RoadSense installation needed or touched:
    nice -n 10 python3 -s pi_offline_demo.py bench . REPORT.json
"""
import argparse
import gzip
import hashlib
import json
import math
import os
import platform
import resource
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

LABEL = "EXPERIMENTAL offline demonstration; failed predictive acceptance; NOT safety-authoritative"
MODEL_SHA256 = "fc3235f96e86a0e85f01ca9e3f2c9d0dfb5a44c8f329f98bb547fe4a7793bce4"  # roadsense-7a2-hgb15-v1, frozen
ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(campaign, bundle, splits):
    if bundle.exists():
        sys.exit("bundle directory exists: %s (never overwritten)" % bundle)
    from roadsense.expanded_risk import Predictor, TemporalFeatures
    from roadsense.learned_risk import FEATURES as BASE
    model = ROOT / "docs/evidence/phase7a2/model.json"
    if sha256(model) != MODEL_SHA256:
        sys.exit("model.json is not the frozen artifact")
    runs = {}
    for line in open(campaign / "features-final/samples.jsonl"):
        r = json.loads(line)
        if r["split"] in splits:
            runs[r["run_id"]] = r["split"]
    predictor, stream = Predictor.load(model), TemporalFeatures()
    (bundle / "roadsense").mkdir(parents=True)
    for name in ("__init__.py", "learned_risk.py", "expanded_risk.py"):
        if name == "__init__.py":
            (bundle / "roadsense" / name).write_text('"""Bundled copy for the offline demonstration only."""\n')
        else:
            shutil.copy2(ROOT / "src/roadsense" / name, bundle / "roadsense" / name)
    shutil.copy2(model, bundle / "model.json")
    shutil.copy2(Path(__file__), bundle / "pi_offline_demo.py")
    db = sqlite3.connect("file:%s?mode=ro" % (campaign / "history/roadsense-history.sqlite3"), uri=True)
    count = 0
    with gzip.open(bundle / "frames.jsonl.gz", "wt") as out:
        for run_id, seq, sim_s, encoded in db.execute(
                "SELECT g.run_id, s.sequence, s.sim_s, s.features FROM samples s JOIN segments g USING (segment_id) "
                "ORDER BY g.run_id, s.sim_s, s.sequence"):
            if run_id not in runs:
                continue
            base = {n: json.loads(encoded)[n] for n in BASE}
            current = stream.observe(base, sim_s, run_id, seq)
            result = predictor.predict({n: current[n] for n in predictor.names}, predictor.input_schema)
            out.write(json.dumps({"run_id": run_id, "split": runs[run_id], "sequence": seq, "sim_s": sim_s,
                                  "base": base, "reference": {"features": {n: current[n] for n in predictor.names},
                                                              "status": result["status"], "score": result["risk_score"],
                                                              "warning": result["warning"]}},
                                 separators=(",", ":")) + "\n")
            count += 1
    db.close()
    manifest = {"label": LABEL, "model_version": predictor.version, "model_sha256": MODEL_SHA256,
                "threshold": predictor.threshold, "frames": count, "runs": len(runs), "splits": sorted(splits),
                "reference_platform": platform.platform(), "reference_python": platform.python_version(),
                "source": "recorded roadsense.features.v1 frames of the Phase 7A.2 campaign; Mac reference outputs "
                          "from the same standard-library code"}
    (bundle / "MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (bundle / "README.txt").write_text(LABEL + "\n\n" + __doc__)
    files = sorted(p for p in bundle.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (bundle / "SHA256SUMS").write_text("".join("%s  %s\n" % (sha256(p), p.relative_to(bundle)) for p in files))
    print("%s: %d frames from %d runs" % (bundle, count, len(runs)))


def rss_kib():
    """Current and peak resident set size (KiB) from /proc on Linux; peak only elsewhere."""
    try:
        status = dict(line.split(":", 1) for line in open("/proc/self/status"))
        return int(status["VmRSS"].split()[0]), int(status["VmHWM"].split()[0])
    except OSError:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return None, peak // 1024 if sys.platform == "darwin" else peak


def pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, round((len(values) - 1) * q))], 3)


def bench(bundle, report_path):
    if report_path.exists():
        sys.exit("report exists: %s (never overwritten)" % report_path)
    for line in open(bundle / "SHA256SUMS"):
        digest, name = line.split(None, 1)
        if sha256(bundle / name.strip()) != digest:
            sys.exit("checksum mismatch: %s" % name.strip())
    sys.modules.update(numpy=None, scipy=None, sklearn=None, joblib=None)  # standard library only
    sys.path.insert(0, str(bundle))  # the bundled modules, never an installed RoadSense
    rss_start, _ = rss_kib()
    wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
    from roadsense.expanded_risk import Predictor, TemporalFeatures
    predictor = Predictor.load(bundle / "model.json")
    load_us = (time.perf_counter_ns() - wall0) / 1000
    load_cpu_us = (time.process_time_ns() - cpu0) / 1000
    rss_loaded, peak_loaded = rss_kib()
    if predictor.version is None or sha256(bundle / "model.json") != MODEL_SHA256:
        sys.exit("not the frozen model")
    frames = [json.loads(line) for line in gzip.open(bundle / "frames.jsonl.gz", "rt")]
    stream = TemporalFeatures()
    feature_us, model_us, total_us = [], [], []
    score_err = feature_err = 0.0
    status_mismatch = warning_mismatch = feature_mismatch = 0
    warnings = 0
    wall1, cpu1 = time.perf_counter_ns(), time.process_time_ns()
    for fr in frames:
        a = time.perf_counter_ns()
        current = stream.observe(fr["base"], fr["sim_s"], fr["run_id"], fr["sequence"])
        selected = {n: current[n] for n in predictor.names}
        b = time.perf_counter_ns()
        result = predictor.predict(selected, predictor.input_schema)
        c = time.perf_counter_ns()
        feature_us.append((b - a) / 1000)
        model_us.append((c - b) / 1000)
        total_us.append((c - a) / 1000)
        ref = fr["reference"]
        for n, v in selected.items():
            r = ref["features"][n]
            if (v is None) != (r is None):
                feature_mismatch += 1
            elif v is not None:
                feature_err = max(feature_err, abs(v - r))
        if result["status"] != ref["status"]:
            status_mismatch += 1
        elif result["risk_score"] is not None:
            score_err = max(score_err, abs(result["risk_score"] - ref["score"]))
            warning_mismatch += result["warning"] != ref["warning"]
            warnings += bool(result["warning"])
    wall_s = (time.perf_counter_ns() - wall1) / 1e9
    cpu_s = (time.process_time_ns() - cpu1) / 1e9
    _, peak_end = rss_kib()
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    manifest = json.load(open(bundle / "MANIFEST.json"))
    report = {
        "label": LABEL, "experimental": True, "safety_authoritative": False, "connected_to_live_pipeline": False,
        "model_version": predictor.version, "model_sha256": MODEL_SHA256, "threshold": predictor.threshold,
        "platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
        "cpu_count": os.cpu_count(), "load_average_at_end": load, "niceness": os.nice(0),
        "frames": len(frames), "runs": manifest["runs"],
        "model_load": {"wall_us": round(load_us, 1), "cpu_us": round(load_cpu_us, 1),
                       "rss_before_kib": rss_start, "rss_after_kib": rss_loaded,
                       "rss_increase_kib": None if rss_start is None else rss_loaded - rss_start,
                       "note": "includes importing the two bundled modules"},
        "latency_us_batch1": {name: {"p50": pct(v, .5), "p95": pct(v, .95), "p99": pct(v, .99), "max": round(max(v), 3),
                                     "mean": round(statistics.fmean(v), 3)}
                              for name, v in (("features", feature_us), ("model", model_us), ("features_plus_model", total_us))},
        "cpu": {"replay_wall_s": round(wall_s, 3), "replay_cpu_s": round(cpu_s, 3),
                "one_core_utilisation_during_replay": round(cpu_s / wall_s, 3),
                "cpu_fraction_of_one_core_at_5hz": round(5 * statistics.fmean(total_us) / 1e6, 6),
                "note": "replay runs back to back; at the live 5 Hz rate the cost is the last figure"},
        "memory": {"peak_rss_kib_after_load": peak_loaded, "peak_rss_kib_end": peak_end,
                   "note": "the end figure includes all replay frames held in memory, not needed live"},
        "parity_vs_mac_reference": {"max_abs_score_error": score_err, "max_abs_feature_error": feature_err,
                                    "status_mismatches": status_mismatch, "feature_null_mismatches": feature_mismatch,
                                    "threshold_disagreements": warning_mismatch,
                                    "reference_platform": manifest["reference_platform"]},
        "warnings_issued_offline": warnings,
    }
    report_path.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(json.dumps(report, indent=1, sort_keys=True))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("campaign", type=Path)
    p.add_argument("bundle", type=Path)
    p.add_argument("--splits", default="test,stress,ordinary")
    b = sub.add_parser("bench")
    b.add_argument("bundle", type=Path)
    b.add_argument("report", type=Path)
    args = ap.parse_args()
    if args.command == "prepare":
        prepare(args.campaign, args.bundle, set(args.splits.split(",")))
    else:
        bench(args.bundle.resolve(), args.report)


if __name__ == "__main__":
    main()
