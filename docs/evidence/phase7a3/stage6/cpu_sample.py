"""Per-thread CPU of one process over an interval, read from /proc (read-only). Usage: cpu_sample.py PID SECONDS"""
import json
import os
import sys
import time


def threads(pid):
    out = {}
    for tid in os.listdir("/proc/%d/task" % pid):
        try:
            with open("/proc/%d/task/%s/stat" % (pid, tid)) as f:
                stat = f.read()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2:].split()
        out[int(tid)] = (fields[0], int(fields[11]), int(fields[12]), int(fields[19]))  # state, utime, stime, starttime (stat 3, 14, 15, 22)
    return out


def system():
    with open("/proc/stat") as f:
        values = [int(v) for v in f.readline().split()[1:]]
    return sum(values), values[3] + values[4]


pid, seconds = int(sys.argv[1]), float(sys.argv[2])
hz = os.sysconf("SC_CLK_TCK")
a, (ta, ia), t0 = threads(pid), system(), time.monotonic()
time.sleep(seconds)
b, (tb, ib), t1 = threads(pid), system(), time.monotonic()
elapsed = t1 - t0
rows = {}
for tid, (state, user, sys_, started) in sorted(b.items()):
    if tid in a:
        du, ds = user - a[tid][1], sys_ - a[tid][2]
        rows[tid] = {"state": state, "user_ticks": du, "system_ticks": ds, "start_ticks": started,
                     "core_pct": round(100.0 * (du + ds) / hz / elapsed, 2)}
with open("/proc/loadavg") as f:
    load = f.read().split()[:3]
print(json.dumps({"pid": pid, "seconds": round(elapsed, 2), "clk_tck": hz, "threads": rows,
                  "process_core_pct": round(sum(r["core_pct"] for r in rows.values()), 2),
                  "system_busy_pct_of_all_cores": round(100.0 * (1 - (ib - ia) / (tb - ta)), 2),
                  "loadavg": load}, sort_keys=True))
