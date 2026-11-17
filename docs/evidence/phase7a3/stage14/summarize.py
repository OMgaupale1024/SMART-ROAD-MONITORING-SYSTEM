"""Phase 7A.3 Stage 14: the A/B/C/D/legacy+P4 comparison tables from the Stage 12/14 outputs (truth evaluates only).

    PYTHONPATH=src:tools venv/bin/python docs/evidence/phase7a3/stage14/summarize.py OUT.json
"""
import collections
import gzip
import json
import sys
from pathlib import Path

import perception_audit as pa
import tracker_replay as tr
from roadsense.autonomy import perception

R = Path.home() / "Library/Application Support/RoadSense"
A = R / "analysis"
SETS = {"stage1": ("phase7a3-recording-20261117", A / "phase7a3-20261117"),
        "diagnostic": ("phase7a3-s9-diagnostic-20261117", A / "phase7a3-s9-20261117/diagnostic"),
        "evaluation": ("phase7a3-s9-evaluation-20261117", A / "phase7a3-s9-20261117/evaluation"),
        "stress": ("phase7a3-s9-stress-20261117", A / "phase7a3-s9-20261117/stress")}
# configuration: (name in the Stage 14 tables, failures directory, its file stem, audit directory)
CONFIGS = {"A": ("legacy", "s12", "legacy"), "B": ("p1", "s12", "p1"), "C": ("p1p2", "s12", "p1p2"),
           "D_v1": ("p1p2p4", "s14", "p1p2p4"), "LP4_v1": ("legacyp4", "s14", "legacyp4"),
           "D_v2a": ("p1p2p4", "s14b", "p1p2p4"), "LP4_v2a": ("legacyp4", "s14b", "legacyp4"),
           "D_v2b": ("p1p2p4", "s14c", "p1p2p4"), "LP4_v2b": ("legacyp4", "s14c", "legacyp4"),
           "D": ("p1p2p4", "s14d", "p1p2p4"), "LP4": ("legacyp4", "s14d", "legacyp4")}
DIRS = {"s12": A / "phase7a3-s12-20261117", "s14": A / "phase7a3-s14-20261118", "s14b": A / "phase7a3-s14b-20261118",
        "s14c": A / "phase7a3-s14c-20261118", "s14d": A / "phase7a3-s14d-20261118"}
SUPERSEDED = {"_v1": " (P4 v1 @ 283e6fb, superseded)", "_v2a": " (P4 v2a: outage judged on track counts, superseded)",
              "_v2b": " (P4 v2b: no lateral hold, superseded)"}


def audit(s, cfg):
    name, where, stem = CONFIGS[cfg]
    d = SETS[s][1] / ("audit-%s" % name) if where == "s12" else DIRS[where] / s / ("audit-%s" % name)
    return json.load(open(d / "perception-audit.json"))


def failures(s, cfg, oracle):
    name, where, stem = CONFIGS[cfg]
    return json.load(open(DIRS[where] / s / ("failures-%s%s.json" % (stem, oracle))))


def collisions(s):
    """{run: first collision-proxy time} from truth: frames after it are excluded from 'before any collision'."""
    campaign = R / "campaigns" / SETS[s][0]
    first = {}
    for r in tr.runs(campaign):
        out = tr.truth_outcomes(campaign / "runs" / r["run_id"])
        ticks = [k for k, o in out.items() if o["collision_proxy"]]
        if ticks:
            first[r["run_id"]] = min(ticks) * 0.2 if isinstance(min(ticks), int) else min(ticks)
    return first


LEVELS = ("SAFE", "CAUTION", "HIGH", "CRITICAL")


def decisions(d):
    out = collections.defaultdict(dict)
    for r in map(json.loads, open(d / "decisions.jsonl")):
        out[r["run_id"]][r["sequence"]] = (r["sim_s"], LEVELS.index(r["decision"]["risk"]))
    return out


def episodes(s, hit):
    """Every oracle episode (track_failures.analyse's definition: frames at or above a level, a warning counting from
    2 s before) where the configurations differ in warning at all, with each one's onset (s from the episode start)."""
    e = SETS[s][1]
    oracle = decisions(e / "replay-oracle")
    runs = {"A": decisions(e / "replay-legacy"), "C": decisions(e / "replay-p1p2")}
    runs.update({cfg: decisions(DIRS[CONFIGS[cfg][1]] / s / "trace-p1p2p4") for cfg in ("D_v1", "D_v2a", "D_v2b", "D")})
    out = []
    for run in sorted(oracle):
        seqs = sorted(oracle[run])
        for li in (1, 2, 3):
            found, start = [], None
            for i, q in enumerate(seqs):
                if oracle[run][q][1] >= li and start is None:
                    start = i
                if (oracle[run][q][1] < li or i == len(seqs) - 1) and start is not None:
                    found.append((start, i if oracle[run][q][1] < li else i + 1))
                    start = None
            for a, b in found:
                t0, t1 = oracle[run][seqs[a]][0], oracle[run][seqs[b - 1]][0]
                onset = {}
                for cfg, dd in runs.items():
                    hits = [t for t, m in dd[run].values() if t0 - 2 - 1e-9 <= t <= t1 + 1e-9 and m >= li]
                    onset[cfg] = round(min(hits) - t0, 1) if hits else None
                if len({v is None for v in onset.values()}) > 1:
                    out.append({"run": run, "level": LEVELS[li], "t": [t0, t1], "onset_s": onset,
                                "after_collision": run in hit and t0 >= hit[run] - 1e-6})
    return out


