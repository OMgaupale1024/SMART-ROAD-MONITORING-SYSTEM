"""Stages W9-W11: the experimental multi-sensor profile, an opt-in copy of the highway world, never the original.

    PYTHONPATH=src venv/bin/python tools/sensor_profile.py run --out DIR --sumo-ms 100 --radar-ms 100 [--sync stock|current|aligned|smooth]
        [--webots PATH] [--seed 1] [--duration 60] [--camera-s 0] [--stock-models]

Each run writes a generated world (roadsense_highway.wbt + the profile Robot nested in EGO_ROADSENSE's centre slot +
SumoInterface's step), its SUMO network (the Phase 6C scenario generator, family "normal", left-hand traffic, the
Indian vehicle mix), the sensor_profile_plugin and symlinked controllers into DIR, then runs Webots in fast batch mode
until the scenario plugin quits. The repository's world, network and controllers are only read. Development
recordings only: never acceptance data.

Sensor extrinsics (ego frame: origin at the rear axle centre, x forward, y left, z up; metres, radians):
"""
import argparse
import json
import math
import os
import re
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORLD = ROOT / "simulation/webots/worlds/roadsense_highway.wbt"
NET_DIR = ROOT / "simulation/webots/worlds/roadsense_highway_net"
INSTALLED = "/Applications/Webots.app/Contents/MacOS/webots"
SUMO_PORT = 8911  # never 8873 (an open Webots with the repository world) or 8901 (scenario_batch)

RADAR = ('rangeNoise 0.25 speedNoise 0.12 angularNoise 0.001 frequency 76.5 transmittedPower 31 '
         'minDetectableSignal -80 occlusion TRUE')
# name: (x, y, z, yaw, max range, horizontal FOV, vertical FOV); front and rear copy RoadSense's radars exactly
PROFILE_RADARS = {
    "profile radar front": (4.054, 0.0, 0.142, 0.0, 150, 2.6, 0.07854),
    "profile radar rear": (-1.16, 0.0, 0.33, 3.14159265, 100, 2.6, 0.07854),
    "profile radar corner fl": (3.85, 0.85, 0.142, 0.7854, 80, 1.6, 0.25),
    "profile radar corner fr": (3.85, -0.85, 0.142, -0.7854, 80, 1.6, 0.25),
    "profile radar corner rl": (-1.1, 0.85, 0.33, 2.3562, 80, 1.6, 0.25),
    "profile radar corner rr": (-1.1, -0.85, 0.33, -2.3562, 80, 1.6, 0.25),
}
PROFILE_LIDAR = (1.11, 0.0, 1.51)  # roof: sensorsSlotTop (1.11, 0, 1.16) + 0.35
PROFILE_CAMERA = (1.61, 0.0, 1.16)  # roof front edge, forward, horizontal FOV 1.05 rad, 1280x720
__doc__ += "".join("  %-26s x %6.3f  y %6.3f  z %5.3f  yaw %7.4f  range %3d m  hfov %.2f  vfov %.4f\n" % ((n,) + v)
                   for n, v in PROFILE_RADARS.items())
__doc__ += ("  profile lidar roof         x %.2f y %.2f z %.2f: 360 deg x 30 deg, 16 layers x 1800 columns, 0.5-100 m,\n"
            "                             noise 0.0003 (fraction of range), range image float32 m\n"
            "  profile camera 720p        x %.2f y %.2f z %.2f: recording only, never Recognition\n"
            "  profile gps / profile imu  at the ego origin\n" % (PROFILE_LIDAR + PROFILE_CAMERA))

# The bodies the sensors observe: each model's bounding-object boxes as model-frame axis-aligned boxes (centre,
# size l w h; the tilted boxes of a few models enclosed), from the Webots 838bb6f PROTOs and RoadSenseAutorickshaw.
MODEL_BOXES = {
    "BmwX5Simple": [((1.35, 0, 0.7), (4.5, 1.8, 1.6))],
    "CitroenCZeroSimple": [((1.25, 0, 0.5), (3.3, 1.4, 1.5))],
    "ToyotaPriusSimple": [((1.4, 0, 0.6), (4.2, 1.8, 1.4))],
    "LincolnMKZSimple": [((1.44, 0, 0.3), (4.928, 1.8, 0.995)), ((1.44, 0, 0.85), (2.0, 1.6, 0.5))],
    "RangeRoverSportSVRSimple": [((1.25, 0, 0.35), (4.515, 1.8, 0.89)), ((0.6, 0, 0.85), (2.7, 1.5, 0.75))],
    "TeslaModel3Simple": [((1.37, 0, 0.21), (4.5, 1.8, 0.7)), ((0.68, 0, 0.76), (2.9, 1.5, 0.7))],
    "MercedesBenzSprinterSimple": [((1.69, 0, 0.52), (6.6, 2.0, 1.0)), ((0.67, 0, 1.64), (4.5, 1.95, 1.3)),
                                   ((3.6, 0, 1.091), (2.811, 1.88, 2.415))],
    "BusSimple": [((1.462, 0, 1.35), (9.73, 2.64, 2.9))],
    "TruckSimple": [((5.4, 0, 2.0), (3.2, 2.8, 4.1)), ((1.55, 0, 0.35), (4.5, 2.8, 0.8))],
    "ScooterSimple": [((0.624, 0, 0.7), (1.95, 0.66, 1.52))],
    "MotorbikeSimple": [((0.79, 0, 0.55), (2.48, 1.1, 1.5))],
    "RoadSenseAutorickshaw": [((0.95, 0, 0.775), (2.8, 1.4, 1.55))],
}


