"""Webots sensor calibration laboratory (Stages W2-W7): bounded test plans, runs and analysis.

    plan     write a calibration plan (JSON) for roadsense_calib in roadsense_calibration.wbt
    run      run Webots on the plan in batch mode, recording CPU time, peak memory and GPU utilisation
    analyse  score raw sensor output and the RoadSense transform against supervisor ground truth

Ground truth only scores. Nothing here feeds RoadSense inference, and the highway world is not touched.
Diagnostic tool: development evidence, not acceptance evidence.
"""
import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
WORLD = ROOT / "simulation/webots/worlds/roadsense_calibration.wbt"
# ROADSENSE_WEBOTS: another Webots executable (e.g. a W8 patched copy); the installed one by default
WEBOTS = os.environ.get("ROADSENSE_WEBOTS", "/Applications/Webots.app/Contents/MacOS/webots")

# ---------------------------------------------------------------------------------------------------------------
# Geometry, from the installed Webots assets (838bb6f) and roadsense_calibration.wbt. Model frame: x forward,
# y left, z up, origin = the model node's origin (a car's rear axle). Boxes: (centre xyz, size lwh).
# wrapper_dx: the model's offset behind SUMO's wrapper Solid; sumo_length: SUMO's intended vehicle length.
KINDS = {
    "T_BOX_C": dict(boxes=[((0.0, 0.0, 0.0), (4.5, 1.8, 1.5))], wrapper_dx=0.0, sumo_length=4.5, label="box (origin at centre)"),
    "T_BOX_O": dict(boxes=[((1.4, 0.0, 0.433), (4.5, 1.8, 1.5))], wrapper_dx=0.0, sumo_length=4.5, label="box (origin 1.4 m behind centre)"),
    "T_CAR": dict(boxes=[((1.4, 0.0, 0.6), (4.2, 1.8, 1.4))], wrapper_dx=-2.85, sumo_length=5.0, label="car (ToyotaPriusSimple)"),
    "T_CAR2": dict(boxes=[((1.35, 0.0, 0.7), (4.5, 1.8, 1.6))], wrapper_dx=-2.85, sumo_length=5.0, label="car (BmwX5Simple)"),
    "T_TRUCK": dict(boxes=[((5.4, 0.0, 2.0), (3.2, 2.8, 4.1)), ((1.55, 0.0, 0.35), (4.5, 2.8, 0.8))], wrapper_dx=-5.2,
                    sumo_length=8.0, label="truck (TruckSimple)"),
    "T_MOTO": dict(boxes=[((0.79, 0.0, 0.55), (2.48, 1.1, 1.5))], wrapper_dx=-1.7, sumo_length=3.0, label="motorcycle (MotorbikeSimple + driver)"),
    "T_RICK": dict(boxes=[((1.4, 0.0, 0.6), (4.2, 1.8, 1.4))], wrapper_dx=-2.85, sumo_length=2.8, label="autorickshaw (SUMO 2.8 m passenger drawn as ToyotaPriusSimple)"),
}
EGO_FRONT_M, EGO_REAR_M, EGO_HALF_WIDTH_M = 3.89, -1.01, 0.9  # LincolnMKZ LOWER_BOX: x 1.44 +- 2.45, y +- 0.9
# radar name -> (ego-frame mount x, y, z, yaw, max range, horizontal FOV, vertical FOV)
RADARS = {
    "radar front": (4.054, 0.0, 0.142, 0.0, 150, 2.6, 0.07854),
    "diag radar front": (4.054, 0.0, 0.142, 0.0, 150, 2.6, 0.07854),
    "diag radar front noocc": (4.054, 0.0, 0.142, 0.0, 150, 2.6, 0.07854),
    # mounted at a car origin's height (0.317 m above the ground with the ego origin 0.313 m up): no elevation leak
    "diag radar front low": (4.054, 0.0, 0.004, 0.0, 150, 2.6, 0.07854),
    "exp radar front vfov": (4.054, 0.0, 0.142, 0.0, 150, 2.6, 0.25),
    "radar rear": (-1.16, 0.0, 0.33, math.pi, 100, 2.6, 0.07854),
    "diag radar rear": (-1.16, 0.0, 0.33, math.pi, 100, 2.6, 0.07854),
    "exp radar corner fl": (3.85, 0.85, 0.142, 0.7854, 80, 1.6, 0.25),
    "exp radar corner fr": (3.85, -0.85, 0.142, -0.7854, 80, 1.6, 0.25),
    "exp radar corner rl": (-1.1, 0.85, 0.33, 2.3562, 80, 1.6, 0.25),
    "exp radar corner rr": (-1.1, -0.85, 0.33, -2.3562, 80, 1.6, 0.25),
}
# lidar name -> ego-frame mount (x, y, z, yaw); the Lidar node's frame looks along +x
LIDARS = {
    "exp lidar roof": (1.11, 0.0, 1.51, 0.0),
    "ibeo 1": (3.944 - 0.225167, 0.71, 0.142, 0.785398),
    "ibeo 2": (3.944 - 0.225, -0.71, 0.142, -0.819204),
    "ibeo 3": (-1.06 + 0.164545, -0.745, 0.33, math.pi + 0.721593),
    "ibeo 4": (-1.06 + 0.189857, 0.75851, 0.33, math.pi - 0.7915953071795858),
}
ALL_RADARS = list(RADARS)
LANE_M = 3.75


def model_bounds(kind):
    """Model-frame (xmin, xmax, ymin, ymax, zmin, zmax) of a kind's boxes."""
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for c, s in KINDS[kind]["boxes"]:
        for i in range(3):
            lo[i], hi[i] = min(lo[i], c[i] - s[i] / 2), max(hi[i], c[i] + s[i] / 2)
    return lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]


def wrapper_lon_for_gap(kind, gap, ahead=True):
    """Wrapper longitudinal offset (ego frame, yaw 0) that leaves `gap` m between the ego's bumper and the body."""
    xmin, xmax = model_bounds(kind)[:2]
    dx = KINDS[kind]["wrapper_dx"]
    return EGO_FRONT_M + gap - (dx + xmin) if ahead else EGO_REAR_M - gap - (dx + xmax)


def wrapper_lon_for_centre(kind, centre_lon):
    xmin, xmax = model_bounds(kind)[:2]
    return centre_lon - (KINDS[kind]["wrapper_dx"] + (xmin + xmax) / 2)


# ---------------------------------------------------------------------------------------------------------------
# plan

def T(name, lon, lat=0.0, yaw=0.0, v_lon=0.0, v_lat=0.0):
    return {"name": name, "lon": round(lon, 3), "lat": lat, "yaw_deg": yaw, "v_lon": v_lon, "v_lat": v_lat}


