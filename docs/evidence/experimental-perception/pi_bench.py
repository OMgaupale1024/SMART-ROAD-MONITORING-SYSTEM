"""Per-cycle fused timing on one recording; no tracemalloc. Prints one JSON line."""
import json, platform, resource, sys, time
import numpy
import experimental_replay as er
run = er.read_run(sys.argv[1])
t0 = time.perf_counter()
cyc, info = er.experimental_cycles(run, "fused")
wall = time.perf_counter() - t0
ms = sorted(c["ms"]["total"] for c in cyc)
auth = sorted(c["ms"]["total"] for c in er.original_cycles(run))
q = lambda v, p: v[min(len(v) - 1, int(round(p * (len(v) - 1))))]
print(json.dumps({"run": run["name"], "python": platform.python_version(), "numpy": numpy.__version__,
    "machine": platform.machine(), "cycles": len(ms),
    "fused_ms": {"p50": q(ms, .5), "p95": q(ms, .95), "p99": q(ms, .99), "max": ms[-1]},
    "over_100ms": sum(x > 100 for x in ms), "over_100ms_share": sum(x > 100 for x in ms) / len(ms),
    "stage_ms_p95": er.timing(cyc) and {k: v.get("p95") for k, v in er.timing(cyc).items()},
    "queue_10hz_cap2": er.queue_sim(cyc), "rejected_frames": info["rejected_frames"],
    "authoritative_ms": {"cycles": len(auth), "p50": q(auth, .5), "p95": q(auth, .95), "p99": q(auth, .99)},
    "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2 ** 20 if sys.platform == "darwin" else 2 ** 10), 1),
    "replay_wall_s": round(wall, 1)}))