def body_x_range(model):
    boxes = MODEL_BOXES[model]
    return min(c[0] - s[0] / 2 for c, s in boxes), max(c[0] + s[0] / 2 for c, s in boxes)


def profile_robot():
    radars = "".join(
        '      Radar { translation %g %g %g rotation 0 0 1 %g name "%s" maxRange %g horizontalFieldOfView %g '
        'verticalFieldOfView %g %s }\n' % (x, y, z, yaw, name, rng, hfov, vfov, RADAR)
        for name, (x, y, z, yaw, rng, hfov, vfov) in PROFILE_RADARS.items())
    return (
        '    Robot {\n      name "roadsense sensor profile"\n      controller "roadsense_sensor_recorder"\n'
        '      supervisor TRUE\n      children [\n' + radars +
        '      Lidar { translation %g %g %g name "profile lidar roof" horizontalResolution 1800 fieldOfView 6.283 '
        'verticalFieldOfView 0.5236 numberOfLayers 16 near 0.3 minRange 0.5 maxRange 100 noise 0.0003 '
        'defaultFrequency 10 maxFrequency 20 }\n' % PROFILE_LIDAR +
        '      Camera { translation %g %g %g name "profile camera 720p" fieldOfView 1.05 width 1280 height 720 '
        'near 0.5 far 300 }\n' % PROFILE_CAMERA +
        '      GPS { name "profile gps" }\n      InertialUnit { name "profile imu" }\n      ]\n    }\n')


def world(params, sumo_ms):
    wbt = scenarios.world(WORLD.read_text(), params, SUMO_PORT)
    wbt, n = re.subn(r"SumoInterface \{\n", "SumoInterface {\n  step %d\n" % sumo_ms, wbt, count=1)
    assert n == 1
    head = re.search(r"DEF WEBOTS_VEHICLE0 LincolnMKZ \{.*?\n  sensorsSlotCenter \[\n", wbt, flags=re.S)
    assert head
    wbt = wbt[:head.end()] + profile_robot() + wbt[head.end():]
    wbt = wbt.replace('IMPORTABLE EXTERNPROTO "../protos/RoadSensePothole.proto"',
                      'IMPORTABLE EXTERNPROTO "../protos/RoadSensePothole.proto"\n'
                      'IMPORTABLE EXTERNPROTO "../protos/RoadSenseAutorickshaw.proto"', 1)
    assert "RoadSenseAutorickshaw" in wbt
    return wbt


def prepare(run_dir, params, sumo_ms, webots):
    worlds, net = run_dir / "worlds", run_dir / "worlds" / "profile_net"
    net.mkdir(parents=True)
    (worlds / "profile.wbt").write_text(world(params, sumo_ms))
    (net / "sumo.net.xml").write_text(scenarios.network((NET_DIR / "sumo.net.xml").read_text(), params))
    (net / "sumo.rou.xml").write_text(scenarios.routes(params))
    (net / "sumo.sumocfg").write_text((NET_DIR / "sumo.sumocfg").read_text())
    (net / "plugin.py").write_text("import sys\nsys.path.insert(0, %r)\nfrom sensor_profile_plugin import "
                                   "SumoSupervisorPlugin  # noqa\n" % str(ROOT / "tools"))
    (worlds / "forest").symlink_to(WORLD.parent / "forest")
    (run_dir / "protos").symlink_to(ROOT / "simulation/webots/protos")
    controllers = run_dir / "controllers"
    controllers.mkdir()
    for name in ("roadsense_ego", "roadsense_sensor_recorder"):
        (controllers / name).symlink_to(ROOT / "simulation/webots/controllers" / name)
    (controllers / "radar_target_tracker").symlink_to(Path(webots).parents[1] / "projects/vehicles/controllers"
                                                       / "radar_target_tracker")
    (run_dir / "scenario.json").write_text(json.dumps(params, indent=1) + "\n")
    return worlds / "profile.wbt"


def _with(p, **event):
    p["event"].update(event)
    p.pop("configuration_id", None)
    return p


def _mixed_potholes(seed):
    p = scenarios.draw("mixed", seed)
    p["potholes"] = scenarios.draw("potholes", seed)["potholes"]
    return p


def _hs(builder, seed):
    import phase7a3_highspeed as hs  # Stage 9's builders (the previously failing diagnostic configurations)
    return hs.build(builder, seed)


# Bounded validation cases (approved: at most 12 simulation attempts), each a scenario-generator configuration.
CASES = {
    "normal": lambda s: scenarios.draw("normal", s),
    "stationary": lambda s: _hs("stationary", s),  # a lead brakes to a standstill and stays
    "truck_brake": lambda s: _with(scenarios.draw("sudden_brake", s), vehicle_type="truck"),
    "moto_cutin": lambda s: _with(_hs("gradual", s), vehicle_type="motorcycle"),
    "rickshaw_cutin": lambda s: _with(_hs("assertive", s), vehicle_type="autorickshaw"),
    "mixed_potholes": _mixed_potholes,  # dense mixed traffic: adjacent lanes, blind zone, occlusion, potholes
    "expressway": lambda s: _hs("expressway", s),  # traffic to 130 km/h: high-speed approaches from behind
}