def build_plan():
    cases = []

    def add(group, ego_speed, targets, duration=1.6, **extra):
        cid = "%s-%03d" % (group, sum(1 for c in cases if c["group"] == group))
        case = {"id": cid, "group": group, "ego_speed": ego_speed, "duration": duration, "targets": targets,
                "devices": list(ALL_RADARS), "radar_period_ms": 200}
        case.update(extra)
        cases.append(case)

    kinds = list(KINDS)
    # W2/W3 static geometry: ego stopped, one target, ranges to the radar limits and three lateral offsets
    for kind in kinds:
        for gap in (3, 8, 20, 50, 90, 130):
            for lat in (0.0, LANE_M, -LANE_M):
                add("static", 0, [T(kind, wrapper_lon_for_gap(kind, gap), lat)])
    for gap in (138, 142, 146):  # the front radar's 150 m edge, measured to the body centre
        add("static", 0, [T("T_CAR", wrapper_lon_for_gap("T_CAR", gap), 0.0)])
    for lat in (2 * LANE_M,):
        for gap in (8, 20, 50):
            add("static", 0, [T("T_CAR", wrapper_lon_for_gap("T_CAR", gap), lat)])
    for kind in ("T_CAR", "T_MOTO", "T_TRUCK", "T_BOX_C"):
        for gap in (3, 15, 60, 92):
            for lat in (0.0, LANE_M):
                add("static_rear", 0, [T(kind, wrapper_lon_for_gap(kind, gap, ahead=False), lat)])
    # orientation
    for kind in ("T_CAR", "T_BOX_O", "T_BOX_C"):
        for lat in (0.0, LANE_M):
            for yaw in (0, 20, 45, 90, 180):
                add("orientation", 0, [T(kind, wrapper_lon_for_centre(kind, EGO_FRONT_M + 15), lat, yaw)])
    # both stopped, longer: the Stage 12 stationary losses
    for kind in ("T_CAR", "T_TRUCK", "T_MOTO"):
        for gap in (0.3, 1.0, 2, 5, 10, 20):
            add("both_stopped", 0, [T(kind, wrapper_lon_for_gap(kind, gap), 0.0)], duration=4.0)
    # relative motion with the ego stopped
    add("rel_speed", 0, [T("T_CAR", wrapper_lon_for_gap("T_CAR", 60), 0.0, v_lon=-10)], duration=4.0)
    add("rel_speed", 0, [T("T_CAR", wrapper_lon_for_gap("T_CAR", 60), LANE_M, v_lon=-10)], duration=4.0)
    add("rel_speed", 0, [T("T_CAR", wrapper_lon_for_gap("T_CAR", 10), 0.0, v_lon=10)], duration=4.0)
    add("rel_speed", 0, [T("T_MOTO", wrapper_lon_for_gap("T_MOTO", 60), LANE_M, v_lon=-10)], duration=4.0)
    add("rel_speed", 0, [T("T_CAR", wrapper_lon_for_gap("T_CAR", 15), LANE_M, v_lat=-1.0)], duration=3.0)
    add("rel_speed", 0, [T("T_CAR", wrapper_lon_for_gap("T_CAR", 10, ahead=False), LANE_M, v_lon=8)], duration=3.0)
    # occlusion and several targets
    gc = wrapper_lon_for_gap
    add("occlusion", 0, [T("T_CAR", gc("T_CAR", 10), 0.0), T("T_TRUCK", gc("T_TRUCK", 30), 0.0)], duration=2.0)
    add("occlusion", 0, [T("T_CAR", gc("T_CAR", 10), 0.0), T("T_CAR2", gc("T_CAR2", 25), 0.0)], duration=2.0)
    add("occlusion", 0, [T("T_CAR", gc("T_CAR", 10), 0.0), T("T_MOTO", gc("T_MOTO", 25), 1.0)], duration=2.0)
    add("occlusion", 0, [T("T_CAR", gc("T_CAR", 10), 0.0), T("T_MOTO", gc("T_MOTO", 25), 0.0)], duration=2.0)
    add("occlusion", 0, [T("T_TRUCK", gc("T_TRUCK", 10), LANE_M), T("T_CAR", gc("T_CAR", 30), LANE_M)], duration=2.0)
    add("multi", 0, [T("T_CAR", gc("T_CAR", 30), 0.0), T("T_CAR2", gc("T_CAR2", 30), LANE_M)], duration=2.0)
    add("multi", 0, [T("T_CAR", gc("T_CAR", 30), 0.0), T("T_CAR2", gc("T_CAR2", 30), -LANE_M)], duration=2.0)
    add("multi", 0, [T("T_CAR", gc("T_CAR", 20), 0.0), T("T_MOTO", gc("T_MOTO", 20), 2.2)], duration=2.0)
    add("multi", 0, [T("T_CAR", gc("T_CAR", 20), LANE_M), T("T_CAR2", gc("T_CAR2", 20), 2 * LANE_M),
                     T("T_MOTO", gc("T_MOTO", 20), 0.0)], duration=2.0)
    # alongside: the blind zones, body centre positions relative to the ego origin
    for kind, lat in (("T_CAR", LANE_M), ("T_CAR", -LANE_M), ("T_MOTO", 2.4), ("T_TRUCK", LANE_M)):
        for centre in (-9, -6, -3, 0, 1.5, 3, 6, 9):
            add("alongside", 0, [T(kind, wrapper_lon_for_centre(kind, centre), lat)])
    # frequency: radar sampling period x target update (10 ms smooth, 200 ms SUMO-like)
    for period in (50, 100, 200):
        for upd in (10, 200):
            add("frequency", 0, [T("T_CAR", gc("T_CAR", 60), LANE_M, v_lon=-10)], duration=3.0,
                radar_period_ms=period, target_update_ms=upd)
    for period in (50, 100):  # SUMO-like steps plus SumoSupervisor's setVelocity on the physics-less wrapper
        add("frequency_setvel", 0, [T("T_CAR", gc("T_CAR", 60), LANE_M, v_lon=-10)], duration=3.0,
            radar_period_ms=period, target_update_ms=200, set_velocity=True)
    # moving ego (15 m/s): co-moving targets (relative geometry of the static cases), stopped targets approached,
    # lateral motion, oncoming
    for kind in ("T_CAR", "T_MOTO", "T_TRUCK", "T_BOX_O"):
        for gap in (8, 30, 90):
            for lat in (0.0, LANE_M):
                add("moving_ego", 15, [T(kind, gc(kind, gap), lat)])
    for kind in ("T_CAR", "T_MOTO"):
        for gap in (8, 30):
            for lat in (0.0, LANE_M):
                add("moving_ego_rear", 15, [T(kind, gc(kind, gap, ahead=False), lat)])
    for kind in ("T_CAR", "T_MOTO", "T_TRUCK"):
        for lat in (0.0, LANE_M):  # a stopped vehicle approached at 15 m/s: 80 m gap, 4 s, ends 20 m away
            add("stopped_target", 15, [T(kind, gc(kind, 80), lat, v_lon=-15)], duration=4.0)
    add("lateral", 15, [T("T_CAR", gc("T_CAR", 15), LANE_M, v_lat=-1.0)], duration=3.0)
    add("lateral", 15, [T("T_CAR", gc("T_CAR", 15), -LANE_M, v_lat=1.0)], duration=3.0)
    add("oncoming", 15, [T("T_CAR", gc("T_CAR", 120), 2 * LANE_M, yaw=180, v_lon=-30)], duration=3.0)
    for period in (50, 100, 200):
        for upd in (10, 200):
            add("frequency_moving", 15, [T("T_CAR", gc("T_CAR", 20), LANE_M, v_lat=-0.7)], duration=3.0,
                radar_period_ms=period, target_update_ms=upd)
    return {"name": "w-radar", "cases": cases}


