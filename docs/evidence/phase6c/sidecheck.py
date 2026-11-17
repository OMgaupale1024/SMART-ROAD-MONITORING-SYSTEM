"""Perceived left/right/lead slots vs the ground-truth side of the nearest true vehicle."""
import json, sqlite3, sys, math
sys.path.insert(0, "tools"); import scenarios as sc
C, run = sys.argv[1:3]
gt = {}
for l in open(C + "/runs/%s/ground_truth.jsonl" % run):
    r = json.loads(l)
    if r["record"] == "frame": gt[round(r["t"], 1)] = r
db = sqlite3.connect("file:%s/history/roadsense-history.sqlite3?mode=ro" % C, uri=True)
ok = bad = 0; far_bad = 0
for sim, feats in db.execute("SELECT s.sim_s, s.features FROM samples s JOIN segments g USING (segment_id) WHERE g.run_id=?", (run,)):
    f = json.loads(feats); g = gt.get(round(sim, 1))
    if not g: continue
    for slot in ("left", "right", "lead"):
        if not f.get(slot + "_present"): continue
        lon, lat = f[slot + "_lon_m"], f[slot + "_lat_m"]
        best = min(((math.hypot(glon - lon, glat - lat), glon, glat) for v in g["vehicles"]
                    for glon, glat in [sc._ego_frame(g["ego"], v["x"] - v["length"] / 2 * math.cos(v["yaw"]),
                                                     v["y"] - v["length"] / 2 * math.sin(v["yaw"]))]), default=None)
        if best is None: continue
        want = {"left": best[2] > 1.9, "right": best[2] < -1.9, "lead": abs(best[2]) < 1.9 and best[1] > 0}[slot]
        if best[0] < 6 and want: ok += 1
        else:
            bad += 1; far_bad += lon > 50
print(run, "agree", ok, "disagree", bad, "(of which beyond 50 m: %d)" % far_bad)