def event_time_s(params):
    """When the ego reaches the event's trigger distance, at its set speed (an estimate for the run length)."""
    e = params.get("event")
    return None if not e else e["at_m"] / (params.get("ego_speed_kmh", 80) / 3.6) + e.get("settle_s", 3.0)


def run(a):
    run_dir = Path(a.out).resolve()
    if run_dir.exists():
        sys.exit("%s exists: a run is never overwritten" % run_dir)
    params = CASES[a.case](a.seed)
    duration = a.duration
    if a.after_event is not None and event_time_s(params) is not None:
        duration = round(event_time_s(params) + a.after_event, 1)
    params.update(duration_s=duration, run_id="profile-%s-%d-%d-%d-%s" % (a.case, a.seed, a.sumo_ms, a.radar_ms, a.sync))
    wbt = prepare(run_dir, params, a.sumo_ms, a.webots)
    rec = run_dir / "rec"
    rec.mkdir()
    env = dict(os.environ, ROADSENSE_SCENARIO=str(run_dir / "scenario.json"),
               ROADSENSE_GROUND_TRUTH=str(run_dir / "ground_truth.jsonl"), ROADSENSE_ROAD_PROFILE="highway_lht",
               ROADSENSE_EGO_SPEED_KMH=str(params.get("ego_speed_kmh", 80)), ROADSENSE_SENSOR_RECORD=str(rec),
               ROADSENSE_PROFILE_RADAR_MS=str(a.radar_ms), ROADSENSE_PROFILE_LIDAR_MS=str(a.lidar_ms),
               ROADSENSE_PROFILE_CAMERA_S=str(a.camera_s), ROADSENSE_PROFILE_SYNC=a.sync,
               ROADSENSE_PROFILE_RICKSHAW="0" if a.stock_models else "1",
               ROADSENSE_PROFILE_ALIGN_BODIES="0" if a.stock_models else "1",
               ROADSENSE_PROFILE_SUMO_LOG=str(run_dir / "sumo_state.jsonl"), PYTHONDONTWRITEBYTECODE="1")
    for key in ("ROADSENSE_EDGE_URL", "ROADSENSE_LIVE_PUBLISH", "ROADSENSE_FRAME_CAPTURE"):
        env.pop(key, None)
    if a.capture_frames:  # RoadSense's own sensor frames (roadsense.sensors.v1), exactly as its controller builds
        # them, for offline replay through the unchanged pipeline. Edge mode needs a URL: a closed local port, so
        # nothing is ever received; the publisher drops what it can't send and the simulation never waits.
        env.update(ROADSENSE_EDGE_URL="http://127.0.0.1:9/api/sensors", ROADSENSE_FRAME_CAPTURE=str(run_dir / "frames.jsonl"),
                   ROADSENSE_RUN_ID=params["run_id"][:64])
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    with open(run_dir / "webots.log", "w") as log:
        p = subprocess.Popen([a.webots, "--batch", "--mode=fast", "--no-rendering", "--stdout", "--stderr",
                              "--minimize", str(wbt)], env=env, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            rc = p.wait(timeout=a.timeout)
        except subprocess.TimeoutExpired:
            rc = None
        finally:  # only this run's process group: never other Webots instances
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            p.wait()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    info = {"webots": a.webots, "returncode": rc, "wall_s": round(time.monotonic() - t0, 2),
            "cpu_user_s": round(after.ru_utime - before.ru_utime, 2), "cpu_sys_s": round(after.ru_stime - before.ru_stime, 2),
            "max_rss_mb_children": round(after.ru_maxrss / 2 ** 20, 1), "sumo_ms": a.sumo_ms, "radar_ms": a.radar_ms, "lidar_ms": a.lidar_ms, "camera_s": a.camera_s,
            "sync": a.sync, "stock_models": a.stock_models, "case": a.case, "seed": a.seed, "duration_s": duration}
    (run_dir / "run.json").write_text(json.dumps(info, indent=1) + "\n")
    print(json.dumps(info))


# ---------------------------------------------------------------------------------------------------------------
# analysis (scoring only: ground truth never reaches RoadSense)

EGO_BOX = (-1.01, 3.89, 0.9)  # LincolnMKZ body: rear x, front x, half width (ego frame)
ZONES = {"left": (-6.0, 4.5, 1.0, 4.5), "right": (-6.0, 4.5, -4.5, -1.0)}  # blind-zone boxes beside the ego
ORIGINAL = ("profile radar front", "profile radar rear")


def _pct(v, q):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    i = int(k)
    return round(v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i), 4)


def _st(v):
    v = [x for x in v if x is not None]
    return {"n": len(v), "p50": _pct(v, 0.5), "p95": _pct(v, 0.95)} if v else {"n": 0}


def _abs(v):
    return [abs(x) for x in v if x is not None]


def _to_ego(ego, x, y):
    c, s = math.cos(ego[3]), math.sin(ego[3])
    dx, dy = x - ego[0], y - ego[1]
    return c * dx + s * dy, -s * dx + c * dy


def _rect_dist(px, py, x0, x1, y0, y1):
    return math.hypot(max(x0 - px, 0, px - x1), max(y0 - py, 0, py - y1))