def lidar_plan():
    """Lidars (existing IbeoLux x4 and the experimental roof lidar) on a bounded subset, plus a convention case."""
    cases, gc, lid = [], wrapper_lon_for_gap, ["exp lidar roof", "ibeo 1", "ibeo 2", "ibeo 3", "ibeo 4"]

    def add(group, ego_speed, targets, duration=0.5, **extra):
        cid = "%s-%03d" % (group, sum(1 for c in cases if c["group"] == group))
        case = {"id": cid, "group": group, "ego_speed": ego_speed, "duration": duration, "targets": targets,
                "devices": ["radar front", "radar rear"] + lid, "radar_period_ms": 100, "lidar_period_ms": 100}
        case.update(extra)
        cases.append(case)

    add("lidar_convention", 0, [T("T_BOX_C", gc("T_BOX_C", 10), LANE_M)], duration=0.2, point_cloud=True)
    for kind in ("T_CAR", "T_MOTO", "T_TRUCK"):
        for gap in (3, 8, 20, 50, 90):
            for lat in (0.0, LANE_M):
                add("lidar_static", 0, [T(kind, gc(kind, gap), lat)])
    for kind in ("T_CAR", "T_MOTO"):
        for gap in (3, 15, 40):
            add("lidar_static_rear", 0, [T(kind, gc(kind, gap, ahead=False), LANE_M)])
    for kind, lat in (("T_CAR", LANE_M), ("T_CAR", -LANE_M), ("T_MOTO", 2.4), ("T_TRUCK", LANE_M)):
        for centre in (-9, -6, -3, 0, 1.5, 3, 6, 9):
            add("lidar_alongside", 0, [T(kind, wrapper_lon_for_centre(kind, centre), lat)])
    add("lidar_occlusion", 0, [T("T_CAR", gc("T_CAR", 10), 0.0), T("T_MOTO", gc("T_MOTO", 25), 1.0)])
    add("lidar_occlusion", 0, [T("T_CAR", gc("T_CAR", 10), 0.0), T("T_TRUCK", gc("T_TRUCK", 30), 0.0)])
    add("lidar_multi", 0, [T("T_CAR", gc("T_CAR", 20), 0.0), T("T_MOTO", gc("T_MOTO", 20), 2.2)])
    add("lidar_multi", 0, [T("T_CAR", gc("T_CAR", 20), LANE_M), T("T_CAR2", gc("T_CAR2", 20), 2 * LANE_M),
                           T("T_MOTO", gc("T_MOTO", 20), 0.0)])
    add("lidar_lateral", 0, [T("T_CAR", gc("T_CAR", 15), LANE_M, v_lat=-1.0)], duration=3.0)
    for kind in ("T_CAR", "T_MOTO"):
        add("lidar_moving", 15, [T(kind, gc(kind, 20), LANE_M)], duration=1.0)
        add("lidar_moving", 15, [T(kind, gc(kind, 60), 0.0, v_lon=-15)], duration=2.0)
    return {"name": "w-lidar", "cases": cases}


def camera_plan():
    cases, gc = [], wrapper_lon_for_gap
    for kind in ("T_CAR", "T_MOTO", "T_TRUCK"):
        for gap in (5, 15, 40, 80, 120):
            for lat in (0.0, LANE_M):
                cases.append({"id": "camera-%03d" % len(cases), "group": "camera", "ego_speed": 0, "duration": 0.2,
                              "targets": [T(kind, gc(kind, gap), lat)], "devices": ["exp camera 720p", "hazard camera"],
                              "camera_period_ms": 100, "save_images": True})
    cases.append({"id": "camera-%03d" % len(cases), "group": "camera_moving", "ego_speed": 15, "duration": 0.6,
                  "targets": [T("T_MOTO", gc("T_MOTO", 12), LANE_M, v_lon=-15), T("T_CAR", gc("T_CAR", 25), 0.0)],
                  "devices": ["exp camera 720p", "hazard camera"], "camera_period_ms": 100, "save_images": True})
    return {"name": "w-camera", "cases": cases}


def perf_plan(profile):
    """One 60 s scene per sensor profile (moving ego, 4 targets), for simulation speed and resource use."""
    sets = {
        "original": ["radar front", "radar rear"],
        "radar_experimental": ["radar front", "radar rear", "exp radar corner fl", "exp radar corner fr",
                               "exp radar corner rl", "exp radar corner rr"],
        "radar_lidar": ["radar front", "radar rear", "exp radar corner fl", "exp radar corner fr",
                        "exp radar corner rl", "exp radar corner rr", "exp lidar roof"],
        "ibeo": ["radar front", "radar rear", "ibeo 1", "ibeo 2", "ibeo 3", "ibeo 4"],
        "camera720": ["radar front", "radar rear", "exp camera 720p"],
        "all": ["radar front", "radar rear", "exp radar corner fl", "exp radar corner fr", "exp radar corner rl",
                "exp radar corner rr", "exp lidar roof", "ibeo 1", "ibeo 2", "ibeo 3", "ibeo 4", "exp camera 720p"],
    }
    gc = wrapper_lon_for_gap
    targets = [T("T_CAR", gc("T_CAR", 25), 0.0, v_lon=0.5), T("T_CAR2", gc("T_CAR2", 10), LANE_M, v_lon=0.3),
               T("T_MOTO", gc("T_MOTO", 5, ahead=False), -LANE_M, v_lon=0.4),
               T("T_TRUCK", gc("T_TRUCK", 40), LANE_M, v_lon=-0.2)]
    return {"name": "w-perf-" + profile, "cases": [
        {"id": "perf-" + profile, "group": "perf", "ego_speed": 15, "duration": 60.0, "targets": targets,
         "devices": sets[profile], "radar_period_ms": 200, "lidar_period_ms": 100, "camera_period_ms": 100}]}


# ---------------------------------------------------------------------------------------------------------------
# run

def gpu_sampler(stop, samples):
    while not stop.is_set():
        try:
            out = subprocess.run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], capture_output=True, text=True,
                                 timeout=5).stdout
            i = out.find('"Device Utilization %"=')
            if i >= 0:
                samples.append(int(out[i + 23:].split(",")[0].split("}")[0]))
        except (subprocess.SubprocessError, ValueError):
            pass
        stop.wait(0.5)


