"""Bounded Phase 7A logistic experiment. `fit` freezes choices; `evaluate` never fits or tunes."""
import argparse
from collections import Counter
import hashlib
import json
import math
import platform
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
import warnings

import numpy as np
import scipy
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from roadsense.learned_risk import FEATURES, INPUT_SCHEMA, MODEL_SCHEMA, Predictor
import forward_risk_metrics as metrics
import scenario_dataset as dataset

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261117
DATA_SHA = "18d58ee83e05e11164876b68feb0cebacba9755eb3395d6f0a8097cb2eb652ed"
THRESHOLDS = [float(v) for v in np.geomspace(.001, .95, 41)] + [1.0]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def verify(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if tuple(manifest["features"]) != FEATURES or FEATURES != dataset.INPUTS or manifest["input_schema"] != INPUT_SCHEMA:
        raise ValueError("incompatible feature contract")
    if digest(directory / "samples.jsonl") != DATA_SHA or manifest["files"]["samples.jsonl"]["sha256"] != DATA_SHA:
        raise ValueError("unexpected dataset checksum")
    if manifest["schema"] != dataset.SCHEMA or manifest["horizon_s"] != 3:
        raise ValueError("unexpected target definition")
    return manifest


def load_rows(directory, splits):
    rows = []
    isolation = {}
    for line in (directory / "samples.jsonl").open():
        r = json.loads(line)
        for kind in ("run_id", "configuration_id", "group"):
            key = kind, r[kind]
            if isolation.setdefault(key, r["split"]) != r["split"]:
                raise ValueError("split leakage")
        if r["split"] not in splits:
            continue
        if set(r["features"]) != set(FEATURES) or r["labels"]["forward_risk_3s"] not in (0, 1):
            raise ValueError("malformed sample")
        rows.append(r)
    return sorted(rows, key=lambda r: (r["run_id"], r["sim_s"]))


def matrix(rows):
    x = np.asarray([[np.nan if r["features"][n] is None else r["features"][n] for n in FEATURES] for r in rows], dtype=np.float64)
    if np.any(np.isinf(x)):
        raise ValueError("infinite feature")
    return x, np.asarray([r["labels"]["forward_risk_3s"] for r in rows], dtype=np.int64)


def load_events(directory, rows):
    campaign = directory.parent
    checks = dict((name, h) for line in (directory / "SHA256SUMS").read_text().splitlines()
                  for h, name in [line.split("  ", 1)])
    events = {}
    for rid in sorted({r["run_id"] for r in rows}):
        if Path(rid).name != rid:
            raise ValueError("invalid run identity")
        path = campaign / "runs" / rid / "ground_truth.jsonl"
        if digest(path) != checks[str(path.relative_to(campaign))]:
            raise ValueError("truth checksum mismatch")
        header, frames, _, end = dataset.sc.read_ground_truth(path)
        if header["run_id"] != rid or header["schema"] != "roadsense.groundtruth.v2" or not end:
            raise ValueError("incompatible truth")
        outcomes = [dataset.outcomes(f) for f in frames if not f.get("missing_physical")]
        events[rid] = metrics.incidents(outcomes)
    return events


def ttc_scores(rows):
    scores = []
    for r in rows:
        f = r["features"]
        score = 0.0
        if f["lead_present"] and f["ego_speed_mps"] > 2 and f["lead_lon_m"] is not None:
            gap = max(0., f["lead_lon_m"] - 3.9)
            closing = -f["lead_v_lon_mps"] if f["lead_v_lon_mps"] is not None else 0
            if gap < 2:
                score = 1.0
            elif closing > .1:
                score = 1 / (1 + gap / closing)
        scores.append(score)
    return np.asarray(scores)


def score_report(rows, scores, threshold, events):
    y = np.asarray([r["labels"]["forward_risk_3s"] for r in rows])
    report = metrics.evaluate(rows, (scores >= threshold).tolist(), events)
    report.update(threshold=threshold, average_precision=float(average_precision_score(y, scores)) if np.any(y) else None,
                  roc_auc=float(roc_auc_score(y, scores)) if len(set(y)) == 2 else None,
                  brier_score=float(brier_score_loss(y, scores)), calibration=[])
    bins = np.minimum((scores * 10).astype(int), 9)
    for b in range(10):
        ix = bins == b
        report["calibration"].append({"lower": b / 10, "upper": (b + 1) / 10, "count": int(ix.sum()),
                                       "mean_score": float(scores[ix].mean()) if ix.any() else None,
                                       "observed_fraction": float(y[ix].mean()) if ix.any() else None})
    report["ece_10_bins"] = float(sum(v["count"] / len(rows) * abs(v["mean_score"] - v["observed_fraction"])
                                      for v in report["calibration"] if v["count"]))
    return report


def ranking(report, include_threshold=True):
    key = (report["f2"] or 0, -(report["false_alarms_per_hour"] or 0), report["pre_event_detection_rate"] or 0,
           report["median_warning_lead_s"] or 0)
    return key + (report["threshold"],) if include_threshold else key


def pipeline(weight):
    return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                         LogisticRegression(C=1., solver="lbfgs", class_weight=weight, tol=1e-6,
                                            max_iter=2000, random_state=SEED))