def _body(ego, model_type, model):
    """Drawn body in the ego frame: centre, and its axis-aligned footprint (x0, x1, y0, y1) for a body about
    parallel to the ego (highway); from the model's bounding boxes, the object the sensors observe."""
    lo, hi = body_x_range(model_type)
    half = max(sz[1] for _, sz in MODEL_BOXES[model_type]) / 2
    yaw = model[3]
    corners = [(model[0] + math.cos(yaw) * bx - math.sin(yaw) * by, model[1] + math.sin(yaw) * bx + math.cos(yaw) * by)
               for bx in (lo, hi) for by in (-half, half)]
    pts = [_to_ego(ego, x, y) for x, y in corners]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"centre": _to_ego(ego, *[(a + b) / 2 for a, b in zip(corners[0], corners[3])]),
            "box": (min(xs), max(xs), min(ys), max(ys)), "length": hi - lo, "width": 2 * half}


def _clearance(x, y):
    """Distance from the ego's body to an ego-frame point (0 inside)."""
    return _rect_dist(x, y, EGO_BOX[0], EGO_BOX[1], -EGO_BOX[2], EGO_BOX[2])


def _box_clearance(b):
    x0, x1, y0, y1 = b
    gx = max(x0 - EGO_BOX[1], EGO_BOX[0] - x1, 0)
    gy = max(y0 - EGO_BOX[2], -EGO_BOX[2] - y1, 0)
    return math.hypot(gx, gy)


def _sumo_tracks(run_dir):
    tracks = {}
    for line in open(run_dir / "sumo_state.jsonl"):
        s = json.loads(line)
        for v in s["vehicles"]:
            if v["node"] is not None:
                tracks.setdefault(v["node"], []).append((s["sumo_t"], v))
    return tracks


def _sumo_at(track, t):
    """SUMO's state interpolated at SUMO time t (the common clock: SUMO time s is Webots time s)."""
    for (ta, a), (tb, b) in zip(track, track[1:]):
        if ta <= t <= tb and a["id"] == b["id"]:
            f = (t - ta) / (tb - ta) if tb > ta else 0.0
            out = dict(a)
            for k in ("x", "y", "speed"):
                out[k] = a[k] + f * (b[k] - a[k])
            out["yaw"] = a["yaw"] + f * math.remainder(b["yaw"] - a["yaw"], 2 * math.pi)
            out["cx"] = out["x"] - a["length"] / 2 * math.cos(out["yaw"])
            out["cy"] = out["y"] - a["length"] / 2 * math.sin(out["yaw"])
            return out
    return None