def d_only_by_truth(s, oracle_suffix):
    """D's unsupported HIGH/CRITICAL frames that C does not have, by what truth shows at D's threat track."""
    c = failures(s, "C", oracle_suffix)["unsupported_frames"]
    d = failures(s, "D", oracle_suffix)["unsupported_frames"]
    cset, dset = {(r[0], r[1], r[3]) for r in c}, {(r[0], r[1], r[3]) for r in d}
    want = collections.defaultdict(dict)
    for r in d:
        if (r[0], r[1], r[3]) not in cset:
            want[r[0]][r[1]] = r
    cycles = {}
    for line in gzip.open(DIRS["s14d"] / s / "trace-p1p2p4" / "cycles.jsonl.gz", "rt"):
        run = line[8:line.index('"', 8)]
        if run in want:
            row = json.loads(line)
            if row["seq"] in want[run]:
                cycles[(run, row["seq"])] = row
    kinds = collections.Counter()
    for run, rows in want.items():
        truth = pa.truth_frames(R / "campaigns" / SETS[s][0] / "runs" / run / "ground_truth.jsonl")
        for seq, r in rows.items():
            cyc = cycles[(run, seq)]
            track = next((x for x in cyc["tracks"] if x[1] == cyc["decision"][2]), None)
            if track is None:
                kinds["%s | the threat is not a track" % r[3]] += 1
                continue
            unseen = round(cyc["t"] - track[7], 1)
            seen = "measured" if unseen < 0.01 else "unseen <=3 s" if unseen <= 3 else "unseen >3 s"
            _, vehicles = truth.get(round(cyc["t"], 1), (None, []))
            gap, v = min(((abs(v["centre"][0] - track[2]) + abs(v["centre"][1] - track[3]), v) for v in vehicles),
                         default=(None, None), key=lambda p: p[0])
            if v is None or gap > 3.0:
                kind = "no vehicle within 3 m of the track (%s)" % seen
            else:
                kind = "%s vehicle there (track %s)" % ("a stationary" if abs(v["v"][0]) < 0.5 else "a moving", seen)
            kinds["%s | %s" % (r[3], kind)] += 1
    return {"D_only": sum(len(r) for r in want.values()), "C_only": len(cset - dset), "D_only_by_truth": dict(kinds)}


def lead_phantoms(f):
    lead = f["lead_slot"]
    return {"present": lead.get("present", 0), "correct": lead.get("correct", 0),
            "not correct": lead.get("present", 0) - lead.get("correct", 0),
            "not correct, excluding real targets beyond the truth window":
                lead.get("present", 0) - lead.get("correct", 0)
                - lead.get("beyond the truth window: measured, a real radar target", 0),
            "categories": {k: v for k, v in lead.items() if k not in ("present", "correct")}}


def main(out):
    result = {"schema": "roadsense.stage14-comparison.v1", "truth": "evaluation only",
              "data": "all recordings are historical development and regression data (Stage 14 instruction)",
              "configs": {k: v[0] + next((n for tail, n in SUPERSEDED.items() if k.endswith(tail)), "")
                          for k, v in CONFIGS.items()},
              "sets": {}}
    for s in SETS:
        hit = collisions(s)
        perception.use_road("highway_lht")
        res = {"collision_runs": hit, "episodes_where_configurations_differ": episodes(s, hit),
               "D_only_unsupported": {k: d_only_by_truth(s, o) for k, o in (("oracle_footprint", ""), ("oracle_body", "-body"))}}
        for cfg in CONFIGS:
            a, row = audit(s, cfg), {}
            m = a["missed_after_confirmation_allowance_by"]
            row["missed_tracking"] = {k: {b: [c["missed"], c["observable_frames"]] for b, c in m[k].items()}
                                      for k in ("speed_100m", "type_100m", "range")}
            row["criteria"] = a["criteria"]
            for oracle in ("", "-body"):
                f = failures(s, cfg, oracle)
                frames = f["unsupported_frames"]
                pre = [x for x in frames if x[0] not in hit or x[2] < hit[x[0]] - 1e-6]
                key = "oracle_footprint" if not oracle else "oracle_body"
                row[key] = {
                    "unsupported": {lv: sum(x[3] == lv for x in frames) for lv in ("HIGH", "CRITICAL")},
                    "unsupported_before_collision": {lv: sum(x[3] == lv for x in pre) for lv in ("HIGH", "CRITICAL")},
                    "unsupported_causes": f["unsupported_high_critical"],
                    "missed": f["missed_warnings"], "onset": f["warning_onset_s"]}
                if not oracle:
                    row.update(lead=lead_phantoms(f), coasting=f["coasting"], stale=f["stale_before_removal_s"],
                               duplicates=f["duplicates"], lane=f["lane_assignment"],
                               stationary=f["stationary_vehicles"], handover=f["handover"],
                               uncertainty=f["uncertainty_while_coasting"])
            res[cfg] = row
        result["sets"][s] = res
    Path(out).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(out)


if __name__ == "__main__":
    main(sys.argv[1])
