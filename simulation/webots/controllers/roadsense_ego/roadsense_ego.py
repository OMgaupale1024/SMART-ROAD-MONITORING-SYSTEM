# Copyright 1996-2025 Cyberbotics Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified for RoadSense (2026): copied from the Webots R2025b highway_overtake sample controller
# and renamed; the driving logic is unchanged. Added EGO_ROADSENSE telemetry (telemetry GPS and
# inertial unit, printed once per simulated second); removed the unused camera lookup. Added
# nearby-vehicle perception: two radars, tracked every radar cycle, printed once per simulated second.
# Added trajectory prediction and collision risk for the tracks, every radar cycle, printed once per
# simulated second; the driving logic doesn't use them. Added road hazards: a hazard camera's object
# recognition, every radar cycle, into a local hazard map, printed once per simulated second and when a
# hazard is discovered; the driving logic doesn't use it either. Added unified safety recommendations
# every radar cycle, printed once per simulated second; no recommendations feed the driving logic.
# Added optional live-state publishing to the RoadSense web server (ROADSENSE_LIVE_PUBLISH=1), from a
# background thread that the control loop never waits for. ROADSENSE_EDGE_URL offloads the
# autonomy pipeline to an edge server by sending sensor readings from the same cycle.

"""RoadSense ego controller: the Webots highway_overtake driving logic plus local telemetry, perception, risk,
road hazards, safety recommendations and an optional live-state stream."""

import os
import sys
from pathlib import Path

from vehicle import Driver

# Webots uses its own controller interpreter. The shared autonomy package is pure Python.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
from roadsense.autonomy import hazard_map, live_publisher, live_state, perception, pipeline, risk, safety, telemetry

TELEMETRY_PERIOD_S = 1.0  # simulated seconds between telemetry lines
PERCEPTION_PERIOD_S = 1.0  # simulated seconds between perception summaries
# Radar sampling period, the SUMO step: SUMO moves its cars every 200 ms, and a Webots radar computes the
# range rate from the movement between two refreshes, so a shorter period sees cars stand still, then jump.
RADAR_PERIOD_MS = 200

sensorsNames = [
    "front",
    "front right 0",
    "front right 1",
    "front right 2",
    "front left 0",
    "front left 1",
    "front left 2",
    "rear",
    "rear left",
    "rear right",
    "right",
    "left"]
sensors = {}

# Road profile (ROADSENSE_ROAD_PROFILE, perception.ROAD_PROFILES): "highway_lht" drives the same lanes toward +x,
# left-hand traffic. The sample's driving logic below is unchanged; for left-hand traffic its steering is mirrored
# and its side sensors swapped, so that "toward the median" (its overtaking side) stays right for the car.
LEFT_HAND = perception.TRAFFIC == "left"
STEERING_SIGN = -1 if LEFT_HAND else 1


def sample_side(name):
    """The device behind a sample sensor name: left and right swapped in left-hand traffic."""
    if not LEFT_HAND:
        return name
    return name.replace("left", "\0").replace("right", "left").replace("\0", "right")


lanePositions = [10.6, 6.875, 3.2]
currentLane = 1
overtakingSide = None
maxSpeed = float(os.environ.get("ROADSENSE_EGO_SPEED_KMH", "80"))
if not 20 <= maxSpeed <= 90:
    raise ValueError("experiment ego speed must be 20–90 km/h")
safeOvertake = False


def apply_PID(position, targetPosition):
    """Apply the PID controller and return the angle command."""
    p_coefficient = 0.05
    i_coefficient = 0.000015
    d_coefficient = 25
    diff = position - targetPosition
    if apply_PID.previousDiff is None:
        apply_PID.previousDiff = diff
    # anti-windup mechanism
    if diff > 0 and apply_PID.previousDiff < 0:
        apply_PID.integral = 0
    if diff < 0 and apply_PID.previousDiff > 0:
        apply_PID.integral = 0
    apply_PID.integral += diff
    # compute angle
    angle = p_coefficient * diff + i_coefficient * apply_PID.integral + d_coefficient * (diff - apply_PID.previousDiff)
    apply_PID.previousDiff = diff
    return STEERING_SIGN * angle


apply_PID.integral = 0
apply_PID.previousDiff = None