def analyse(run_dir, lidar_frames=None):
    import time as _time
    import numpy as np
    from roadsense.experimental import lidar as lg
    import radar_measurement_model as rmm
    run_dir = Path(run_dir)
    info = json.loads((run_dir / "run.json").read_text())
    lines = [json.loads(l) for l in open(run_dir / "rec/records.jsonl")]
    start, recs = lines[0], [r for r in lines[1:] if r["type"] == "sample"]
    tracks = _sumo_tracks(run_dir)
    step_s = info["sumo_ms"] / 1000.0
    lid = start["lidar"]
    cfg = lg.Config.webots("profile lidar roof", PROFILE_LIDAR + (0.0, 0.0, 0.0), lid["w"], lid["layers"], lid["fov"],
                           lid["min_range"], lid["max_range"], lg.ROOF16_ELEVATIONS)
    blob = (run_dir / "rec/lidar.bin").read_bytes() if (run_dir / "rec/lidar.bin").exists() else b""
    out = {"run": info, "sim_s": recs[-1]["t"], "rtf": round(recs[-1]["t"] / info["wall_s"], 2)}
    pose_err = {"lag_s": [], "along_m": [], "cross_m": [], "along_fast_m": [], "phase_on_step": [], "phase_between": []}
    geometry = {}
    radar = {n: {k: [] for k in ("lon_c", "lat_c", "lon_s", "lat_s", "lon_h", "lat_h", "clear_err", "rr_err",
                                 "rr_err_first", "rr_err_fast")} for n in PROFILE_RADARS}
    counts = {n: {"returns": 0, "self": 0, "matched": 0, "unmatched": 0, "first_suspect": 0} for n in PROFILE_RADARS}
    flags = {n: {} for n in PROFILE_RADARS}  # section 6 representation: flag combination x (vehicle, no vehicle)
    cover = {n: {} for n in PROFILE_RADARS}
    zone = {z: {s: [0, 0, 0, 0] for s in ("radar_original", "radar_all", "lidar")} for z in ZONES}  # tp fp fn tn
    lidar_rows, lidar_ms, payload = [], [], {}
    last_seen = {}
    ego_prev = None
    for r in recs:
        t, ego = r["t"], r["ego"]
        evx, evy, ewz = r["ego_v"]
        bodies = {}
        for g in r["gt"]:
            if g["model"][0] > 5000 or g["model"][1] > 5000:
                continue
            b = _body(ego, g["model_type"], g["model"])
            if math.hypot(*b["centre"]) > 120:
                continue
            truth = _sumo_at(tracks.get(g["node"], []), t)
            b.update(node=g["node"], type=g["model_type"], yaw_rel=g["model"][3] - ego[3], v=g["v"], sumo=truth, wrapper=_to_ego(ego, *g["wrapper"][:2]))
            bodies[g["node"]] = b
            if truth is not None and truth["speed"] > 3:
                dx, dy = g["wrapper"][0] - truth["cx"], g["wrapper"][1] - truth["cy"]
                along = dx * math.cos(truth["yaw"]) + dy * math.sin(truth["yaw"])
                cross = -dx * math.sin(truth["yaw"]) + dy * math.cos(truth["yaw"])
                pose_err["lag_s"].append(-along / truth["speed"])
                pose_err["along_m"].append(abs(along))
                pose_err["cross_m"].append(abs(cross))
                if truth["speed"] > 15:
                    pose_err["along_fast_m"].append(abs(along))
                on = abs(math.remainder(t, step_s)) < 1e-6
                pose_err["phase_on_step" if on else "phase_between"].append(math.hypot(along, cross))
                # simulator truth vs the drawn object: the wrapper is SUMO's vehicle centre (where sumo_supervisor
                # puts it); how far along the heading the body the sensors observe is from it, and the lengths
                wx, wy = b["wrapper"]
                h = g["model"][3] - ego[3]
                gm = geometry.setdefault(g["model_type"], {"sumo_type": truth["type"], "sumo_length": truth["length"],
                                                          "drawn_length": round(b["length"], 3), "centre_along": []})
                gm["centre_along"].append((b["centre"][0] - wx) * math.cos(h) + (b["centre"][1] - wy) * math.sin(h))
        if "radars" in r:
            for name, targets in r["radars"].items():
                mx, my, _mz, myaw, rng, hfov, _vfov = PROFILE_RADARS[name]
                hit = set()
                for d, az, speed, _power in targets:
                    counts[name]["returns"] += 1
                    a = myaw - az
                    px, py = mx + d * math.cos(a), my + d * math.sin(a)
                    best = min(bodies.values(), key=lambda b: _rect_dist(px, py, *b["box"]), default=None)
                    real = best is not None and _rect_dist(px, py, *best["box"]) <= 2.5
                    first = real and t - last_seen.get((name, best["node"]), -1e9) > 2.5 * info["radar_ms"] / 1000.0
                    m = rmm.describe(name.replace("profile radar corner", "exp radar corner").replace(
                        "profile radar", "radar"), d, az, speed, hfov, first_refresh=first)
                    key = "+".join(k for k in ("self_suspect", "range_clamped", "bearing_clamped", "velocity_unreliable")
                                   if m[k]) or "plain"
                    fl = flags[name].setdefault(key, [0, 0])
                    fl[0 if real else 1] += 1
                    if d <= 1.0001:
                        counts[name]["self"] += 1
                        continue
                    if not real:
                        counts[name]["unmatched"] += 1
                        continue
                    counts[name]["matched"] += 1
                    hit.add(best["node"])
                    R = radar[name]
                    R["lon_c"].append(px - best["centre"][0])
                    R["lat_c"].append(py - best["centre"][1])
                    R["lon_h"].append(px - best["wrapper"][0])
                    R["lat_h"].append(py - best["wrapper"][1])
                    if best["sumo"] is not None:
                        sx, sy = _to_ego(ego, best["sumo"]["cx"], best["sumo"]["cy"])
                        R["lon_s"].append(px - sx)
                        R["lat_s"].append(py - sy)
                    # clearance: what the return implies (a point) minus the true gap to the body; > 0 = optimistic
                    R["clear_err"].append(_clearance(px, py) - _box_clearance(best["box"]))
                    # true range rate of the measured point: relative velocity (ego frame, with the ego's yaw rate)
                    vx, vy = _to_ego((0, 0, 0, ego[3]), best["v"][0], best["v"][1])
                    ex, ey = _to_ego((0, 0, 0, ego[3]), evx, evy)
                    rx, ry = px - mx, py - my
                    rvx, rvy = vx - (ex - ewz * py), vy - (ey + ewz * px)
                    truth_rr = (rvx * rx + rvy * ry) / max(1e-6, math.hypot(rx, ry))
                    if speed is not None and math.isfinite(speed):
                        err = speed - truth_rr
                        (R["rr_err_first"] if first else R["rr_err"]).append(err)
                        if first and abs(err) > 3:
                            counts[name]["first_suspect"] += 1
                        if abs(truth_rr) > 10:
                            R["rr_err_fast"].append(err)
                    last_seen[(name, best["node"])] = t
                for node, b in bodies.items():  # coverage: same-direction traffic, centre in field of view and range
                    if abs(math.remainder(b["yaw_rel"], 2 * math.pi)) > 0.5:
                        continue  # (the opposite carriageway is behind the median's guardrail; not occlusion-aware)
                    cx, cy = b["centre"][0] - mx, b["centre"][1] - my
                    if math.hypot(cx, cy) > rng or abs(math.remainder(math.atan2(cy, cx) - myaw, 2 * math.pi)) > hfov / 2:
                        continue
                    band = "0-30" if math.hypot(cx, cy) < 30 else "30-80" if math.hypot(cx, cy) < 80 else "80+"
                    cv = cover[name].setdefault("%s %s" % (b["type"], band), [0, 0])
                    cv[0] += 1
                    cv[1] += node in hit
        if "lidar" in r:
            raw = np.frombuffer(blob, "<f4", r["lidar"]["n"], r["lidar"]["offset"]).reshape(lid["layers"], lid["w"])
            w0 = _time.perf_counter()
            res = lg.process(cfg, raw, EGO_BOX)
            lidar_ms.append(1000 * (_time.perf_counter() - w0))
            if not payload:
                pts = lg.points(cfg, raw)[0]
                payload = {"raw_points_xyz_f32": int(12 * len(pts)), "range_image_f32": int(raw.size * 4),
                           "frame_f32_mask": len(lg.encode_frame(cfg, raw, 0, t)),
                           "frame_f32_mask_zlib": len(lg.encode_frame(cfg, raw, 0, t, compress=True)),
                           "frame_u16_cm_zlib": len(lg.encode_frame(lg.Config.webots(
                               "profile lidar roof", cfg.mount, cfg.width, cfg.layers, cfg.fov, cfg.min_range,
                               cfg.max_range, cfg.elevations, encoding=lg.UINT16, scale=0.01), raw, 0, t, compress=True)),
                           "objects": len(lg.encode_objects(cfg, 0, t, res["clusters"])),
                           "config_once": len(cfg.encode())}
            payload.setdefault("objects_all", []).append(len(lg.encode_objects(cfg, 0, t, res["clusters"])))
            cl = res["clusters"]
            for node, b in bodies.items():
                x0, x1, y0, y1 = b["box"]
                if math.hypot(*b["centre"]) > 80:
                    continue
                mine = [c for c in cl if c["x"][0] <= x1 + 0.3 and c["x"][1] >= x0 - 0.3 and c["y"][0] <= y1 + 0.3
                        and c["y"][1] >= y0 - 0.3]
                row = {"type": b["type"], "range": math.hypot(*b["centre"]), "detected": bool(mine),
                       "fragments": len(mine), "speed": math.hypot(*b["v"][:2])}
                if mine:
                    ox0, ox1 = min(c["x"][0] for c in mine), max(c["x"][1] for c in mine)
                    oy0, oy1 = min(c["y"][0] for c in mine), max(c["y"][1] for c in mine)
                    if x0 > EGO_BOX[1]:
                        row["near_face_err"] = ox0 - x0  # > 0: seen farther than the body's rear face (optimistic)
                    elif x1 < EGO_BOX[0]:
                        row["near_face_err"] = x1 - ox1
                    if x0 < EGO_BOX[1] + 2 and x1 > EGO_BOX[0] - 2 and abs(b["centre"][1]) > 1.5:
                        row["near_side_err"] = (oy0 - y0) if b["centre"][1] > 0 else (y1 - oy1)
                    row["clear_err"] = _box_clearance((ox0, ox1, oy0, oy1)) - _box_clearance(b["box"])
                lidar_rows.append(row)
            if "radars" in r:
                for zname, zb in ZONES.items():
                    truth = any(b["box"][0] <= zb[1] and b["box"][1] >= zb[0] and b["box"][2] <= zb[3]
                                and b["box"][3] >= zb[2] for b in bodies.values())

                    def radar_in(names):
                        for name in names:
                            mx, my, _mz, myaw = PROFILE_RADARS[name][:4]
                            hfov = PROFILE_RADARS[name][5]
                            for d, az, sp, _p in r["radars"][name]:
                                if not rmm.describe(name.replace("profile radar corner", "exp radar corner").replace(
                                        "profile radar", "radar"), d, az, sp, hfov)["self_suspect"]:
                                    px, py = mx + d * math.cos(myaw - az), my + d * math.sin(myaw - az)
                                    if zb[0] <= px <= zb[1] and zb[2] <= py <= zb[3]:
                                        return True
                        return False
                    for sensor, seen in (("radar_original", radar_in(ORIGINAL)), ("radar_all", radar_in(PROFILE_RADARS)),
                                         ("lidar", lg.zone_occupied(cl, zb))):
                        zone[zname][sensor][(0 if seen else 2) if truth else (1 if seen else 3)] += 1
    out["pose_vs_sumo"] = {k: _st(v) for k, v in pose_err.items()}
    out["simulator_truth_vs_drawn_body"] = {k: dict(v, centre_along=_st(v["centre_along"])) for k, v in geometry.items()}
    out["radar"] = {n: dict(counts[n], **{k: _st(v if k == "clear_err" else _abs(v)) for k, v in radar[n].items()},
                            bias_lon_c=_pct(radar[n]["lon_c"], 0.5), bias_lat_c=_pct(radar[n]["lat_c"], 0.5),
                            coverage={k: round(v[1] / v[0], 3) for k, v in sorted(cover[n].items()) if v[0] >= 20},
                            flags_vehicle_vs_none=flags[n])
                    for n in PROFILE_RADARS}
    by = {}
    for row in lidar_rows:
        band = "0-20" if row["range"] < 20 else "20-40" if row["range"] < 40 else "40-80"
        by.setdefault("%s %s" % (row["type"], band), []).append(row)
    out["lidar"] = {k: {"n": len(v), "detected": round(sum(x["detected"] for x in v) / len(v), 3),
                        "fragmented": round(sum(x["fragments"] > 1 for x in v) / len(v), 3),
                        "near_face_err": _st([x.get("near_face_err") for x in v]),
                        "near_side_err": _st([x.get("near_side_err") for x in v]),
                        "clear_err": _st([x.get("clear_err") for x in v])}
                    for k, v in sorted(by.items()) if len(v) >= 10}
    out["lidar_process_ms"] = _st(lidar_ms)
    if payload:
        objs = payload.pop("objects_all")
        payload["objects_p50"], payload["objects_max"] = _pct(objs, 0.5), max(objs)
    out["lidar_payload_bytes_per_frame"] = payload
    out["blind_zones"] = {z: {s: {"recall": round(c[0] / (c[0] + c[2]), 3) if c[0] + c[2] else None,
                                  "false_alarm": round(c[1] / (c[1] + c[3]), 3) if c[1] + c[3] else None,
                                  "occupied_frames": c[0] + c[2], "frames": sum(c)}
                              for s, c in v.items()} for z, v in zone.items()}
    return out