def run(plan_path, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy(plan_path, out_dir / "plan.json")
    env = dict(os.environ, ROADSENSE_CALIB_PLAN=str(out_dir / "plan.json"), ROADSENSE_CALIB_OUT=str(out_dir))
    cmd = ["/usr/bin/time", "-l", WEBOTS, "--batch", "--stdout", "--stderr", "--mode=fast", "--no-rendering",
           str(WORLD)]
    stop, gpu = threading.Event(), []
    sampler = threading.Thread(target=gpu_sampler, args=(stop, gpu), daemon=True)
    sampler.start()
    wall = time.perf_counter()
    with open(out_dir / "console.txt", "w") as console:
        proc = subprocess.Popen(cmd, stdout=console, stderr=subprocess.STDOUT, cwd=ROOT, env=env)
        while proc.poll() is None:  # a controller that dies leaves Webots simulating forever in fast mode
            time.sleep(1)
            if "controller exited with status" in (out_dir / "console.txt").read_text(errors="replace"):
                proc.terminate()
                proc.wait()
    wall = time.perf_counter() - wall
    stop.set()
    sampler.join()
    text = (out_dir / "console.txt").read_text(errors="replace")
    usage = {}
    for line in text.splitlines():
        parts = line.split()
        if line.strip().endswith(" real") or " real " in line:
            usage.update(real_s=float(parts[0]), user_s=float(parts[2]), sys_s=float(parts[4]))
        elif "maximum resident set size" in line:
            usage["max_rss_mb"] = round(int(parts[0]) / 2 ** 20, 1)
        elif "peak memory footprint" in line:
            usage["peak_footprint_mb"] = round(int(parts[0]) / 2 ** 20, 1)
    sim_t = None
    for line in (out_dir / "calib.jsonl").read_text().splitlines()[::-1]:
        rec = json.loads(line)
        if rec.get("type") == "sample":
            sim_t = rec["t"]
            break
    meta = {"webots": WEBOTS, "returncode": proc.returncode, "wall_s": round(wall, 2), "sim_s": sim_t, "usage": usage,
            "gpu_util_pct": {"n": len(gpu), "median": statistics.median(gpu) if gpu else None,
                             "max": max(gpu) if gpu else None},
            "errors": [l for l in text.splitlines() if l.startswith("ERROR") or "Traceback" in l][:20]}
    (out_dir / "run.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))
    return proc.returncode


# ---------------------------------------------------------------------------------------------------------------
# analysis helpers

def mat(p):
    """Row-major rotation from a logged pose (x, y, z, yaw, m0..m8)."""
    return p[4:13]


def rot_t(m, v):  # R^T v
    return (m[0] * v[0] + m[3] * v[1] + m[6] * v[2], m[1] * v[0] + m[4] * v[1] + m[7] * v[2],
            m[2] * v[0] + m[5] * v[1] + m[8] * v[2])


def rot(m, v):  # R v
    return (m[0] * v[0] + m[1] * v[1] + m[2] * v[2], m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
            m[6] * v[0] + m[7] * v[1] + m[8] * v[2])


def to_world(p, local):
    d = rot(mat(p), local)
    return (p[0] + d[0], p[1] + d[1], p[2] + d[2])


def radar_pose(ego, name):
    """World position and the radar's axes (x, y, z) as world vectors."""
    mx, my, mz, myaw = RADARS[name][:4]
    pos = to_world(ego, (mx, my, mz))
    c, s = math.cos(myaw), math.sin(myaw)
    ax = rot(mat(ego), (c, s, 0.0))
    ay = rot(mat(ego), (-s, c, 0.0))
    az = rot(mat(ego), (0.0, 0.0, 1.0))
    return pos, ax, ay, az


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def body_points(kind, model_pose):
    """World corners of a kind's boxes."""
    pts = []
    for c, s in KINDS[kind]["boxes"]:
        for dx in (-0.5, 0.5):
            for dy in (-0.5, 0.5):
                for dz in (-0.5, 0.5):
                    pts.append(to_world(model_pose, (c[0] + dx * s[0], c[1] + dy * s[1], c[2] + dz * s[2])))
    return pts


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(v):
    n = math.sqrt(dot(v, v))
    return (v[0] / n, v[1] / n, v[2] / n)


def frustum_normals(hfov, vfov, max_range):
    """WbObjectDetection::computeFrustumPlanes in the device frame (planes through the device, inward normals), in
    Webots' order LEFT, BOTTOM, RIGHT, TOP. Webots computes the half angles in float."""
    import struct
    f32 = lambda x: struct.unpack("f", struct.pack("f", x))[0]
    X = max_range
    Y, Z = X * math.tan(f32(hfov / 2.0)), X * math.tan(f32(vfov / 2.0))
    tr, tl, br, bl = (X, -Y, Z), (X, Y, Z), (X, -Y, -Z), (X, Y, -Z)
    right, bottom, left, top = (_unit(_cross(a, b)) for a, b in ((tr, br), (br, bl), (bl, tl), (tl, tr)))
    return [left, bottom, right, top]


def webots_box_bounds(local_corners, normals, projection_fixed):
    """WbObjectDetection::isWithinBounds for one Box (planar device), in the device frame: returns the clipped AABB
    (min, max) or None. The installed R2025b projects a point outside a plane with v + n (v.n) (it moves further out);
    the corrected projection is v - n (v.n)."""
    LEFT, BOTTOM, RIGHT, TOP = range(4)
    front, back, inside_n = [], [], 0
    outside, correct = [False] * 4, [False] * 4
    for p in local_corners:
        if p[0] <= 0:  # PARALLEL plane: at the back of the device
            back.append(p)
            continue
        inside = True
        for j, n in enumerate(normals):
            d = dot(n, p)
            if d < 0:
                k = -d if projection_fixed else d
                p = (p[0] + n[0] * k, p[1] + n[1] * k, p[2] + n[2] * k)
                outside[j], inside = True, False
            else:
                correct[j] = True
        inside_n += inside
        front.append(p)
    if not front:
        return None
    if inside_n == 0 and not (outside[RIGHT] and outside[LEFT] and correct[BOTTOM] and correct[TOP]) and not (
            outside[BOTTOM] and outside[TOP] and correct[RIGHT] and correct[LEFT]):
        return None
    pts = front + back
    return tuple(min(q[i] for q in pts) for i in range(3)), tuple(max(q[i] for q in pts) for i in range(3))


def webots_measure(name, ego, kind, model_pose, variant):
    """Predicted noise-free radar (distance, azimuth) for a simulator variant: 'orig' (installed R2025b), 'A' (shared
    projection corrected) or 'AB' (A + range, bearing and radial speed of the one clipped-box-centre point), plus the
    measured point in the device frame. Ported from WbObjectDetection/WbRadar at 838bb6f; scoring only."""
    pos, ax, ay, az = radar_pose(ego, name)
    _, _, _, _, max_range, hfov, vfov = RADARS[name]
    normals = frustum_normals(hfov, vfov, max_range)
    lo = hi = None
    for i, (c, sz) in enumerate(KINDS[kind]["boxes"]):
        corners = []
        for dx in (-0.5, 0.5):
            for dy in (-0.5, 0.5):
                for dz in (-0.5, 0.5):
                    w = sub(to_world(model_pose, (c[0] + dx * sz[0], c[1] + dy * sz[1], c[2] + dz * sz[2])), pos)
                    corners.append((dot(w, ax), dot(w, ay), dot(w, az)))
        b = webots_box_bounds(corners, normals, variant != "orig")
        if b is not None:
            lo = b[0] if lo is None else tuple(min(x, y) for x, y in zip(lo, b[0]))
            hi = b[1] if hi is None else tuple(max(x, y) for x, y in zip(hi, b[1]))
    if lo is None:
        return None
    centre = tuple((x + y) / 2 for x, y in zip(lo, hi))
    o = sub(model_pose[:3], pos)
    ox, oy, oz = dot(o, ax), dot(o, ay), dot(o, az)
    if variant == "orig":
        a = math.acos(max(-1.0, min(1.0, ox / math.sqrt(ox * ox + oy * oy + 4 * oz * oz))))
        a = -a if oy > 0 else a
    elif variant == "A":
        a = -math.atan2(oy, ox)
    else:
        a = -math.atan2(centre[1], centre[0])
    return {"d": math.sqrt(dot(centre, centre)), "az": max(-hfov / 2, min(hfov / 2, a)), "point": centre,
            "origin": (ox, oy, oz)}


def expected(name, ego, kind, model_pose):
    """Expected radar measurements under the verified Webots semantics, and the candidates around them."""
    pos, ax, ay, az = radar_pose(ego, name)
    fov, vfov = RADARS[name][5], RADARS[name][6]
    pts = [sub(p, pos) for p in body_points(kind, model_pose)]
    local = [(dot(p, ax), dot(p, ay), dot(p, az)) for p in pts]
    # WbObjectDetection merges only the boxes the frustum reaches: the beam is thin vertically
    boxes = [local[i:i + 8] for i in range(0, len(local), 8)]
    inside = [b for b in boxes if min(p[2] for p in b) <= math.hypot(sum(p[0] for p in b) / 8, sum(
        p[1] for p in b) / 8) * math.tan(vfov / 2) and max(p[2] for p in b) >= -math.hypot(
        sum(p[0] for p in b) / 8, sum(p[1] for p in b) / 8) * math.tan(vfov / 2)]
    local_d = [p for b in (inside or boxes) for p in b]
    xs, ys, zs = [p[0] for p in local_d], [p[1] for p in local_d], [p[2] for p in local_d]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    h = math.hypot(cx, cy)
    half = h * math.tan(vfov / 2)  # the frustum clips the box to the beam's height at that range
    zlo, zhi = max(min(zs), -half), min(max(zs), half)
    cz = (zlo + zhi) / 2 if zlo <= zhi else (min(zs) + max(zs)) / 2
    o = sub(model_pose[:3], pos)
    ox, oy, oz = dot(o, ax), dot(o, ay), dot(o, az)
    # WbRadar::setTargetProperties: azimuth = angle(radar x, plane.vectorProjection(v)); WbAffinePlane's
    # vectorProjection is v + n (v.n) (838bb6f), which doubles the radar-vertical component instead of removing it
    webots_az = math.acos(max(-1.0, min(1.0, ox / math.sqrt(ox * ox + oy * oy + 4 * oz * oz))))
    if oy > 0:
        webots_az = -webots_az
    webots_az = max(-fov / 2, min(fov / 2, webots_az))
    centre_ego = rot_t(mat(ego), sub(to_world(model_pose, tuple(
        (a + b) / 2 for a, b in zip(model_bounds(kind)[0::2], model_bounds(kind)[1::2]))), ego[:3]))
    return {
        "dist_bbox_centre": math.sqrt(cx * cx + cy * cy + cz * cz),
        "dist_bbox_centre_2d": h,
        "dist_origin": math.sqrt(ox * ox + oy * oy + oz * oz),
        "dist_nearest": min(math.hypot(p[0], p[1]) for p in local),
        "az_origin_true": -math.atan2(oy, ox),
        "az_bbox_true": -math.atan2(cy, cx),
        "az_webots_model": webots_az,
        "elev_origin": math.atan2(oz, math.hypot(ox, oy)),
        "in_fov": all(abs(math.atan2(p[1], p[0])) < fov / 2 and p[0] > 0 for p in local),
        "any_in_fov": any(abs(math.atan2(p[1], p[0])) < fov / 2 and p[0] > 0 for p in local),
        "centre_ego": centre_ego,
        "x_extent_ego": None,
    }


def target_kinds(case):
    return [t["name"] for t in case["spec"]["targets"]]


def load(run_dir):
    cases, samples, ends = {}, defaultdict(list), {}
    with open(Path(run_dir) / "calib.jsonl") as f:
        for line in f:
            r = json.loads(line.replace("NaN", "null").replace("Infinity", "1e999"))
            if r["type"] == "case":
                cases[r["id"]] = r
            elif r["type"] == "sample":
                samples[r["case"]].append(r)
            elif r["type"] == "case_end":
                ends[r["id"]] = r
    return cases, samples, ends


def pct(values, q):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * q
    i = int(k)
    return v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * (k - i)


def stats(values):
    v = [x for x in values if x is not None and math.isfinite(x)]
    if not v:
        return {"n": 0}
    return {"n": len(v), "mean": round(statistics.fmean(v), 4), "p50": round(pct(v, 0.5), 4),
            "p95": round(pct(v, 0.95), 4), "max": round(max(v), 4), "min": round(min(v), 4)}


def lane_of(lat):
    """Lane offset from the ego lane by lateral position: 0 = ego lane, +1 = next lane left, -1 = right."""
    return int(math.floor((lat + LANE_M / 2) / LANE_M))


# ---------------------------------------------------------------------------------------------------------------
# analysis

def match(raw, exp_list, name):
    """Pair raw targets with ground-truth vehicles greedily on range (within 2 m of the bbox-centre distance) and
    azimuth (the verified Webots model, sign-agnostic near the axis, within 1.5 m across). Scoring only."""
    pairs, used = {}, set()
    cands = []
    for tname, e in exp_list.items():
        for i, (d, az, sp, pw) in enumerate(raw):
            if d <= 1.0001:  # the ego's own body, clamped to the minimum range
                continue
            pd, paz = e.get("pred_d", e["dist_bbox_centre"]), e.get("pred_az", e["az_webots_model"])
            err = abs(d - pd)
            across = pd * min(abs(az - paz), abs(az + paz))
            if err < 2.0 and across < 1.5:
                cands.append((err + across, tname, i))
    for err, tname, i in sorted(cands):
        if tname in pairs or i in used:
            continue
        pairs[tname] = i
        used.add(i)
    return pairs, [i for i in range(len(raw)) if i not in used and raw[i][0] > 1.0001]


# Nominal heights for the observable interpretation below: ego origin 0.31 m above the ground (measured in the lab),
# target model origins at a car's wheel radius (0.32 m), a car's origin 1.4 m behind its body centre.
EGO_ORIGIN_Z, NOMINAL_TARGET_ORIGIN_Z, NOMINAL_ORIGIN_BEHIND_CENTRE = 0.31, 0.32, 1.4


def corrected_azimuth(name, d, az):
    """Undo the Webots elevation leak from sensor data and nominal geometry only (evaluation of an interpretation).

    Webots reports |az| = acos(x / sqrt(x^2 + y^2 + 4 z^2)) for the origin vector (x, y, z) in the radar frame.
    Taking the origin's horizontal range as d - 1.4 m (a car) and z from nominal heights gives x, then y."""
    dz = RADARS[name][2] + EGO_ORIGIN_Z - NOMINAL_TARGET_ORIGIN_Z
    rho = max(d - NOMINAL_ORIGIN_BEHIND_CENTRE, 0.3)
    x = math.cos(az) * math.sqrt(rho * rho + 4 * dz * dz)
    y = math.sqrt(max(0.0, rho * rho - x * x))
    return math.copysign(math.atan2(y, x), az)


def ego_point(name, d, az):
    """RoadSense's transform (perception.radar_detections) for any mounted radar: ego-frame (lon, lat)."""
    mx, my, _, myaw = RADARS[name][:4]
    a = myaw - az
    return mx + d * math.cos(a), my + d * math.sin(a)


def analyse_radar(run_dir, variant="orig"):
    """variant: the simulator build that produced run_dir (orig = installed R2025b, A, AB: W8 patches)."""
    from roadsense.autonomy import perception
    cases, samples, ends = load(run_dir)
    rows = []
    for cid, case in cases.items():
        spec = case["spec"]
        prev = {}
        for s in samples[cid]:
            for name, raw in s["sensors"].items():
                if name not in RADARS or not isinstance(raw, list):
                    continue
                exps = {}
                for tname in target_kinds(case):
                    exps[tname] = expected(name, s["gt"]["ego"], tname, s["gt"]["targets"][tname]["model"])
                    pred = webots_measure(name, s["gt"]["ego"], tname, s["gt"]["targets"][tname]["model"], variant)
                    if pred is not None:
                        exps[tname].update(pred_d=pred["d"], pred_az=pred["az"])
                pairs, extra = match(raw, exps, name)
                own = sum(1 for t in raw if t[0] <= 1.0001)
                for tname, e in exps.items():
                    row = {"case": cid, "group": spec["group"], "t": s["t"], "k": s["k"], "radar": name,
                           "target": tname, "ego_speed": spec["ego_speed"], "own_body": own, "extra": len(extra),
                           "in_fov": e["in_fov"], "any_in_fov": e["any_in_fov"],
                           "range_true": e["dist_bbox_centre_2d"], "period": spec.get("radar_period_ms", 200),
                           "update": spec.get("target_update_ms", 10), "first": s is samples[cid][0]}
                    tg = [t for t in spec["targets"] if t["name"] == tname][0]
                    row["stationary_target"] = abs(spec["ego_speed"] + tg["v_lon"]) < 1e-6 and tg["v_lat"] == 0
                    ce = e["centre_ego"]
                    row.update(centre_lon=ce[0], centre_lat=ce[1], lane_true=lane_of(ce[1]))
                    i = pairs.get(tname)
                    row["detected"] = i is not None
                    if i is not None:
                        d, az, sp, pw = raw[i]
                        row.update(d=d, az=az, speed=sp, power=pw,
                                   err_d_bbox=d - e["dist_bbox_centre"], err_d_origin=d - e["dist_origin"],
                                   err_d_nearest=d - e["dist_nearest"],
                                   err_az_origin_deg=math.degrees(az - e["az_origin_true"]),
                                   err_az_bbox_deg=math.degrees(az - e["az_bbox_true"]),
                                   err_az_model_deg=math.degrees(az - e["az_webots_model"]),
                                   # the sign is decided by a lateral component that is ~0 dead ahead: magnitude
                                   err_az_model_mag_deg=math.degrees(abs(az) - abs(e["az_webots_model"])),
                                   err_d_pred=d - e["pred_d"] if "pred_d" in e else None,
                                   err_az_pred_mag_deg=math.degrees(abs(az) - abs(e["pred_az"]))
                                   if "pred_az" in e else None,
                                   elev_origin_deg=math.degrees(e["elev_origin"]),
                                   az_clamped=abs(abs(az) - RADARS[name][5] / 2) < 0.006)
                        if name in perception.RADARS:  # what RoadSense makes of it, with RoadSense's own pose input
                            pose = (s["gps"][0], s["gps"][1], s["imu_yaw"])
                            lim = (1.0, RADARS[name][4], RADARS[name][5])
                            row["roadsense_kept"] = bool(perception.radar_detections(name, [(d, az, sp)], lim, s["t"],
                                                                                     pose))
                        else:  # the same edge rule for the other radars
                            row["roadsense_kept"] = 1.0 < d < RADARS[name][4] and abs(az) <= RADARS[name][5] / 2 - 0.005
                        if row["roadsense_kept"]:
                            lon, lat = ego_point(name, d, az)  # identical to perception.radar_detections (tested)
                            row.update(rs_lon=lon, rs_lat=lat, err_lon=lon - ce[0], err_lat=lat - ce[1],
                                       lane_rs=lane_of(lat))
                            # historical reference (the SUMO-style wrapper, what earlier oracles scored against)
                            wr = rot_t(mat(s["gt"]["ego"]), sub(s["gt"]["targets"][tname]["wrapper"][:3],
                                                                 s["gt"]["ego"][:3]))
                            row.update(hist_err_lon=lon - wr[0], hist_err_lat=lat - wr[1])
                            lon2, lat2 = ego_point(name, d, corrected_azimuth(name, d, az))
                            row.update(cor_err_lon=lon2 - ce[0], cor_err_lat=lat2 - ce[1], lane_cor=lane_of(lat2))
                        # true radial velocity of the origin (Webots' definition) and of the body centre
                        if tname in prev and not row["first"]:
                            p0, t0 = prev[tname]
                            pos, ax, ay, az_ = radar_pose(s["gt"]["ego"], name)
                            pos0, *_ = radar_pose(p0["ego"], name)
                            dt = s["t"] - t0
                            o1 = s["gt"]["targets"][tname]["model"][:3]
                            o0 = p0["targets"][tname]["model"][:3]
                            rel_v = [((o1[j] - o0[j]) - (pos[j] - pos0[j])) / dt for j in range(3)]
                            los = sub(o1, pos)
                            n = math.sqrt(dot(los, los))
                            row["v_radial_true"] = dot(rel_v, los) / n
                            row["err_speed"] = sp - row["v_radial_true"]
                    rows.append(row)
            for tname in target_kinds(case):
                prev[tname] = (s["gt"], s["t"])
    return cases, rows, ends


def summarise_radar(rows, cases, ends):
    out = {}
    det = [r for r in rows if r["detected"]]
    # W3 reference points: noise-free diagnostic radar only, targets wholly inside the field of view
    diag = [r for r in det if r["radar"].startswith("diag") and r["in_fov"]]
    ref = {}
    for kind in KINDS:
        rr = [r for r in diag if r["target"] == kind and r["group"] in ("static", "static_rear", "orientation")]
        ref[kind] = {k: stats([abs(r[k]) for r in rr]) for k in (
            "err_d_bbox", "err_d_origin", "err_d_nearest", "err_az_origin_deg", "err_az_bbox_deg",
            "err_az_model_deg", "err_az_model_mag_deg", "err_d_pred", "err_az_pred_mag_deg")}
    out["reference_points_abs_error"] = ref
    # Azimuth bug: dead-ahead targets
    ahead = [r for r in diag if abs(r["centre_lat"]) < 0.01 and r["group"] in ("static", "both_stopped")]
    out["dead_ahead_azimuth"] = [{"case": r["case"], "target": r["target"], "radar": r["radar"],
                                  "range": round(r["range_true"], 2), "az_deg": round(math.degrees(r["az"]), 3),
                                  "model_deg": round(math.degrees(r["az"]) - r["err_az_model_deg"], 3),
                                  "elev_origin_deg": round(r["elev_origin_deg"], 3)}
                                 for r in ahead if r["t"] == min(x["t"] for x in ahead if x["case"] == r["case"])][:60]
    # detection and accuracy per radar x group, the RoadSense view: only samples whose target is at least partly
    # inside that radar's field of view and range
    table = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["first"] or not r["any_in_fov"] or r["range_true"] > RADARS[r["radar"]][4]:
            continue
        key = (r["radar"], r["group"])
        table[key]["det"].append(r["detected"])
        if r["detected"]:
            for k in ("err_d_bbox", "err_az_origin_deg", "err_lon", "err_lat", "cor_err_lat", "err_speed"):
                if k in r:
                    table[key][k].append(abs(r[k]))
            if "lane_rs" in r:
                table[key]["lane_ok"].append(r["lane_rs"] == r["lane_true"])
                table[key]["lane_cor_ok"].append(r["lane_cor"] == r["lane_true"])
            table[key]["kept"].append(r["roadsense_kept"])
    out["by_radar_group"] = {
        "%s | %s" % k: {"samples": len(v["det"]), "detection_rate": round(sum(v["det"]) / len(v["det"]), 3),
                        **{m: stats(v[m]) for m in ("err_d_bbox", "err_az_origin_deg", "err_lon", "err_lat",
                                                    "cor_err_lat", "err_speed")},
                        "lane_agreement": round(sum(v["lane_ok"]) / len(v["lane_ok"]), 3) if v["lane_ok"] else None,
                        "lane_agreement_corrected": round(sum(v["lane_cor_ok"]) / len(v["lane_cor_ok"]), 3)
                        if v["lane_cor_ok"] else None,
                        "roadsense_kept": round(sum(v["kept"]) / len(v["kept"]), 3) if v["kept"] else None}
        for k, v in sorted(table.items())}
    out["configurations"] = compare_configurations(rows)
    out["own_body_rate"] = {name: round(statistics.fmean([r["own_body"] > 0 for r in rows if r["radar"] == name]), 3)
                            for name in RADARS}
    out["case_wall_s"] = {cid: e["wall_s"] for cid, e in ends.items()}
    return out


CONFIGS = {
    "original": ("radar front", "radar rear"),
    "original+corners": ("radar front", "radar rear", "exp radar corner fl", "exp radar corner fr",
                         "exp radar corner rl", "exp radar corner rr"),
}


def band(r):
    return "0-10" if r < 10 else "10-30" if r < 30 else "30-60" if r < 60 else "60-100" if r < 100 else "100-150"


def compare_configurations(rows):
    """Per configuration: was each vehicle seen (and kept by RoadSense's edge rule) by any of its radars, and how
    well is it placed. The nearest kept detection stands for the configuration."""
    obs = defaultdict(dict)
    for r in rows:
        if r["first"]:
            continue
        obs[(r["case"], r["t"], r["target"])][r["radar"]] = r
    out = {}
    for cname, radars in CONFIGS.items():
        t = defaultdict(lambda: defaultdict(list))
        for (case, _, target), by in obs.items():
            any_r = next(iter(by.values()))
            kept = [by[n] for n in radars if n in by and by[n]["detected"] and by[n].get("roadsense_kept")]
            best = min(kept, key=lambda r: r["d"]) if kept else None
            motion = "stationary" if any_r["stationary_target"] else "moving"
            for key in ((any_r["group"], target), ("ALL " + motion, band(any_r["range_true"]))):
                t[key]["seen"].append(best is not None)
                if best is not None:
                    t[key]["lat"].append(abs(best["err_lat"]))
                    t[key]["lon"].append(abs(best["err_lon"]))
                    t[key]["cor_lat"].append(abs(best["cor_err_lat"]))
                    t[key]["hist_lat"].append(abs(best["hist_err_lat"]))
                    t[key]["hist_lon"].append(abs(best["hist_err_lon"]))
                    t[key]["lane"].append(best["lane_rs"] == best["lane_true"])
                    t[key]["lane_cor"].append(best["lane_cor"] == best["lane_true"])
        out[cname] = {"%s | %s" % k: {"n": len(v["seen"]), "seen": round(statistics.fmean(v["seen"]), 3),
                                      "lat": stats(v["lat"]), "lon": stats(v["lon"]), "cor_lat": stats(v["cor_lat"]),
                                      "hist_lat": stats(v["hist_lat"]), "hist_lon": stats(v["hist_lon"]),
                                      "lane": round(statistics.fmean(v["lane"]), 3) if v["lane"] else None,
                                      "lane_cor": round(statistics.fmean(v["lane_cor"]), 3) if v["lane_cor"] else None}
                      for k, v in sorted(t.items())}
    return out


def range_image_points(raw, n, w, layers, fov, vfov, mount, elevations=None):
    """Ego-frame points from a Webots lidar range image: Euclidean ranges, columns left to right at
    fov/2 - (i + 0.5) fov/w, layers top down (verified against Webots' point cloud). `elevations`: per-layer
    angles measured from a point cloud (Webots spreads the layers over slightly more than verticalFieldOfView)."""
    import numpy as np
    r = np.frombuffer(raw, dtype=np.float32, count=n).reshape(layers, w).astype(np.float64)
    az = fov / 2 - (np.arange(w) + 0.5) * fov / w
    if elevations is None:
        elevations = (vfov / 2 - np.arange(layers) * vfov / (layers - 1)) if layers > 1 else np.zeros(1)
    A, E = np.meshgrid(az, np.asarray(elevations))
    ok = np.isfinite(r) & (r > 0)
    x, y, z = r * np.cos(E) * np.cos(A), r * np.cos(E) * np.sin(A), r * np.sin(E)
    mx, my, mz, myaw = mount
    c, s = math.cos(myaw), math.sin(myaw)
    x, y, z = x[ok], y[ok], z[ok]
    return np.stack([mx + c * x - s * y, my + s * x + c * y, mz + z], axis=1)


def layer_elevations(pc_raw, layers, w):
    """Per-layer elevation (rad) fitted linearly to a Webots point cloud (x, y, z, layer, time; 20 bytes/point)."""
    import numpy as np
    pc = np.frombuffer(pc_raw, dtype=np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("l", "<i4"),
                                               ("t", "<f4")]), count=layers * w)
    P = np.stack([pc["x"], pc["y"], pc["z"]], 1).astype(float).reshape(layers, w, 3)
    el = np.where(np.isfinite(P).all(-1), np.arctan2(P[..., 2], np.hypot(P[..., 0], P[..., 1])), np.nan)
    # only well-populated layers: a few stray points in mostly empty (sky) layers carry no angle information
    rows = [(L, float(np.median(el[L][np.isfinite(el[L])]))) for L in range(layers)
            if np.isfinite(el[L]).sum() >= w // 4]
    b, a = np.polyfit([r[0] for r in rows], [r[1] for r in rows], 1)
    return a + b * np.arange(layers)


def clusters(pts, cell=0.4):
    """2D connected components on a grid: simple and deterministic."""
    import numpy as np
    if len(pts) == 0:
        return []
    keys = np.floor(pts[:, :2] / cell).astype(np.int64)
    cells = defaultdict(list)
    for i, k in enumerate(map(tuple, keys)):
        cells[k].append(i)
    seen, out = set(), []
    for k in cells:
        if k in seen:
            continue
        stack, members = [k], []
        seen.add(k)
        while stack:
            c = stack.pop()
            members.extend(cells[c])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (c[0] + dx, c[1] + dy)
                    if nb in cells and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        out.append(pts[members])
    return out


def analyse_lidar(run_dir):
    import numpy as np
    cases, samples, ends = load(run_dir)
    run_dir = Path(run_dir)
    blobs = {}
    rows = []
    elev = {}
    for cid in cases:  # layer elevations from any recorded point cloud
        for s in samples[cid]:
            for name, meta in s["sensors"].items():
                if isinstance(meta, dict) and "pc_offset" in meta and name not in elev:
                    pc = (run_dir / (name.replace(" ", "_") + "_pc.bin")).read_bytes()
                    elev[name] = layer_elevations(pc[meta["pc_offset"]:meta["pc_offset"] + meta["n"] * 20],
                                                  meta["layers"], meta["w"])
    for cid, case in cases.items():
        spec = case["spec"]
        for s in samples[cid]:
            ego = s["gt"]["ego"]
            for name, meta in s["sensors"].items():
                if name not in LIDARS or not isinstance(meta, dict):
                    continue
                if name not in blobs:
                    blobs[name] = (run_dir / (name.replace(" ", "_") + ".bin")).read_bytes()
                raw = blobs[name][meta["offset"]:meta["offset"] + meta["n"] * 4]
                pts = range_image_points(raw, meta["n"], meta["w"], meta["layers"], meta["fov"], meta["vfov"],
                                         LIDARS[name], elev.get(name))
                # sensor-only filtering: no ground truth. Ground: below 0.25 m above the ego's wheel-contact plane
                # (ego origin z 0.31 above the ground); the ego's own body: inside its footprint.
                z_ground = -0.31
                keep = (pts[:, 2] > z_ground + 0.25) & ~((pts[:, 0] > EGO_REAR_M - 0.3) & (pts[:, 0] < EGO_FRONT_M + 0.3)
                                                          & (np.abs(pts[:, 1]) < EGO_HALF_WIDTH_M + 0.3))
                obj = pts[keep]
                cl = [c for c in clusters(obj) if len(c) >= 3]
                for tname in target_kinds(case):
                    mp = s["gt"]["targets"][tname]["model"]
                    corners = [rot_t(mat(ego), sub(p, ego[:3])) for p in body_points(tname, mp)]
                    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
                    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
                    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
                    # the cluster with points closest to the true footprint (scoring only)
                    best, bd = None, 1.5
                    for c in cl:
                        dx = np.maximum(np.maximum(xmin - c[:, 0], c[:, 0] - xmax), 0)
                        dy = np.maximum(np.maximum(ymin - c[:, 1], c[:, 1] - ymax), 0)
                        d = float(np.min(np.hypot(dx, dy)))
                        if d < bd:
                            best, bd = c, d
                    row = {"case": cid, "group": spec["group"], "lidar": name, "target": tname, "t": s["t"],
                           "centre_lon": cx, "centre_lat": cy, "lane_true": lane_of(cy), "n_points_scene": int(len(obj)),
                           "detected": best is not None, "ego_speed": spec["ego_speed"]}
                    if best is not None:
                        lon_near = float(best[:, 0].min()) if cx > 0 else float(best[:, 0].max())
                        true_near = xmin if cx > 0 else xmax
                        lat_mid = float((best[:, 1].min() + best[:, 1].max()) / 2)
                        lat_near = float(best[:, 1].min()) if cy > 0 else float(best[:, 1].max())
                        true_lat_near = ymin if cy > 0 else ymax
                        row.update(n_points=int(len(best)), err_lon_nearface=lon_near - true_near,
                                   err_lat_nearside=(lat_near - true_lat_near) if abs(cy) > 1.0 else None,
                                   err_lat_mid=lat_mid - cy, lane_lidar=lane_of(lat_mid),
                                   extent_lon=float(np.ptp(best[:, 0])), extent_lat=float(np.ptp(best[:, 1])))
                    rows.append(row)
    return cases, rows, ends


LIDAR_CONFIGS = {"ibeo x4 (installed, disabled)": ("ibeo 1", "ibeo 2", "ibeo 3", "ibeo 4"),
                 "roof 16-layer (experimental)": ("exp lidar roof",)}


def lidar_union(rows):
    """Per configuration and group: seen by any of its lidars in the same scan instant (lidars sampled at 400 ms
    common instants for the IbeoLux, every 100 ms for the roof unit), and the best lateral near-side error."""
    by = defaultdict(dict)
    for r in rows:
        by[(r["case"], r["t"], r["target"])][r["lidar"]] = r
    out = {}
    for cname, lids in LIDAR_CONFIGS.items():
        t = defaultdict(lambda: defaultdict(list))
        for (case, _, target), d in by.items():
            have = [d[n] for n in lids if n in d]
            if len(have) < len(lids):
                continue
            seen = [r for r in have if r["detected"]]
            key = (have[0]["group"], target)
            t[key]["seen"].append(bool(seen))
            if seen:
                t[key]["lat_mid"].append(min(abs(r["err_lat_mid"]) for r in seen))
                t[key]["lane"].append(any(r["lane_lidar"] == r["lane_true"] for r in seen))
        out[cname] = {"%s | %s" % k: {"n": len(v["seen"]), "seen": round(statistics.fmean(v["seen"]), 3),
                                      "lat_mid": stats(v["lat_mid"]),
                                      "lane": round(statistics.fmean(v["lane"]), 3) if v["lane"] else None}
                      for k, v in sorted(t.items())}
    return out


def summarise_lidar(rows):
    table = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["lidar"], r["group"], r["target"])
        table[key]["det"].append(r["detected"])
        if r["detected"]:
            for k in ("err_lon_nearface", "err_lat_nearside", "err_lat_mid", "n_points"):
                if r.get(k) is not None:
                    table[key][k].append(abs(r[k]) if k != "n_points" else r[k])
            table[key]["lane_ok"].append(r["lane_lidar"] == r["lane_true"])
    return {"%s | %s | %s" % k: {"scans": len(v["det"]), "detection_rate": round(sum(v["det"]) / len(v["det"]), 3),
                                  **{m: stats(v[m]) for m in ("err_lon_nearface", "err_lat_nearside", "err_lat_mid",
                                                              "n_points")},
                                  "lane_agreement": round(sum(v["lane_ok"]) / len(v["lane_ok"]), 3) if v["lane_ok"] else None}
            for k, v in sorted(table.items())}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub_ = ap.add_subparsers(dest="cmd", required=True)
    p = sub_.add_parser("plan")
    p.add_argument("kind", choices=["radar", "lidar", "camera", "perf"])
    p.add_argument("--profile", default="original")
    p.add_argument("out")
    r = sub_.add_parser("run")
    r.add_argument("plan")
    r.add_argument("out_dir")
    a = sub_.add_parser("analyse")
    a.add_argument("kind", choices=["radar", "lidar"])
    a.add_argument("run_dir")
    a.add_argument("out")
    a.add_argument("--variant", default="orig", choices=["orig", "A", "AB"],
                   help="simulator build that produced the run (W8)")
    args = ap.parse_args()
    if args.cmd == "plan":
        plan = {"radar": build_plan, "lidar": lidar_plan, "camera": camera_plan}.get(args.kind)
        plan = plan() if plan else perf_plan(args.profile)
        Path(args.out).write_text(json.dumps(plan, indent=1))
        print("%d cases, %.0f s simulated (excluding ego settling)" % (
            len(plan["cases"]), sum(c["duration"] for c in plan["cases"])))
    elif args.cmd == "run":
        sys.exit(run(args.plan, args.out_dir))
    elif args.kind == "radar":
        cases, rows, ends = analyse_radar(args.run_dir, args.variant)
        summary = summarise_radar(rows, cases, ends)
        with open(Path(args.out).with_suffix(".rows.jsonl"), "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        Path(args.out).write_text(json.dumps(summary, indent=1))
    else:
        cases, rows, ends = analyse_lidar(args.run_dir)
        with open(Path(args.out).with_suffix(".rows.jsonl"), "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        Path(args.out).write_text(json.dumps({"per_lidar": summarise_lidar(rows), "union": lidar_union(rows)},
                                             indent=1))


if __name__ == "__main__":
    main()
