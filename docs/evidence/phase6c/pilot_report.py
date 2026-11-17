import json, sys, hashlib, math, collections, os
from pathlib import Path
sys.path.insert(0, "tools"); import scenarios as sc
C = Path(sys.argv[1])
runs = {r["run_id"]: r for r in map(json.loads, open(C / "campaign.jsonl"))}
def h(p): return hashlib.sha256(p.read_bytes()).hexdigest()[:12]
print("== per run")
for rid, r in runs.items():
    g = r["ground_truth"]
    print("%-26s ok=%s wall=%5.1fs sim=%5.1fs x%.2f samples=%d joined=%d accepted=%d missed=%d rejected=%d gaps=%s MB=%.2f | gt %s ttc=%s gap=%s fhw=%s holes=%s | labels %s | events %s" % (
        rid, r["ok"], r["wall_s"], r["sim_s"], r["speed_x"], r["samples"], r["samples_joined_to_ground_truth"], r["server"]["accepted"], r["server"]["missed"], r["server"]["rejected"], r["gaps"] or "-", r["run_bytes"] / 1e6,
        g["frames_with"], g["min_lead_ttc_s"], g["min_lead_gap_m"], g["min_follow_headway_s"], g["potholes_under_ego"], r["label_actions"], r["events"]))
print("== reproducibility (run vs -repeat)")
for fam in sc.FAMILIES:
    a, b = C / "runs" / ("lht-%s-1" % fam), C / "runs" / ("lht-%s-1-repeat" % fam)
    same_inputs = all(h(a / f) == h(b / f) for f in ("worlds/scenario.wbt", "worlds/scenario_net/sumo.net.xml", "worlds/scenario_net/sumo.rou.xml"))
    pa, pb = json.load(open(a / "scenario.json")), json.load(open(b / "scenario.json"))
    pa.pop("run_id"); pb.pop("run_id")
    fa = [f for f in sc.read_ground_truth(a / "ground_truth.jsonl")[1]]; fb = sc.read_ground_truth(b / "ground_truth.jsonl")[1]
    dev = max(math.hypot(x["ego"]["x"] - y["ego"]["x"], x["ego"]["y"] - y["ego"]["y"]) for x, y in zip(fa, fb))
    first = next((x["t"] for x, y in zip(fa, fb) if x != y), None)
    ra, rb = runs["lht-%s-1" % fam], runs["lht-%s-1-repeat" % fam]
    la, lb = ra["label_actions"], rb["label_actions"]
    print("%-13s params_equal=%s inputs_identical=%s first_divergence_t=%s max_ego_dev=%.2fm | gt a %s b %s | min_ttc %s/%s | labels a %s b %s" % (
        fam, pa == pb, same_inputs, first, dev, ra["ground_truth"]["frames_with"], rb["ground_truth"]["frames_with"], ra["ground_truth"]["min_lead_ttc_s"], rb["ground_truth"]["min_lead_ttc_s"], la, lb))
tot = collections.Counter(); gtc = collections.Counter(); n = 0; wall = 0; sim = 0
for r in runs.values():
    tot.update(r["label_actions"]); gtc.update(r["ground_truth"]["frames_with"]); n += r["samples"]; wall += r["wall_s"]; sim += r["sim_s"]
print("== totals: runs", len(runs), "samples", n, "wall %.0fs sim %.0fs (x%.1f)" % (wall, sim, sim / wall), "labels", dict(tot), "gt frames", dict(gtc))
du = lambda p: sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file() and not f.is_symlink())
print("== storage: campaign %.1f MB, history db %.1f MB, runs %.1f MB" % (du(C) / 1e6, du(C / "history") / 1e6, du(C / "runs") / 1e6))
