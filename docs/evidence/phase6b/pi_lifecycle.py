"""Phase 6B lifecycle checks on the deployed Pi service (history on): restart, gap + stall, SIGKILL.

One scenario, posted in slices so simulation time only moves forward:
  seq   1-150   lifetime 1
  restart (systemctl --user restart)                  -> run_resumed (inferred) 150 -> 161
  seq 161-260   lifetime 2
  pause 4 s, skip 261-270                             -> sequence_gap (observed), stream_stall (measured)
  seq 271-370   lifetime 2
  seq 371-420, then SIGKILL of MainPID, systemd restarts it
  seq 431-480   lifetime 3                            -> processor_end_not_observed (inferred), run_resumed
"""
import http.client, json, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, "tools")
import bench_history as b

PI, PORT = "192.168.0.104", 8001
SSH = ["ssh", "-i", str(Path.home() / ".ssh/roadsense_pi"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
       "omgaupale@" + PI]
FRAMES = [json.dumps(f).encode() for f in b.scenario(100, "phase6b-lifecycle", end=False)]


def ssh(command):
    return subprocess.run(SSH + [command], capture_output=True, text=True, timeout=60).stdout.strip()


def history():
    c = http.client.HTTPConnection(PI, PORT, timeout=10)
    c.request("GET", "/api/system")
    h = json.loads(c.getresponse().read())["history"]
    return {k: h[k] for k in ("state", "processor_id", "degraded", "losses_observed", "queue", "states", "lost", "rows",
                              "batches")}


def post(first, last):
    """Sequences first..last at 5 Hz; returns the HTTP statuses."""
    conn, statuses, t0 = http.client.HTTPConnection(PI, PORT, timeout=10), {}, time.monotonic()
    for i, body in enumerate(FRAMES[first - 1:last]):
        delay = t0 + i / 5.0 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        conn.request("POST", "/api/sensor-frame", body, {"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        statuses[r.status] = statuses.get(r.status, 0) + 1
    return statuses


def ready():
    ssh("for i in $(seq 1 60); do curl -sf -m 1 http://127.0.0.1:8001/api/system >/dev/null && break; sleep 0.5; done")


def pid():
    return ssh("systemctl --user show roadsense-edge -p MainPID -p NRestarts | tr '\\n' ' '")


out = {"start": pid()}
out["lifetime1"] = {"posted": post(1, 150)}
time.sleep(2.5)  # one flush interval
out["lifetime1"]["history"] = history()
ssh("systemctl --user restart roadsense-edge")
ready()
out["after_restart"] = pid()
out["lifetime2"] = {"posted_a": post(161, 260)}
time.sleep(4.0)
out["lifetime2"]["posted_b"] = post(271, 370)
out["lifetime2"]["posted_c"] = post(371, 420)
out["lifetime2"]["history_before_kill"] = history()
ssh("kill -KILL $(systemctl --user show roadsense-edge -p MainPID --value)")
time.sleep(1.0)
ready()
out["after_kill"] = pid()
out["lifetime3"] = {"posted": post(431, 480)}
time.sleep(2.5)
out["lifetime3"]["history"] = history()
json.dump(out, sys.stdout, indent=1)
print()
