import json, sqlite3, sys, hashlib
C = sys.argv[1]
db = sqlite3.connect("file:%s/history/roadsense-history.sqlite3?mode=ro" % C, uri=True)
cols = [r[1] for r in db.execute("PRAGMA table_info(samples)")]
def gt(run):
    out = []
    for l in open("%s/runs/%s/ground_truth.jsonl" % (C, run)):
        r = json.loads(l)
        if r["record"] == "header": continue
        out.append(r)
    return out
def samples(run):
    rows = db.execute("SELECT s.sim_s, s.features, s.labels FROM samples s JOIN segments g USING (segment_id) WHERE g.run_id=? ORDER BY s.sim_s", (run,)).fetchall()
    return [(round(a, 2), json.loads(b), json.loads(c)) for a, b, c in rows]
for fam in ("normal", "sudden_brake", "cut_in", "tailgating", "potholes", "mixed", "combined"):
    a, b = "lht-%s-1" % fam, "lht-%s-1-repeat" % fam
    ga, gb = gt(a), gt(b)
    sa, sb = samples(a), samples(b)
    feat_diff = sum(1 for x, y in zip(sa, sb) if x[1] != y[1]); lab_diff = sum(1 for x, y in zip(sa, sb) if x[2] != y[2])
    first = next((i for i, (x, y) in enumerate(zip(ga, gb)) if x != y), None)
    print("%-13s gt records %d/%d identical=%s first_diff=%s | samples %d/%d features_differ=%d labels_differ=%d" % (
        fam, len(ga), len(gb), ga == gb, first if first is None else (first, ga[first].get("t")), len(sa), len(sb), feat_diff, lab_diff))
print("sample columns:", cols)
import collections
keys = collections.Counter()
for fam in ("tailgating", "normal"):
    for x, y in zip(samples("lht-%s-1" % fam), samples("lht-%s-1-repeat" % fam)):
        keys.update(k for k in x[1] if x[1][k] != y[1].get(k))
print("differing feature keys:", keys.most_common(12))
for fam in ("normal", "sudden_brake"):
    ga, gb = gt("lht-%s-1" % fam), gt("lht-%s-1-repeat" % fam)
    i = next(i for i, (x, y) in enumerate(zip(ga, gb)) if x != y)
    x, y = ga[i], gb[i]
    print(fam, "t", x.get("t"), "ego equal:", x.get("ego") == y.get("ego"), x.get("ego"), y.get("ego"))
    va = {v["id"]: v for v in x.get("vehicles", [])}; vb = {v["id"]: v for v in y.get("vehicles", [])}
    print("   vehicles differ:", [(k, va.get(k), vb.get(k)) for k in sorted(set(va) | set(vb)) if va.get(k) != vb.get(k)][:2])
