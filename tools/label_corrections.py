"""Phase 7A.3 P7: initialization-frame speed corrections to the forward-risk labels, and their impact (post hoc).

    PYTHONPATH=src python3 tools/label_corrections.py CAMPAIGN OUT_DIR [--model docs/evidence/phase7a2/model.json]

For every supervised row of CAMPAIGN/features-final, the original roadsense.forward-risk.v1 label is recomputed (it
must reproduce the stored one exactly) next to the corrected label (scenario_dataset.correct_initialization).
Writes labels.jsonl (both labels per row; the originals are never replaced) and label-corrections.json: the fixes,
the labels and incidents they change, and the frozen Phase 7A.2 model's metrics under both label sets. The frozen
model and its failed acceptance result are unchanged: this is an observation for a future protocol, not a re-score.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forward_risk_metrics as metrics  # noqa: E402
import scenario_dataset as ds  # noqa: E402
from roadsense.expanded_risk import Predictor  # noqa: E402

SPLITS = ("train", "validation", "test", "stress", "ordinary")


def run_truth(path):
    header, frames, _, end = ds.sc.read_ground_truth(path)
    corrected, fixes = ds.correct_initialization(frames)
    result = {}
    for name, fr in (("original", frames), ("corrected", corrected)):
        valid = [f for f in fr if not f.get("missing_physical")]
        outcomes = [ds.outcomes(f) for f in valid]
        result[name] = ({ds.tick(o["t"]): o for o in outcomes}, metrics.incidents(outcomes))
    return result, fixes


def short(report):
    keys = ("samples", "positive_samples", "pre_event_eligible_incidents", "pre_event_detected_incidents",
            "pre_event_detection_rate", "median_warning_lead_s", "false_alarms_per_hour", "recall", "precision")
    out = {k: report[k] for k in keys}
    out["early_frame_recall"] = report["before_developing"]["recall"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("campaign", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--model", type=Path, default=Path("docs/evidence/phase7a2/model.json"))
    args = ap.parse_args()
    if args.out.exists():
        sys.exit("output exists: %s (never overwritten)" % args.out)
    predictor = Predictor.load(args.model)
    rows = defaultdict(list)
    for line in open(args.campaign / "features-final/samples.jsonl"):
        r = json.loads(line)
        rows[r["run_id"]].append(r)
    args.out.mkdir(parents=True)
    fixes_by = Counter()
    changed = Counter()
    incidents = {name: defaultdict(list) for name in ("original", "corrected")}
    labelled = {name: [] for name in ("original", "corrected")}
    all_fixes, reproduced = [], True
    with open(args.out / "labels.jsonl", "w") as out:
        for run_id in sorted(rows):
            truth, fixes = run_truth(args.campaign / "runs" / run_id / "ground_truth.jsonl")
            split = rows[run_id][0]["split"]
            for f in fixes:
                fixes_by[(split, "staged" if f["vehicle"].startswith("rs_") else "traffic")] += 1
                all_fixes.append(dict(f, run_id=run_id, split=split))
            for name in incidents:
                incidents[name][run_id] = truth[name][1]
            for r in sorted(rows[run_id], key=lambda r: r["sim_s"]):
                t = ds.tick(r["sim_s"])
                original = ds.future_label(truth["original"][0], t)
                corrected = ds.future_label(truth["corrected"][0], t)
                reproduced &= original == r["labels"]
                a, b = r["labels"]["forward_risk_3s"], corrected["forward_risk_3s"]
                changed[(split, "%d->%d" % (a, b))] += 1
                out.write(json.dumps({"run_id": run_id, "sequence": r["sequence"], "sim_s": r["sim_s"], "split": split,
                                      "original": r["labels"], "corrected": corrected,
                                      "corrected_schema": ds.CORRECTED_SCHEMA}, separators=(",", ":")) + "\n")
                labelled["original"].append(r)
                cur = truth["corrected"][0].get(t, {}).get("forward_near_miss", r["current_forward_risk"])
                labelled["corrected"].append(dict(r, labels=dict(r["labels"], forward_risk_3s=b), current_forward_risk=cur))
    evaluation = {}
    for name, items in labelled.items():
        scores = {}
        for split in ("test", "stress", "ordinary"):
            subset = [r for r in items if r["split"] == split]
            warnings = [bool(predictor.predict(r["features"], r["feature_schema"])["warning"]) for r in subset]
            scores[split] = short(metrics.evaluate(subset, warnings, incidents[name]))
        evaluation[name] = scores
    per_split = {}
    for split in SPLITS:
        runs = [rid for rid in rows if rows[rid][0]["split"] == split]
        per_split[split] = {
            "fixes_staged": fixes_by[(split, "staged")], "fixes_traffic": fixes_by[(split, "traffic")],
            "labels_unchanged_pos": changed[(split, "1->1")], "labels_unchanged_neg": changed[(split, "0->0")],
            "labels_1_to_0": changed[(split, "1->0")], "labels_0_to_1": changed[(split, "0->1")],
            "incidents_original": sum(len(incidents["original"][r]) for r in runs),
            "incidents_corrected": sum(len(incidents["corrected"][r]) for r in runs)}
    report = {"schema": "roadsense.label-corrections.v1", "original_labels_reproduced": reproduced,
              "rule": ds.correct_initialization.__doc__.split("\n\n")[1].strip() if "\n\n" in ds.correct_initialization.__doc__ else None,
              "thresholds_mps": {"still": ds.STILL_MPS, "moving": ds.MOVING_MPS, "agree": ds.AGREE_MPS},
              "per_split": per_split, "fixes": all_fixes,
              "frozen_model_post_hoc": {"model_sha256_note": "docs/evidence/phase7a2/model.json, unchanged",
                                        "original_labels": evaluation["original"],
                                        "corrected_labels": evaluation["corrected"]}}
    (args.out / "label-corrections.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"reproduced": reproduced, "per_split": per_split}, indent=1))


if __name__ == "__main__":
    main()