def artifact(model, variant, threshold, provenance):
    imputer, scaler, logistic = model.steps[0][1], model.steps[1][1], model.steps[2][1]
    return dict(provenance, schema=MODEL_SCHEMA, input_schema=INPUT_SCHEMA, features=list(FEATURES),
                target="forward_risk_3s", horizon_s=3.0, model_version="roadsense-forward-logistic-%s-v1" % variant,
                output="uncalibrated_risk_score", impute_median=imputer.statistics_.tolist(),
                scale_mean=scaler.mean_.tolist(), scale_scale=scaler.scale_.tolist(),
                coefficients=logistic.coef_[0].tolist(), intercept=float(logistic.intercept_[0]), threshold=threshold,
                iterations=int(logistic.n_iter_[0]))


def fit(directory, out):
    if out.exists() or ROOT == out.resolve() or ROOT in out.resolve().parents:
        raise ValueError("use a new output directory outside Git")
    manifest = verify(directory)
    rows = load_rows(directory, {"train"})
    x, y = matrix(rows)
    groups = np.asarray([r["group"] for r in rows])
    if len(set(groups[y == 1])) < 4:
        raise ValueError("insufficient positive groups for four-fold CV")
    folds = list(StratifiedGroupKFold(4, shuffle=True, random_state=SEED).split(x, y, groups))
    assert all(not set(groups[a]) & set(groups[b]) and len(set(y[a])) == len(set(y[b])) == 2 for a, b in folds)
    events = load_events(directory, rows)
    out.mkdir(parents=True)
    source_files = [Path(__file__), ROOT / "tools/forward_risk_metrics.py", ROOT / "src/roadsense/learned_risk.py",
                    ROOT / "requirements-ml.txt", ROOT / "docs/phase7a-plan.md"]
    provenance = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"])),
                  "source_sha256": {str(p.relative_to(ROOT)): digest(p) for p in source_files},
                  "dataset_sha256": DATA_SHA, "dataset_manifest_sha256": digest(directory / "manifest.json"),
                  "versions": {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__, "sklearn": sklearn.__version__},
                  "training": {"C": 1, "solver": "lbfgs", "tol": 1e-6, "max_iter": 2000, "folds": 4,
                               "seed": SEED, "preprocessing": "train-fold median then train-fold StandardScaler", "threads": 1,
                               "class_weight_balanced": {"0": len(y) / (2 * int((y == 0).sum())), "1": len(y) / (2 * int(y.sum()))},
                               "threshold_grid": THRESHOLDS, "selection": "training OOF F2; false alarms, pre-event recall and lead tie-breaks"}}
    for p in source_files:
        dest = out / "source" / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
    cv = {"folds": [{"train_groups": sorted(set(groups[a])), "validation_groups": sorted(set(groups[b])),
                     "train_positive": int(y[a].sum()), "validation_positive": int(y[b].sum())} for a, b in folds], "candidates": {}}
    start = time.perf_counter()
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        for variant, weight in (("unweighted", None), ("balanced", "balanced")):
            scores = np.full(len(y), np.nan)
            for a, b in folds:
                model = pipeline(weight).fit(x[a], y[a])
                scores[b] = model.predict_proba(x[b])[:, 1]
            assert np.isfinite(scores).all()
            choices = [score_report(rows, scores, t, events) for t in THRESHOLDS]
            chosen = max(choices, key=ranking)
            cv["candidates"][variant] = {"selected": chosen, "threshold_search": [
                {k: r[k] for k in ("threshold", "precision", "recall", "f2", "false_alarms_per_hour", "pre_event_detection_rate", "median_warning_lead_s")} for r in choices]}
            model = pipeline(weight).fit(x, y)
            a = artifact(model, variant, chosen["threshold"], provenance)
            portable = Predictor(a)
            portable_scores = np.asarray([portable.predict(r["features"])["risk_score"] for r in rows])
            error = float(np.max(np.abs(portable_scores - model.predict_proba(x)[:, 1])))
            if error > 1e-12:
                raise RuntimeError("portable inference mismatch")
            a["training_parity_max_abs_error"] = error
            write(out / (variant + ".json"), a)
    cv["baselines"] = {"always_negative": score_report(rows, np.zeros(len(rows)), 1., events),
                       "ttc_forward": score_report(rows, ttc_scores(rows), 1 / 5.5, events)}
    chosen_variant = max(cv["candidates"], key=lambda v: ranking(cv["candidates"][v]["selected"], False))
    write(out / "training-cv.json", cv)
    freeze = dict(provenance, schema="roadsense.training-freeze.v1", chosen_variant=chosen_variant,
                  candidate_sha256={v: digest(out / (v + ".json")) for v in cv["candidates"]},
                  training_cv_sha256=digest(out / "training-cv.json"),
                  thresholds={v: cv["candidates"][v]["selected"]["threshold"] for v in cv["candidates"]},
                  selection_splits=["train"], heldout_evaluated=False, fit_count=10,
                  fit_wall_s=round(time.perf_counter() - start, 3), peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    write(out / "freeze.json", freeze)
    print(json.dumps({"chosen_variant": chosen_variant, "thresholds": freeze["thresholds"], "freeze_sha256": digest(out / "freeze.json"), "fit_count": 10}))


def bootstrap(report):
    counts = []
    for g in report["group_counts"]:
        c = g["confusion"]
        counts.append([c[0][0], c[0][1], c[1][0], c[1][1], g["eligible_incidents"], g["detected_incidents"]])
    a = np.asarray(counts)
    rng = np.random.default_rng(SEED)
    sums = a[rng.integers(0, len(a), size=(1000, len(a)))].sum(axis=1)
    distributions = {"precision": (sums[:, 3], sums[:, 3] + sums[:, 1]), "recall": (sums[:, 3], sums[:, 3] + sums[:, 2]),
                     "false_positive_rate": (sums[:, 1], sums[:, 1] + sums[:, 0]),
                     "pre_event_detection_rate": (sums[:, 5], sums[:, 4])}
    return {k: {"interval_95": np.quantile(n[d > 0] / d[d > 0], [.025, .975]).tolist() if (d > 0).any() else None,
                "undefined_resamples": int((d == 0).sum()), "groups": len(a), "resamples": 1000}
            for k, (n, d) in distributions.items()}


def evaluate(directory, out):
    if (out / "evaluation.json").exists():
        raise FileExistsError("held-out evaluation already exists; no retuning or overwrite")
    verify(directory)
    freeze_path = out / "freeze.json"
    freeze_hash = digest(freeze_path)
    freeze = json.loads(freeze_path.read_text())
    if freeze["selection_splits"] != ["train"]:
        raise ValueError("invalid selection provenance")
    artifacts = {}
    for v, h in freeze["candidate_sha256"].items():
        if digest(out / (v + ".json")) != h:
            raise ValueError("model changed after freeze")
        artifacts[v] = json.loads((out / (v + ".json")).read_text())
    rows = load_rows(directory, {"train", "validation", "test", "stress"})
    events = load_events(directory, rows)
    predictors = {v: Predictor(a) for v, a in artifacts.items()}
    scores = {v: np.asarray([p.predict(r["features"])["risk_score"] for r in rows]) for v, p in predictors.items()}
    scores["always_negative"], scores["ttc_forward"] = np.zeros(len(rows)), ttc_scores(rows)
    thresholds = dict(freeze["thresholds"], always_negative=1., ttc_forward=1 / 5.5)
    results = {"schema": "roadsense.logistic-evaluation.v1", "freeze_sha256": freeze_hash,
               "chosen_variant": freeze["chosen_variant"], "splits": {}, "all_sample_parity": {}}
    x, _ = matrix(rows)
    for v, a in artifacts.items():
        # Independent vector reconstruction plus train-time sklearn parity cover all held-out rows.
        imputed = np.where(np.isnan(x), np.asarray(a["impute_median"]), x)
        z = ((imputed - a["scale_mean"]) / a["scale_scale"]) @ np.asarray(a["coefficients"]) + a["intercept"]
        from scipy.special import expit
        error = float(np.max(np.abs(expit(z) - scores[v])))
        assert error < 1e-12
        results["all_sample_parity"][v] = {"rows": len(rows), "max_abs_error": error}
    for split in ("train", "validation", "test", "stress"):
        indices = [i for i, r in enumerate(rows) if r["split"] == split]
        subset = [rows[i] for i in indices]
        results["splits"][split] = {"samples": len(subset), "groups": len({r["group"] for r in subset}),
            "positive_groups": len({r["group"] for r in subset if r["labels"]["forward_risk_3s"]}),
            "positive_configurations": len({r["configuration_id"] for r in subset if r["labels"]["forward_risk_3s"]}),
            "models": {}}
        for name, s in scores.items():
            report = score_report(subset, s[indices], thresholds[name], events)
            report["group_bootstrap"] = bootstrap(report)
            report["families"] = {}
            for family in sorted({r["family"] for r in subset}):
                ix = [i for i, r in zip(indices, subset) if r["family"] == family]
                family_rows = [rows[i] for i in ix]
                report["families"][family] = score_report(family_rows, s[ix], thresholds[name], events)
            if name in artifacts:
                report["default_0_5_diagnostic"] = score_report(subset, s[indices], .5, events)
            results["splits"][split]["models"][name] = report
    selected = predictors[freeze["chosen_variant"]]
    times = []
    for i in range(10000):
        r = rows[i % len(rows)]
        t = time.perf_counter_ns()
        answer = selected.predict(r["features"])
        times.append(time.perf_counter_ns() - t)
        assert answer["status"] == "ok"
    results["mac_inference"] = {"iterations": len(times), "python": platform.python_version(), "platform": platform.platform(),
        "p50_us": float(np.quantile(times, .5) / 1000), "p95_us": float(np.quantile(times, .95) / 1000),
        "p99_us": float(np.quantile(times, .99) / 1000), "includes": "30-feature validation, imputation, scaling, sigmoid and status; excludes I/O and feature extraction"}
    assert digest(freeze_path) == freeze_hash
    write(out / "evaluation.json", results)
    shutil.copyfile(out / (freeze["chosen_variant"] + ".json"), out / "model.json")
    files = [p for p in sorted(out.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"]
    (out / "SHA256SUMS").write_text("\n".join(digest(p) + "  " + str(p.relative_to(out)) for p in files) + "\n")
    print(json.dumps({"chosen_variant": freeze["chosen_variant"], "model_bytes": (out / "model.json").stat().st_size,
                      "mac_inference": results["mac_inference"], "test": {k: v["f2"] for k, v in results["splits"]["test"]["models"].items()}}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fit", "evaluate"))
    parser.add_argument("dataset", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        (fit if args.command == "fit" else evaluate)(args.dataset, args.out)


if __name__ == "__main__":
    main()