def lab_lidar(run_dir, sensor="exp lidar roof"):
    """The W4 calibration-lab lidar cases (static targets) through roadsense.experimental.lidar: near face from all clusters on the
    body (a fragmented truck is still one obstacle at its nearest point) and from the single cluster the W4 scoring
    picked (the first one on the footprint, which for a truck was often the cab)."""
    import numpy as np
    from roadsense.experimental import lidar as lg
    import webots_calibration as wc
    cases, samples, _ends = wc.load(run_dir)
    run_dir = Path(run_dir)
    blob = (run_dir / (sensor.replace(" ", "_") + ".bin")).read_bytes()
    rows = []
    for cid, case in cases.items():
        for s in samples[cid][1:]:
            meta = s["sensors"].get(sensor)
            if not isinstance(meta, dict):
                continue
            cfg = lg.Config.webots(sensor, wc.LIDARS[sensor][:3] + (0.0, 0.0, wc.LIDARS[sensor][3]), meta["w"],
                                   meta["layers"], meta["fov"], 0.5, meta["max"], lg.ROOF16_ELEVATIONS)
            raw = np.frombuffer(blob, "<f4", meta["n"], meta["offset"]).reshape(meta["layers"], meta["w"])
            cl = lg.process(cfg, raw, EGO_BOX)["clusters"]
            ego = s["gt"]["ego"]
            for tname in wc.target_kinds(case):
                corners = [wc.rot_t(wc.mat(ego), wc.sub(p, ego[:3])) for p in wc.body_points(tname, s["gt"]["targets"][tname]["model"])]
                x0, x1 = min(p[0] for p in corners), max(p[0] for p in corners)
                y0, y1 = min(p[1] for p in corners), max(p[1] for p in corners)
                mine = [c for c in cl if c["x"][0] <= x1 + 0.3 and c["x"][1] >= x0 - 0.3 and c["y"][0] <= y1 + 0.3
                        and c["y"][1] >= y0 - 0.3]
                row = {"case": cid, "group": case["spec"]["group"], "target": tname, "range": round((x0 + x1) / 2, 1),
                       "lat": round((y0 + y1) / 2, 1), "clusters_on_body": len(mine)}
                if mine and x0 > EGO_BOX[1]:
                    row["near_face_err_all"] = round(min(c["x"][0] for c in mine) - x0, 3)
                    row["near_face_err_first_cluster"] = round(mine[0]["x"][0] - x0, 3)
                    row["unseen_vertical_gap"] = round(min(c["unseen_vertical_gap"] for c in mine), 2)
                    # does the reported interval contain the true near face (horizontal range from the sensor)?
                    lo = min(c["near_range_interval"][0] for c in mine)
                    hi = min(c["near_range_interval"][1] for c in mine)
                    true_near = math.hypot(x0 - cfg.mount[0], max(y0, min(y1, 0.0)) - cfg.mount[1])
                    row["interval_m"] = [round(lo, 2), round(hi, 2), round(true_near, 2)]
                    row["interval_contains_truth"] = lo - 0.15 <= true_near <= hi + 0.15
                rows.append(row)
    return rows


