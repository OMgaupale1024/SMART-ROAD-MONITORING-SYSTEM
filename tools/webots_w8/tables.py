import json, sys
d = json.load(open(sys.argv[1]))
f = lambda s, k="p50": "—" if not s or s.get("n", 0) == 0 or s.get(k) is None else "%.2f" % s[k]
pp = lambda s: "%s / %s" % (f(s), f(s, "p95"))
print("| Run | RTF | wall s | CPU s | RSS MB | lag s | along m p50/p95 | cross p95 | front lon vs SUMO p50 | front rr err p50/p95 | rr first p95 | rear rr p95 |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
for k, v in d.items():
    p, fr, re_ = v["pose_vs_sumo"], v["radar"]["profile radar front"], v["radar"]["profile radar rear"]
    print("| %s | %.2f | %.0f | %.0f | %.0f | %s | %s | %s | %s | %s | %s | %s |" % (
        k, v["rtf"], v["run"]["wall_s"], v["run"]["cpu_user_s"] + v["run"]["cpu_sys_s"], v["run"]["max_rss_mb_children"],
        f(p["lag_s"]), pp(p["along_m"]), f(p["cross_m"], "p95"), f(fr["lon_s"]), pp(fr["rr_err"]), f(fr["rr_err_first"], "p95"), f(re_["rr_err"], "p95")))
print()
print("| Run | radar | matched | self | unmatched | lon vs body p50/p95 | lat vs body p50/p95 | lon vs SUMO p50/p95 | lat vs SUMO p95 | clearance optimism p50/p95 | rr p95 |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for k, v in d.items():
    for n, r in v["radar"].items():
        if r["returns"] == 0: continue
        print("| %s | %s | %d | %d | %d | %s | %s | %s | %s | %s | %s |" % (k, n.replace("profile radar ", ""), r["matched"], r["self"], r["unmatched"], pp(r["lon_c"]), pp(r["lat_c"]), pp(r["lon_s"]), f(r["lat_s"], "p95"), pp(r["clear_err"]), f(r["rr_err"], "p95")))
