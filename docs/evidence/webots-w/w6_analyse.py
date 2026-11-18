"""W6: per variant, real-time factor and range-rate consistency of the front radar in the real highway world."""
import json, math, sys, glob, os, statistics as st
S = os.path.dirname(os.path.abspath(__file__))
out = {}
for d in sorted(glob.glob(S + "/v_*")):
    name = os.path.basename(d)
    lines = [json.loads(l.replace("NaN", "null")) for l in open(d + "/probe.jsonl")]
    t0, t1 = float(open(d + "/t_start.txt").read()), float(open(d + "/t_done.txt").read())
    con = open(d + "/console.txt").read()
    rss = [int(l.split()[0]) for l in con.splitlines() if "maximum resident set size" in l]
    dt = lines[1]["t"] - lines[0]["t"]
    jumps, pairs, stale = 0, 0, 0
    rr_all = []
    for a, b in zip(lines, lines[1:]):
        for rad in ("radar front", "radar rear"):
            ta = [x for x in a["radars"][rad] if x[0] > 2 and x[2] is not None and abs(x[2]) < 100]
            tb = [x for x in b["radars"][rad] if x[0] > 2 and x[2] is not None and abs(x[2]) < 100]
            for x in tb:
                # same vehicle: predicted range within 1.5 m and azimuth within 0.05 rad
                c = [y for y in ta if abs(y[0] + x[2] * dt - x[0]) < 1.5 and abs(y[1] - x[1]) < 0.05]
                if not c:
                    continue
                y = min(c, key=lambda y: abs(y[0] + x[2] * dt - x[0]))
                pairs += 1
                rr_all.append(x[2])
                if abs(x[2] - y[2]) > 3.0:
                    jumps += 1
                # a stale refresh: the target's own displacement was zero, so the range rate equals minus the
                # ego's radial speed; flag cycles where the range rate changed by more than 3 m/s in one cycle
    out[name] = {"radar_period_s": round(dt, 3), "cycles": len(lines), "sim_s": lines[-1]["t"],
                 "wall_s": round(t1 - t0, 1), "rtf": round(lines[-1]["t"] / (t1 - t0), 2),
                 "max_rss_mb": round(rss[0] / 2 ** 20, 1) if rss else None,
                 "linked_target_pairs": pairs, "range_rate_jumps_gt3mps": jumps,
                 "jump_share": round(jumps / pairs, 3) if pairs else None,
                 "errors": [l for l in con.splitlines() if l.startswith("ERROR")][:5]}
print(json.dumps(out, indent=1))
