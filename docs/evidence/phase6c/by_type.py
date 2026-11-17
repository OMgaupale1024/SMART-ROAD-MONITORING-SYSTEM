import json, sqlite3, sys, collections
sys.path.insert(0, "tools"); import scenarios as sc
C = sys.argv[1]
db = sqlite3.connect("file:%s/history/roadsense-history.sqlite3?mode=ro" % C, uri=True)
hit = collections.Counter(); tot = collections.Counter(); err = collections.defaultdict(list)
for rid in [json.loads(l)["run_id"] for l in open(C + "/campaign.jsonl")]:
    gt = {}
    for l in open("%s/runs/%s/ground_truth.jsonl" % (C, rid)):
        r = json.loads(l)
        if r["record"] == "frame": gt[round(r["t"], 1)] = r
    for sim, f in db.execute("SELECT sim_s, features FROM samples s JOIN segments g USING (segment_id) WHERE g.run_id=?", (rid,)):
        g = gt.get(round(sim, 1)); f = json.loads(f)
        if not g: continue
        o = sc.frame_outcomes(g, [])
        if o["lead_gap_m"] is not None and 3 < o["lead_gap_m"] < 40:
            t = o["lead_type"]; tot[t] += 1
            if f["lead_present"]:
                hit[t] += 1; err[t].append(abs(f["lead_lon_m"] - sc.EGO_FRONT_M - o["lead_gap_m"]))
for t in sorted(tot):
    e = sorted(err[t]); print("%-12s true leads %5d  tracked %5.1f%%  median range error %.2f m" % (t, tot[t], 100.0 * hit[t] / tot[t], e[len(e) // 2] if e else float("nan")))
