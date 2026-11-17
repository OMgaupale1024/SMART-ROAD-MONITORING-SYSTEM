"""Measure what the history store costs the edge server, on a repeatable synthetic Webots-like workload.

    PYTHONPATH=src python tools/bench_history.py inproc [--seconds 120] [--rate 5] [--dataset]
    PYTHONPATH=src python tools/bench_history.py server [--seconds 300] [--port 8011] [--dataset]

inproc feeds the same sensor frames to a LiveStateHub without and then with a HistoryRecorder (same process),
timing ingest_sensors() per frame and observe() alone, and checks that every published state is identical. server
starts `python -m roadsense.web --no-browser` on an isolated port twice (history off, then on, in a temporary
directory) and drives it over HTTP at --rate Hz while pinging GET /api/live-state for event-loop responsiveness,
sampling RSS. Results are JSON on stdout. Nothing here touches the user's history directory or a running server.

The scenario (scenario()) repeats a 60 s episode at 22 m/s in the middle lane: a lead car brakes hard (SLOW_DOWN,
BRAKE, EMERGENCY_BRAKE) then changes lane, a slower car is overtaken on the left, a faster one passes on the right,
a follower keeps 25 m behind, and a pothole of cycling lane and severity lies every 250 m. It is synthetic: radar
targets are computed from these positions with perception.py's mounting, not recorded from Webots.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

from roadsense.autonomy import hazards, perception, pipeline
from roadsense.web import history, live
from roadsense.web.metrics import distribution

LANE_Y = [perception.CARRIAGEWAY_Y[0] + (i + 0.5) * perception.LANE_WIDTH_M for i in range(3)]
EGO_LANE, SPEED, EPISODE_S, START_X = 1, 22.0, 60.0, -100.0
LIMITS = {"radar front": [1.0, 150.0, 2.6], "radar rear": [1.0, 100.0, 2.6]}
POTHOLE_LANES, SEVERITIES = (1, 1, 0, 1, 2), ("HIGH", "MEDIUM", "LOW", "HIGH", "MEDIUM")


def _vehicles(tau):
    """(name, lane, longitudinal m from the ego origin) at episode time tau."""
    if tau < 20:
        lead = (1, 60.0)
    elif tau < 23:
        lead = (1, 60.0 - 2.0 * (tau - 20) ** 2)  # brakes at 4 m/s² relative
    elif tau < 25:
        lead = (1, 42.0 - 12.0 * (tau - 23))
    else:
        lead = (2, 18.0 + 8.0 * (tau - 25))  # changes to the right lane and pulls away
    return [("lead",) + lead, ("left", 0, 40.0 - 4.0 * tau), ("follower", 1, -25.0), ("right", 2, -80.0 + 6.0 * tau)]


def _radar(name, lon, lat):
    _, mount_x, mount_y, mount_yaw = perception.RADARS[name]
    dx, dy = lon - mount_x, lat - mount_y
    distance, a = math.hypot(dx, dy), math.atan2(dy, dx)
    azimuth = (mount_yaw - a + math.pi) % (2 * math.pi) - math.pi
    low, high, fov = LIMITS[name]
    if (dx > 0) != (mount_yaw == 0) or not low < distance < high or abs(azimuth) > fov / 2 - 0.01:
        return None
    return distance, azimuth


def scenario(seconds=120.0, run_id="bench-run", period=0.2, start_sequence=1, end=True):
    """Sensor frames (roadsense.sensors.v1 dicts) of `seconds` of the repeating episode; the last is the end marker
    when end is set."""
    previous = {}
    n = int(round(seconds / period))
    for i in range(1, n + 1):
        t = round(i * period, 3)
        tau = t % EPISODE_S
        ego_x, ego_y = START_X - SPEED * t, LANE_Y[EGO_LANE]
        targets = {name: [] for name in LIMITS}
        sides = {"left": [10.0, 10.0], "right": [10.0, 10.0]}
        for vehicle, lane, lon in _vehicles(tau):
            lat = ego_y - LANE_Y[lane]
            for name in LIMITS:
                hit = _radar(name, lon, lat)
                if hit is None:
                    continue
                key = (vehicle, name)
                rate = None if key not in previous else round((hit[0] - previous[key]) / period, 3)
                if rate is not None and abs(rate) > 40:
                    rate = None  # an episode restart, not motion
                previous[key] = hit[0]
                targets[name].append([round(hit[0], 3), round(hit[1], 5), rate])
            if abs(lon) < 6 and lane != EGO_LANE:
                sides["left" if lane < EGO_LANE else "right"][0] = 2.0
        objects = []
        for k in range(int((SPEED * t) // 250) + 3):
            px = START_X - 300.0 - 250.0 * k + 0.15 * math.sin(1.7 * i + k)
            py = LANE_Y[POTHOLE_LANES[k % 5]] + 0.3 + 0.1 * math.cos(1.3 * i + k)
            x, y = (ego_x - px) - hazards.HAZARD_CAMERA[0], ego_y - py
            if 1.0 < x < 60.0 and abs(y) < 0.6 * x:
                objects.append([[round(x, 3), round(y, 3), -0.3],
                                "pothole severity=%s length=1.2 width=0.9 depth=0.08" % SEVERITIES[k % 5]])
        last = end and i == n
        frame = {
            "schema": pipeline.SENSOR_SCHEMA, "timestamp": t,
            "ego": {"vehicle_id": "EGO_ROADSENSE", "position": [round(ego_x, 3), ego_y, 0.3],
                    "velocity": [-SPEED, 0.0], "speed_mps": SPEED, "yaw_rad": math.pi, "steering_rad": 0.0,
                    "lane": EGO_LANE, "maneuver": "lane_keep"},
            "radars": {name: {"targets": targets[name], "limits": LIMITS[name]} for name in LIMITS},
            "objects": objects, "sides": sides,
            "simulation": {"run_id": run_id, "sequence": start_sequence + i - 1, "active": True},
        }
        yield frame
        if last:
            yield dict(frame, simulation=dict(frame["simulation"], sequence=frame["simulation"]["sequence"] + 1,
                                              active=False))


def comparable(state_json):
    """A published state without its only wall-clock field."""
    state = json.loads(state_json)
    state["simulation"].pop("generated_at_unix_s", None)
    return state


def replay(bodies, config=None, rate=0.0):
    """Feed encoded frames to a fresh hub (with a recorder when config is given); per-frame seconds and states."""
    recorder = history.HistoryRecorder(config) if config else None
    observe_s = []
    listeners = []
    if recorder:
        def timed(state):
            started = time.perf_counter()
            recorder.observe(state)
            observe_s.append(time.perf_counter() - started)
        listeners.append(timed)
    hub = live.LiveStateHub(listeners=listeners)
    timings, states = [], []
    start = time.monotonic()
    for i, body in enumerate(bodies):
        if rate:
            delay = start + i / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        started = time.perf_counter()
        hub.ingest_sensors(body)
        timings.append(time.perf_counter() - started)
        states.append(hub.latest_json)
    status = None
    if recorder:
        close_started = time.perf_counter()
        recorder.close()
        status = dict(recorder.status(), close_s=round(time.perf_counter() - close_started, 3))
    return timings, observe_s, states, status, hub.metrics.summary()


def _ms(values):
    return distribution([1000.0 * v for v in values], 4)


def state_bytes(bodies, count=256):
    """Python heap held by `count` published states (what a full queue of that size keeps alive), per state."""
    hub = live.LiveStateHub()
    for body in bodies[:50]:
        hub.ingest_sensors(body)
    kept = []
    hub = live.LiveStateHub(listeners=[kept.append])
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    for body in bodies[:count]:
        hub.ingest_sensors(body)
    held = tracemalloc.get_traced_memory()[0] - before
    tracemalloc.stop()
    return round(held / max(1, len(kept)))


def inproc(args):
    frames = list(scenario(args.seconds, "bench-inproc"))
    bodies = [json.dumps(f).encode() for f in frames]
    out = {"workload": {"frames": len(bodies), "sim_seconds": args.seconds, "rate_hz": args.rate or "max",
                        "python": sys.version.split()[0], "platform": sys.platform}}
    with tempfile.TemporaryDirectory(prefix="roadsense-history-bench-") as directory:
        config = history.Config(enabled=True, directory=directory, dataset=args.dataset)
        wall = time.monotonic()
        off, _, off_states, _, off_metrics = replay(bodies, None, args.rate)
        out["off_wall_s"] = round(time.monotonic() - wall, 1)
        wall = time.monotonic()
        on, observe_s, on_states, status, on_metrics = replay(bodies, config, args.rate)
        out["on_wall_s"] = round(time.monotonic() - wall, 1)
        db = Path(directory) / history.DB_NAME
        out["db_bytes"] = db.stat().st_size
        out["wal_bytes"] = (Path(str(db) + "-wal").stat().st_size if Path(str(db) + "-wal").exists() else 0)
    actions = {}
    for s in off_states:
        a = json.loads(s)["unified_safety"]["recommended_action"]
        actions[a] = actions.get(a, 0) + 1
    out.update(
        identical_states=[comparable(a) for a in off_states] == [comparable(b) for b in on_states],
        actions=actions,
        ingest_ms={"off": _ms(off), "on": _ms(on)},
        handler_process_ms={"off": off_metrics["latency_ms"]["process"], "on": on_metrics["latency_ms"]["process"]},
        observe_us=distribution([1e6 * v for v in observe_s], 2),
        history=status,
        state_heap_bytes=state_bytes(bodies),
    )
    json.dump(out, sys.stdout, indent=2)
    print()


def _post(conn, body):
    conn.request("POST", "/api/sensor-frame", body, {"Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    return response.status


def _get(conn, path):
    conn.request("GET", path)
    response = conn.getresponse()
    return response.status, response.read()


def _rss_kb(pid):
    try:
        return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout)
    except ValueError:
        return None


def run_server(bodies, port, rate, environment):
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])),
               **environment)
    env.pop("ROADSENSE_AI_EXPLAIN", None)
    process = subprocess.Popen([sys.executable, "-m", "roadsense.web", "--host", "127.0.0.1", "--port", str(port),
                                "--no-browser"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                status, _ = _get(http.client.HTTPConnection("127.0.0.1", port, timeout=1), "/api/system")
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline or process.poll() is not None:
                raise RuntimeError("server on port %d did not start" % port)
            time.sleep(0.1)
        pings, rss, stop = [], [], threading.Event()

        def ping():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            while not stop.is_set():
                started = time.perf_counter()
                _get(conn, "/api/live-state")
                pings.append(time.perf_counter() - started)
                stop.wait(0.1)

        def sample():
            while not stop.is_set():
                rss.append(_rss_kb(process.pid))
                stop.wait(5.0)
        threads = [threading.Thread(target=ping, daemon=True), threading.Thread(target=sample, daemon=True)]
        for th in threads:
            th.start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        posts, statuses = [], {}
        start = time.monotonic()
        for i, body in enumerate(bodies):
            delay = start + i / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            started = time.perf_counter()
            code = _post(conn, body)
            posts.append(time.perf_counter() - started)
            statuses[code] = statuses.get(code, 0) + 1
        duration = time.monotonic() - start
        time.sleep(3.0)  # let the writer commit, so its counters are final
        stop.set()
        for th in threads:
            th.join(10)
        system = json.loads(_get(http.client.HTTPConnection("127.0.0.1", port, timeout=5), "/api/system")[1])
        rss.append(_rss_kb(process.pid))
        stopped = time.perf_counter()
        process.terminate()
        process.wait(15)
        return {"duration_s": round(duration, 1), "posts": len(posts), "statuses": statuses,
                "post_rtt_ms": _ms(posts), "ping_rtt_ms": _ms(pings), "pings": len(pings),
                "server_latency_ms": {k: system["edge"]["latency_ms"][k] for k in ("process", "publish", "handler")},
                "server_samples": system["edge"]["latency_ms"]["samples"], "rate_hz": system["edge"]["rate_hz"],
                "rss_mb": {"first": round(rss[0] / 1024, 1), "last": round(rss[-1] / 1024, 1),
                           "max": round(max(r for r in rss if r) / 1024, 1)},
                "history": system["history"], "shutdown_s": round(time.perf_counter() - stopped, 3)}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def server(args):
    bodies = [json.dumps(f).encode() for f in scenario(args.seconds, "bench-server")]
    out = {"workload": {"frames": len(bodies), "rate_hz": args.rate, "sim_seconds": args.seconds,
                        "python": sys.version.split()[0], "platform": sys.platform}}
    with tempfile.TemporaryDirectory(prefix="roadsense-history-bench-") as directory:
        out["off"] = run_server(bodies, args.port, args.rate, {"ROADSENSE_HISTORY_ENABLE": "0"})
        env = {"ROADSENSE_HISTORY_ENABLE": "1", "ROADSENSE_HISTORY_DIR": directory,
               "ROADSENSE_HISTORY_DATASET": "1" if args.dataset else "0"}
        out["on"] = run_server(bodies, args.port, args.rate, env)
        db = Path(directory) / history.DB_NAME
        out["db_bytes"] = db.stat().st_size
        wal = Path(str(db) + "-wal")
        out["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        out["db_bytes_per_hour"] = round(out["db_bytes"] * 3600.0 / out["on"]["duration_s"])
    json.dump(out, sys.stdout, indent=2)
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name, seconds, rate in (("inproc", 120.0, 0.0), ("server", 300.0, 5.0)):
        p = sub.add_parser(name)
        p.add_argument("--seconds", type=float, default=seconds, help="simulation seconds of frames (5 per s)")
        p.add_argument("--rate", type=float, default=rate, help="frames per wall second (0: as fast as possible)")
        p.add_argument("--dataset", action="store_true", help="also record dataset samples")
        if name == "server":
            p.add_argument("--port", type=int, default=8011)
    args = parser.parse_args(argv)
    (inproc if args.mode == "inproc" else server)(args)


if __name__ == "__main__":
    main()