def get_filtered_speed(speed):
    """Filter the speed command to avoid abrupt speed changes."""
    get_filtered_speed.previousSpeeds.append(speed)
    if len(get_filtered_speed.previousSpeeds) > 100:  # keep only 80 values
        get_filtered_speed.previousSpeeds.pop(0)
    return sum(get_filtered_speed.previousSpeeds) / float(len(get_filtered_speed.previousSpeeds))


def is_vehicle_on_side(side):
    """Check (using the 3 appropriated front distance sensors) if there is a car in front."""
    for i in range(3):
        name = "front " + side + " " + str(i)
        if sensors[name].getValue() > 0.8 * sensors[name].getMaxValue():
            return True
    return False


def reduce_speed_if_vehicle_on_side(speed, side):
    """Reduce the speed if there is some vehicle on the side given in argument."""
    minRatio = 1
    for i in range(3):
        name = "front " + overtakingSide + " " + str(i)
        ratio = sensors[name].getValue() / sensors[name].getMaxValue()
        if ratio < minRatio:
            minRatio = ratio
    return minRatio * speed


get_filtered_speed.previousSpeeds = []
driver = Driver()
for name in sensorsNames:
    sensors[name] = driver.getDevice("distance sensor " + sample_side(name))
    sensors[name].enable(10)

gps = driver.getDevice("gps")
gps.enable(10)

# telemetry sensors: a GPS at the car's origin (the "gps" above is a look-ahead point for steering)
timestep = int(driver.getBasicTimeStep())
telemetryGps = driver.getDevice("telemetry gps")
telemetryGps.enable(timestep)
inertialUnit = driver.getDevice("inertial unit")
inertialUnit.enable(timestep)
nextTelemetry = TELEMETRY_PERIOD_S
previousTelemetry = None  # (timestamp, speed) of the last telemetry record

# perception: the RoadSense radars (front and rear), tracked in the world frame placed by the telemetry sensors
radars = {name: driver.getDevice(name) for name in perception.RADARS}
for radar in radars.values():
    radar.enable(RADAR_PERIOD_MS)
nextRadarCycle = RADAR_PERIOD_MS / 1000.0
nextPerception = PERCEPTION_PERIOD_S
# road hazards: the hazard camera's object recognition (simulated pothole sensing), read every radar cycle
hazardCamera = driver.getDevice("hazard camera")
hazardCamera.recognitionEnable(RADAR_PERIOD_MS)
# The publisher sends raw readings in edge mode and precomputed snapshots in the standalone demo.
liveConfig = live_publisher.config_from_environment()
livePublisher = live_publisher.LivePublisher(liveConfig.url, liveConfig.timeout_s) if liveConfig else None
edgeMode = liveConfig is not None and liveConfig.edge
localPipeline = None if edgeMode else pipeline.Pipeline(
    driver.getName(), safety.Policy(nominal_speed_kmh=maxSpeed, traffic=perception.TRAFFIC))
# ROADSENSE_RUN_ID: a scenario generator names the run, so its ground truth joins the edge history on run id
liveRunId = os.environ.get("ROADSENSE_RUN_ID", "").strip()[:64] or live_state.new_run_id()
liveSequence, nextLive, liveState = 0, 0.0, None


