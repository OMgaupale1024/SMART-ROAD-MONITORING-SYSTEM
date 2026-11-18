"""RoadSense calibration laboratory controller (Stage W2), for roadsense_calibration.wbt only.

Runs the cases of a calibration plan (ROADSENSE_CALIB_PLAN, JSON written by tools/webots_calibration.py) and writes
what each sensor returned, unprocessed, beside independent supervisor ground truth (ROADSENSE_CALIB_OUT directory).
Ground truth is written for scoring only: nothing here feeds RoadSense inference.

Each case drives the ego at a set speed along +x (stopped when 0), places targets once at the case start, moves them
at constant world velocity (re-placed every target_update_ms, 10 = smooth, 200 = SUMO-like steps) and records every
enabled radar at its sampling instants, ground truth before and after that step, and optionally lidar range images
and camera frames. Python 3.9 (Webots' controller interpreter), standard library only.
"""
import ctypes
import json
import math
import os
import time

from controller.wb import wb
from vehicle import Driver

driver = Driver()
STEP = int(driver.getBasicTimeStep())
plan = json.load(open(os.environ["ROADSENSE_CALIB_PLAN"]))
out_dir = os.environ["ROADSENSE_CALIB_OUT"]
log = open(os.path.join(out_dir, "calib.jsonl"), "x", buffering=1)
ego = driver.getSelf()
gps = driver.getDevice("telemetry gps")
imu = driver.getDevice("inertial unit")
gps.enable(STEP)
imu.enable(STEP)
targets = {name: driver.getFromDef(name) for name in ("T_BOX_C", "T_BOX_O", "T_CAR", "T_CAR2", "T_TRUCK", "T_MOTO",
                                                      "T_RICK")}
models = {name: (node.getField("children").getMFNode(0) if node.getTypeName() == "Solid" and name not in
                 ("T_BOX_C", "T_BOX_O") else node) for name, node in targets.items()}
HIDDEN = {name: [10000.0, 10.0 * i, node.getField("translation").getSFVec3f()[2]]
          for i, (name, node) in enumerate(targets.items())}
binfiles = {}


def emit(**record):
    log.write(json.dumps(record, separators=(",", ":")) + "\n")


def yaw_of(node):
    m = node.getOrientation()  # row-major 3x3, column 0 = the node's x axis in the world
    return math.atan2(m[3], m[0])


def pose(node):
    """World x, y, z, yaw, then the full row-major rotation matrix (roll and pitch matter to the radar)."""
    p = node.getPosition()
    return [round(p[0], 4), round(p[1], 4), round(p[2], 4), round(yaw_of(node), 6)] + [
        round(v, 7) for v in node.getOrientation()]


def ground_truth(active):
    return {"ego": pose(ego), "targets": {n: {"wrapper": pose(targets[n]), "model": pose(models[n])} for n in active}}


def place(name, x, y, yaw):
    node = targets[name]
    node.getField("translation").setSFVec3f([x, y, HIDDEN[name][2]])
    node.getField("rotation").setSFRotation([0, 0, 1, yaw])


def hide_all():
    for name, node in targets.items():
        node.getField("translation").setSFVec3f(HIDDEN[name])
        node.getField("rotation").setSFRotation([0, 0, 1, 0])


def drive(speed_mps, y_ref):
    """Cruise straight along +x at speed (brake to a stop at 0), holding the lateral position y_ref."""
    p, yaw = ego.getPosition(), yaw_of(ego)
    if speed_mps <= 0:
        driver.setCruisingSpeed(0)
        driver.setBrakeIntensity(1)
        driver.setSteeringAngle(0)
        return
    driver.setBrakeIntensity(0)
    driver.setCruisingSpeed(speed_mps * 3.6)
    # Webots: a positive steering angle turns right (toward -y when heading +x)
    driver.setSteeringAngle(max(-0.1, min(0.1, 0.05 * (p[1] - y_ref) + 0.8 * yaw)))


def step():
    if driver.step() == -1:
        raise SystemExit(0)


def settle_ego(speed_mps, y_ref):
    """Run until the ego holds the case speed, straight: never recorded."""
    t0, steady = driver.getTime(), 0.0
    while driver.getTime() - t0 < 40:
        drive(speed_mps, y_ref)
        step()
        v = ego.getVelocity()
        ok = abs(math.hypot(v[0], v[1]) - speed_mps) < 0.1 and abs(yaw_of(ego)) < 0.01 and abs(v[5]) < 0.01
        steady = steady + STEP / 1000.0 if ok else 0.0
        if steady >= 1.0:
            return True
    return False


def dump(dev, kind, data):
    f = binfiles.get(dev)
    if f is None:
        f = binfiles[dev] = open(os.path.join(out_dir, dev.replace(" ", "_") + ".bin"), "xb")
    offset = f.tell()
    f.write(data)
    return offset


devices = {}


def device(name):
    if name not in devices:
        devices[name] = driver.getDevice(name)
    return devices[name]


