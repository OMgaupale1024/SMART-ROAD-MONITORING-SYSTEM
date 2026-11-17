"""Phase 6B acceptance client: drive the deployed Pi service with the benchmark scenario at 5 Hz.

usage: PYTHONPATH=src python pi_accept.py LABEL SECONDS RUN_ID [START_SEQUENCE] > result.json
Writes LABEL.states (one sha256 per published state, without generated_at) beside this script.
"""
import hashlib, http.client, json, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, "tools")
import bench_history as b
from websockets.sync.client import connect

PI, PORT = "192.168.0.104", 8001
SSH = ["ssh", "-i", str(Path.home() / ".ssh/roadsense_pi"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
       "omgaupale@" + PI]
PROBE = ('pid=$(systemctl --user show roadsense-edge -p MainPID --value); '
         'echo $pid $(getconf CLK_TCK) $(cut -d" " -f14,15 /proc/$pid/stat) '
         '$(grep -E "VmRSS|VmHWM" /proc/$pid/status | tr -s " " | cut -d" " -f2) '
         '$(cut -d" " -f1-4 /proc/loadavg) $(vcgencmd measure_temp) $(vcgencmd get_throttled)')


def probe():
    f = subprocess.run(SSH + [PROBE], capture_output=True, text=True, timeout=20).stdout.split()
    return {"pid": int(f[0]), "cpu_s": (int(f[2]) + int(f[3])) / int(f[1]), "rss_mb": int(f[4]) / 1024,
            "hwm_mb": int(f[5]) / 1024, "load1": float(f[6]), "temp": f[10], "throttled": f[11]}


def get(path):
    c = http.client.HTTPConnection(PI, PORT, timeout=10)
    c.request("GET", path)
    return json.loads(c.getresponse().read())


def main(label, seconds, run_id, start=1):
    bodies = [json.dumps(f).encode() for f in b.scenario(float(seconds), run_id, start_sequence=int(start))]
    hashes, actions, stop = [], {}, threading.Event()
    before = probe()

    def collect():
        with connect("ws://%s:%d/ws/live" % (PI, PORT), open_timeout=10) as ws:
            ready.set()
            while not stop.is_set():
                try:
                    msg = ws.recv(timeout=0.5)
                except TimeoutError:
                    continue
                state = b.comparable(msg)
                if state["simulation"]["run_id"] != run_id:
                    continue  # the state that was already there when we connected
                hashes.append(hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest())
                a = state["unified_safety"]["recommended_action"]
                actions[a] = actions.get(a, 0) + 1

    pings, rates = [], []

    def ping():
        c = http.client.HTTPConnection(PI, PORT, timeout=5)
        while not stop.is_set():
            t = time.perf_counter()
            c.request("GET", "/api/live-state")
            c.getresponse().read()
            pings.append(time.perf_counter() - t)
            stop.wait(0.1)

    def rate():
        while not stop.wait(10):
            rates.append(get("/api/system")["edge"]["rate_hz"])

    ready = threading.Event()
    threads = [threading.Thread(target=f, daemon=True) for f in (collect, ping, rate)]
    threads[0].start()
    ready.wait(10)
    for t in threads[1:]:
        t.start()
    conn = http.client.HTTPConnection(PI, PORT, timeout=10)
    posts, statuses, t0 = [], {}, time.monotonic()
    for i, body in enumerate(bodies):
        delay = t0 + i / 5.0 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        t = time.perf_counter()
        conn.request("POST", "/api/sensor-frame", body, {"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        posts.append(time.perf_counter() - t)
        statuses[r.status] = statuses.get(r.status, 0) + 1
    duration = time.monotonic() - t0
    time.sleep(3.0)
    stop.set()
    for t in threads:
        t.join(10)
    system = get("/api/system")
    after = probe()
    Path(__file__).with_name(label + ".states").write_text("\n".join(hashes) + "\n")
    e = system["edge"]
    out = {"label": label, "run_id": run_id, "frames": len(bodies), "duration_s": round(duration, 1),
           "client_rate_hz": round(len(bodies) / duration, 3), "statuses": statuses,
           "server_rate_hz_every_10s": {"min": min(rates), "mean": round(sum(rates) / len(rates), 2),
                                        "n": len(rates)} if rates else None,
           "server_latency_ms": {k: e["latency_ms"][k] for k in ("process", "publish", "handler")},
           "server_samples": e["latency_ms"]["samples"], "missed": e["frames"]["missed"],
           "post_rtt_ms": b._ms(posts), "ping_rtt_ms": b._ms(pings), "pings": len(pings),
           "states_received": len(hashes), "actions": actions,
           "process": {"pid": after["pid"], "same_pid": before["pid"] == after["pid"],
                       "cpu_s": round(after["cpu_s"] - before["cpu_s"], 2),
                       "cpu_percent_of_one_core": round(100 * (after["cpu_s"] - before["cpu_s"]) / (duration + 3), 2),
                       "rss_mb_before": round(before["rss_mb"], 1), "rss_mb_after": round(after["rss_mb"], 1),
                       "hwm_mb": round(after["hwm_mb"], 1)},
           "host": {"load1_before": before["load1"], "load1_after": after["load1"],
                    "temp_before": before["temp"], "temp_after": after["temp"], "throttled": after["throttled"]},
           "ai": system["ai"], "history": system.get("history")}
    json.dump(out, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main(*sys.argv[1:])