while driver.step() != -1:
    # adjust speed according to front vehicle
    frontDistance = sensors["front"].getValue()
    frontRange = sensors["front"].getMaxValue()
    speed = maxSpeed * frontDistance / frontRange
    if sensors["front right 0"].getValue() < 8.0 or sensors["front left 0"].getValue() < 8.0:
        # another vehicle is currently changing lane in front of the vehicle => emergency braking
        speed = min(0.5 * maxSpeed, speed)
    if overtakingSide is not None:
        # check if overtaking should be aborted
        if overtakingSide == 'right' and sensors["left"].getValue() < 0.8 * sensors["left"].getMaxValue():
            overtakingSide = None
            currentLane -= 1
        elif overtakingSide == 'left' and sensors["right"].getValue() < 0.8 * sensors["right"].getMaxValue():
            overtakingSide = None
            currentLane += 1
        else:  # reduce the speed if the vehicle from previous lane is still in front
            speed2 = reduce_speed_if_vehicle_on_side(speed, overtakingSide)
            if speed2 < speed:
                speed = speed2
    speed = get_filtered_speed(speed)
    driver.setCruisingSpeed(speed)
    # brake if needed
    speedDiff = driver.getCurrentSpeed() - speed
    if speedDiff > 0:
        driver.setBrakeIntensity(min(speedDiff / speed, 1))
    else:
        driver.setBrakeIntensity(0)
    # car in front, try to overtake
    if frontDistance < 0.8 * frontRange and overtakingSide is None:
        if (is_vehicle_on_side("left") and
                (not safeOvertake or sensors["rear left"].getValue() > 0.8 * sensors["rear left"].getMaxValue()) and
                sensors["left"].getValue() > 0.8 * sensors["left"].getMaxValue() and
                currentLane < 2):
            currentLane += 1
            overtakingSide = 'right'
        elif (is_vehicle_on_side("right") and
                (not safeOvertake or sensors["rear right"].getValue() > 0.8 * sensors["rear right"].getMaxValue()) and
                sensors["right"].getValue() > 0.8 * sensors["right"].getMaxValue() and
                currentLane > 0):
            currentLane -= 1
            overtakingSide = 'left'
    # adjust steering to stay in the middle of the current lane
    position = gps.getValues()[1]
    angle = max(min(apply_PID(position, lanePositions[currentLane]), 0.5), -0.5)
    driver.setSteeringAngle(-angle)
    # check if overtaking is over
    if abs(position - lanePositions[currentLane]) < 1.5:  # the car is just in the lane
        overtakingSide = None
    # telemetry, once per TELEMETRY_PERIOD_S of simulated time
    now = driver.getTime()
    if now >= nextTelemetry - 1e-6:
        nextTelemetry += TELEMETRY_PERIOD_S
        groundSpeed = telemetryGps.getSpeed()
        record = telemetry.make_record(now, driver.getName(), telemetryGps.getValues(), groundSpeed,
                                       inertialUnit.getRollPitchYaw()[2], driver.getSteeringAngle(),
                                       previousTelemetry, "lane_keep" if overtakingSide is None else "lane_change",
                                       currentLane)
        previousTelemetry = (now, groundSpeed)
        print(telemetry.format_line(record))
    # One complete sensor frame per radar cycle. In edge mode only the server processes it.
    if now >= nextRadarCycle - 1e-6:
        nextRadarCycle += RADAR_PERIOD_MS / 1000.0
        frame = {
            "schema": pipeline.SENSOR_SCHEMA,
            "timestamp": round(now, 3),
            "ego": {
                "vehicle_id": driver.getName(), "position": telemetryGps.getValues(),
                "velocity": telemetryGps.getSpeedVector()[:2], "speed_mps": telemetryGps.getSpeed(),
                "yaw_rad": inertialUnit.getRollPitchYaw()[2], "steering_rad": driver.getSteeringAngle(),
                "maneuver": "lane_keep" if overtakingSide is None else "lane_change", "lane": currentLane,
            },
            "radars": {
                name: {
                    "targets": [(target.distance, target.azimuth, target.speed) for target in radar.getTargets()],
                    "limits": (radar.getMinRange(), radar.getMaxRange(), radar.getHorizontalFov()),
                }
                for name, radar in radars.items()
            },
            "objects": [(tuple(obj.position), obj.model) for obj in hazardCamera.getRecognitionObjects()],
            "sides": {side: (sensors[sample_side(side)].getValue(), sensors[sample_side(side)].getMaxValue())
                      for side in ("left", "right")}  # the car's physical sides
            if edgeMode else None,
        }
        if localPipeline is not None:
            cycle = localPipeline.process(frame)
            for hazard in cycle.discoveries:
                print(hazard_map.format_event(now, hazard, cycle.pose))
        if livePublisher and (edgeMode or now >= nextLive - 1e-6):
            nextLive += liveConfig.period_s
            liveSequence += 1
            if edgeMode:
                frame["simulation"] = {"run_id": liveRunId, "sequence": liveSequence, "active": True}
                liveState = frame
            else:
                liveState = cycle.snapshot(liveRunId, liveSequence)
            livePublisher.publish(live_state.encode(liveState))
    if localPipeline is not None and now >= nextPerception - 1e-6:
        nextPerception += PERCEPTION_PERIOD_S
        print(perception.format_summary(now, cycle.reports))
        print(risk.format_summary(now, cycle.reports, cycle.assessments))
        print(hazard_map.format_summary(cycle.hazards))
        print(safety.format_decision(cycle.decision))

# the simulation is over: tell the web server's clients, if it can be reached within a second
if livePublisher:
    if liveState:
        livePublisher.publish(live_state.encode(live_state.finished(liveState)))
    livePublisher.close()