def run_case(case):
    enabled = case["devices"]
    period = case.get("radar_period_ms", 200)
    lidar_period = case.get("lidar_period_ms", 100)
    y_ref = 0.0
    hide_all()
    settled = settle_ego(case["ego_speed"], y_ref)
    e = ego.getPosition()
    eyaw = yaw_of(ego)
    c, s = math.cos(eyaw), math.sin(eyaw)
    start = {}
    for tg in case["targets"]:  # ego-frame start pose (origin = ego origin, the rear axle) -> world
        wx = e[0] + tg["lon"] * c - tg["lat"] * s
        wy = e[1] + tg["lon"] * s + tg["lat"] * c
        wyaw = eyaw + math.radians(tg.get("yaw_deg", 0.0))
        # world velocity: the ego's set speed along its heading plus the target's relative velocity
        vx = case["ego_speed"] * c + tg.get("v_lon", 0.0) * c - tg.get("v_lat", 0.0) * s
        vy = case["ego_speed"] * s + tg.get("v_lon", 0.0) * s + tg.get("v_lat", 0.0) * c
        start[tg["name"]] = (wx, wy, wyaw, vx, vy)
        place(tg["name"], wx, wy, wyaw)
    for name in enabled:
        dev = device(name)
        if name.startswith(("radar", "diag radar", "exp radar")):
            dev.enable(period)
        elif "lidar" in name or name.startswith("ibeo"):
            dev.enable(lidar_period if name.startswith("exp") else 80)
            if case.get("point_cloud"):
                dev.enablePointCloud()
        elif "camera" in name:
            dev.enable(case.get("camera_period_ms", 100))
    t0 = driver.getTime()
    emit(type="case", id=case["id"], group=case["group"], t0=round(t0, 3), settled=settled, spec=case,
         start={k: [round(v, 4) for v in val] for k, val in start.items()})
    active = [tg["name"] for tg in case["targets"]]
    update_ms = case.get("target_update_ms", STEP)
    steps = int(round(case["duration"] * 1000 / STEP))
    wall0 = time.perf_counter()
    step_wall = []
    for k in range(1, steps + 1):
        drive(case["ego_speed"], y_ref)
        elapsed_ms = k * STEP
        if elapsed_ms % update_ms == 0:  # SUMO moves its cars at the end of each of its steps
            tt = elapsed_ms / 1000.0
            for name, (wx, wy, wyaw, vx, vy) in start.items():
                place(name, wx + vx * tt, wy + vy * tt, wyaw)
                if case.get("set_velocity"):  # as SumoSupervisor does: velocity toward the next SUMO pose
                    targets[name].setVelocity([vx, vy, 0, 0, 0, 0])
        record = elapsed_ms % period == 0 or (elapsed_ms % (case.get("lidar_period_ms", 100)) == 0 and any(
            "lidar" in n or n.startswith("ibeo") for n in enabled))
        pre = ground_truth(active) if record else None
        w = time.perf_counter()
        step()
        step_wall.append(time.perf_counter() - w)
        now = round(driver.getTime(), 3)
        if not record:
            continue
        gt = ground_truth(active)
        sensors = {}
        for name in enabled:
            dev = device(name)
            if "radar" in name and elapsed_ms % period == 0:
                sensors[name] = [[round(t.distance, 4), round(t.azimuth, 6), round(t.speed, 4),
                                  round(t.receiver_power, 2)] for t in dev.getTargets()]
            elif ("lidar" in name or name.startswith("ibeo")) and elapsed_ms % dev.getSamplingPeriod() == 0:
                n = dev.getHorizontalResolution() * dev.getNumberOfLayers()
                raw = ctypes.string_at(wb.wb_lidar_get_range_image(dev._tag), n * 4)
                sensors[name] = {"offset": dump(name, "range", raw), "n": n, "w": dev.getHorizontalResolution(),
                                 "layers": dev.getNumberOfLayers(), "fov": dev.getFov(),
                                 "vfov": dev.getVerticalFov(), "max": dev.getMaxRange()}
                if case.get("point_cloud"):
                    pc = ctypes.string_at(wb.wb_lidar_get_point_cloud(dev._tag), n * 20)
                    sensors[name]["pc_offset"] = dump(name + " pc", "pc", pc)
            elif "camera" in name and elapsed_ms % dev.getSamplingPeriod() == 0 and case.get("save_images"):
                path = os.path.join(out_dir, "%s_%s_%05d.png" % (case["id"], name.replace(" ", "_"), elapsed_ms))
                dev.saveImage(path, 90)
                sensors[name] = os.path.basename(path)
        emit(type="sample", case=case["id"], t=now, k=k, gt_pre=pre, gt=gt,
             gps=[round(v, 4) for v in gps.getValues()], gps_speed=[round(v, 4) for v in gps.getSpeedVector()],
             imu_yaw=round(imu.getRollPitchYaw()[2], 6), sensors=sensors)
    step_wall.sort()
    emit(type="case_end", id=case["id"], wall_s=round(time.perf_counter() - wall0, 4), steps=steps,
         step_ms_p50=round(1000 * step_wall[len(step_wall) // 2], 3),
         step_ms_p95=round(1000 * step_wall[int(len(step_wall) * 0.95)], 3))
    for name in enabled:
        dev = device(name)
        if case.get("point_cloud") and ("lidar" in name or name.startswith("ibeo")):
            dev.disablePointCloud()
        dev.disable()


emit(type="start", world="roadsense_calibration.wbt", basic_time_step=STEP, plan=plan.get("name"))
for case in plan["cases"]:
    run_case(case)
emit(type="end")
log.close()
for f in binfiles.values():
    f.close()
driver.simulationQuit(0)
step()
