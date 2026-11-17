"""CPU per frame and database growth over the 300 s benchmark scenario: history off / events only / dataset.

Run from the repository root: PYTHONPATH=src python docs/evidence/phase6a/cpu_growth.py <scratch directory>
"""
import json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "tools")
import bench_history as b
from roadsense.web import history, live
bodies = [json.dumps(f).encode() for f in b.scenario(300, "cpu-run")]
out = {"frames": len(bodies), "sim_s": 300}
for mode in ("off", "events", "dataset"):
    with tempfile.TemporaryDirectory(dir=sys.argv[1]) as d:
        rec = None if mode == "off" else history.HistoryRecorder(history.Config(
            enabled=True, directory=d, dataset=mode == "dataset", queue_size=100_000, close_timeout_s=120))
        hub = live.LiveStateHub(listeners=[rec.observe] if rec else [])
        cpu, wall = time.process_time(), time.perf_counter()
        for body in bodies:
            hub.ingest_sensors(body)
        if rec:
            rec.close()
        cpu, wall = time.process_time() - cpu, time.perf_counter() - wall
        r = {"cpu_s": round(cpu, 3), "cpu_us_per_frame": round(1e6 * cpu / len(bodies), 1)}
        if rec:
            s = rec.status()
            db = Path(d) / history.DB_NAME
            r.update(rows=s["rows"], lost=sum(s["lost"].values()), db_bytes=db.stat().st_size,
                     wal_bytes=(Path(str(db) + "-wal").stat().st_size if Path(str(db) + "-wal").exists() else 0))
        out[mode] = r
for mode in ("events", "dataset"):
    out[mode]["overhead_cpu_us_per_frame"] = round(out[mode]["cpu_us_per_frame"] - out["off"]["cpu_us_per_frame"], 1)
    out[mode]["core_percent_at_5hz"] = round(out[mode]["overhead_cpu_us_per_frame"] * 5 / 1e4, 3)
print(json.dumps(out, indent=1))