def sync_check(run_dir):
    """W10 verification of a sync mode (scoring only), from one run's records and SUMO log:

    sensor_vs_display  front-radar range residual against the drawn body at the radar's own timestamp, and with the
                       truth shifted by -3..+3 records: the minimum must be at 0 (sensors see the displayed pose then)
    display_vs_sumo    displayed (wrapper) position minus SUMO's state at the same time, along track, split by SUMO's
                       acceleration: a causal extrapolation errs at braking onsets; one using future states would not
    velocity           the node velocity Webots reports minus the finite difference of displayed positions
    heading            displayed yaw minus the direction of motion (above 3 m/s)
    accel / lateral    accelerations and lateral speeds of the displayed bodies (second differences, 0.1 s)
    """
    run_dir = Path(run_dir)
    info = json.loads((run_dir / "run.json").read_text())
    recs = [json.loads(l) for l in open(run_dir / "rec/records.jsonl")][1:]
    recs = [r for r in recs if r["type"] == "sample"]
    tracks = _sumo_tracks(run_dir)
    dt = recs[1]["t"] - recs[0]["t"]
    by_t = {round(r["t"], 3): r for r in recs}
    hist = {}
    for r in recs:
        for g in r["gt"]:
            if g["wrapper"][0] < 9000:
                hist.setdefault(g["node"], {})[round(r["t"], 3)] = g
    shifts = {k: [] for k in range(-3, 4)}
    x, y, z, yaw = PROFILE_RADARS["profile radar front"][:4]
    for r in recs:
        if "radars" not in r:
            continue
        for d, az, _v, _p in r["radars"]["profile radar front"]:
            a = yaw - az
            px, py = x + d * math.cos(a), y + d * math.sin(a)
            for k in shifts:
                other = by_t.get(round(r["t"] + k * dt, 3))
                if other is None:
                    continue
                best = None
                for g in r["gt"]:
                    if g["model_type"] not in MODEL_BOXES or g["wrapper"][0] > 9000:
                        continue
                    g2 = hist.get(g["node"], {}).get(round(other["t"], 3))
                    if g2 is None:
                        continue
                    b = _body(r["ego"], g["model_type"], g2["model"])  # body at t + k dt, ego frame at t
                    cx, cy = b["centre"]
                    rr = math.hypot(cx - x, cy - y) - d
                    if math.hypot(cx - px, cy - py) < 4 and (best is None or abs(rr) < abs(best)):
                        best = rr
                if best is not None:
                    shifts[k].append(best)
    along = {"steady": [], "braking": [], "onset": []}
    vel, head, acc, lat_v = [], [], [], []
    for node, h in hist.items():
        ts = sorted(h)
        for t0, t1, t2 in zip(ts, ts[1:], ts[2:]):
            if t2 - t0 > 2 * dt + 1e-6:
                continue
            p0, p1, p2 = (h[t]["wrapper"] for t in (t0, t1, t2))
            v1 = ((p2[0] - p0[0]) / (2 * dt), (p2[1] - p0[1]) / (2 * dt))
            vel.append(math.hypot(h[t1]["v"][0] - v1[0], h[t1]["v"][1] - v1[1]))
            sp = math.hypot(*v1)
            if sp > 3:
                head.append(abs(math.remainder(p1[3] - math.atan2(v1[1], v1[0]), 2 * math.pi)))
            a = ((p2[0] - 2 * p1[0] + p0[0]) / dt ** 2, (p2[1] - 2 * p1[1] + p0[1]) / dt ** 2)
            c, s_ = math.cos(p1[3]), math.sin(p1[3])
            acc.append(a[0] * c + a[1] * s_)
            lat_v.append(abs(-v1[0] * s_ + v1[1] * c))
        for t in ts:
            g = h[t]
            st = _sumo_at(tracks.get(node, []), t)
            if st is None:
                continue
            cx, cy = g["wrapper"][0], g["wrapper"][1]
            e = (cx - st["cx"]) * math.cos(st["yaw"]) + (cy - st["cy"]) * math.sin(st["yaw"])
            prev = _sumo_at(tracks.get(node, []), t - 3 * info["sumo_ms"] / 1000.0)
            onset = prev is not None and prev["accel"] > -0.5 and st["accel"] < -2.0
            along["onset" if onset else "braking" if st["accel"] < -2.0 else "steady"].append(abs(e))
    rms = lambda v: round(math.sqrt(sum(x * x for x in v) / len(v)), 4) if v else None  # noqa: E731
    return {"sync": info["sync"], "sumo_ms": info["sumo_ms"],
            "sensor_vs_display_range_rms_m": {k: rms(v) for k, v in shifts.items()},
            "display_vs_sumo_along_m": {k: dict(_st(v), p99=_pct(v, 0.99)) for k, v in along.items()},
            "velocity_minus_displacement_mps": dict(_st(vel), p99=_pct(vel, 0.99)),
            "heading_minus_motion_rad": dict(_st(head), p99=_pct(head, 0.99)),
            "longitudinal_accel_mps2": {"p01": _pct(acc, 0.01), "p99": _pct(acc, 0.99), "min": min(acc, default=None),
                                        "max": max(acc, default=None)},
            "lateral_speed_mps": {"p99": _pct(lat_v, 0.99), "max": max(lat_v, default=None)}}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--sumo-ms", type=int, default=100)
    r.add_argument("--radar-ms", type=int, default=100)
    r.add_argument("--sync", choices=("stock", "current", "aligned", "smooth"), default="stock")
    r.add_argument("--stock-models", action="store_true", help="autorickshaws drawn as cars, bodies not aligned")
    r.add_argument("--webots", default=INSTALLED)
    r.add_argument("--case", choices=sorted(CASES), default="normal")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--duration", type=float, default=60.0)
    r.add_argument("--after-event", type=float, help="end this many seconds after the estimated event time")
    r.add_argument("--capture-frames", action="store_true", help="also capture RoadSense's own sensor frames")
    r.add_argument("--camera-s", type=float, default=0.0)
    r.add_argument("--lidar-ms", type=int, default=100, help="0: lidar off")
    r.add_argument("--timeout", type=float, default=900.0)
    an = sub.add_parser("analyse")
    an.add_argument("runs", nargs="+")
    an.add_argument("--out")
    lab = sub.add_parser("lab-lidar")
    lab.add_argument("run")
    sc = sub.add_parser("sync-check")
    sc.add_argument("runs", nargs="+")
    sc.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a)
    elif a.cmd == "sync-check":
        result = {Path(d).name: sync_check(d) for d in a.runs}
        if a.out:
            Path(a.out).write_text(json.dumps(result, indent=1) + "\n")
        print(json.dumps(result, indent=1))
    elif a.cmd == "lab-lidar":
        print(json.dumps(lab_lidar(a.run)))
    else:
        result = {Path(d).name: analyse(d) for d in a.runs}
        text = json.dumps(result, indent=1)
        if a.out:
            Path(a.out).write_text(text + "\n")
        print(text)


if __name__ == "__main__":
    main()
