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
# background thread that the control loop never waits for.

"""RoadSense ego controller: the Webots highway_overtake driving logic plus local telemetry, perception, risk,
road hazards, safety recommendations and an optional live-state stream."""

from vehicle import Driver

import hazard_map
import hazards
import live_publisher
import live_state
import perception
import prediction
import risk
import safety
import telemetry
import tracking

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

lanePositions = [10.6, 6.875, 3.2]
currentLane = 1
overtakingSide = None
maxSpeed = 80
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
    return angle


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
    sensors[name] = driver.getDevice("distance sensor " + name)
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
tracker = tracking.Tracker()
nextRadarCycle = RADAR_PERIOD_MS / 1000.0
nextPerception = PERCEPTION_PERIOD_S
reports, assessments = [], []  # the confirmed tracks and their collision risk, as of the latest radar cycle
# road hazards: the hazard camera's object recognition (simulated pothole sensing), read every radar cycle
hazardCamera = driver.getDevice("hazard camera")
hazardCamera.recognitionEnable(RADAR_PERIOD_MS)
hazardMap = hazard_map.HazardMap(driver.getName())
safetyPolicy = safety.Policy(nominal_speed_kmh=maxSpeed)
# live state for the RoadSense web server, if ROADSENSE_LIVE_PUBLISH asks for it: one snapshot per publication
# period, sent by a background thread; the algorithms above never wait for the network
liveConfig = live_publisher.config_from_environment()
livePublisher = live_publisher.LivePublisher(liveConfig.url, liveConfig.timeout_s) if liveConfig else None
liveRunId, liveSequence, nextLive, previousLive, liveState = live_state.new_run_id(), 0, 0.0, None, None


def ego_pose():
    """EGO_ROADSENSE's world (x, y, yaw) from the telemetry GPS and the inertial unit."""
    x, y, _ = telemetryGps.getValues()
    return x, y, inertialUnit.getRollPitchYaw()[2]


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
    # perception, prediction, risk and road hazards every radar cycle, printed once per PERCEPTION_PERIOD_S of
    # simulated time
    # (a multiple of the radar period, so the print follows a radar cycle in the same step)
    if now >= nextRadarCycle - 1e-6:
        nextRadarCycle += RADAR_PERIOD_MS / 1000.0
        pose = ego_pose()
        detections = []
        for name, radar in radars.items():
            targets = [(target.distance, target.azimuth, target.speed) for target in radar.getTargets()]
            limits = (radar.getMinRange(), radar.getMaxRange(), radar.getHorizontalFov())
            detections += perception.radar_detections(name, targets, limits, now, pose)
        tracker.update(now, detections)
        egoVelocity = telemetryGps.getSpeedVector()[:2]
        reports = [perception.track_report(track, now, pose, egoVelocity) for track in tracker.confirmed]
        assessments = [risk.assess(report, prediction.predict(report)) for report in reports]
        objects = [(tuple(o.position), o.model) for o in hazardCamera.getRecognitionObjects()]
        for hazard in hazardMap.update(hazards.simulated_detections(objects, now, pose)):
            print(hazard_map.format_event(now, hazard, pose))
        hazardSnapshot = hazardMap.snapshot(now, pose)
        # This world's three driving lanes; lane 3 is reserved for pedestrians. No suggestion while merging.
        laneIndex = perception.lane_index(pose[1])
        adjacentLanes = []
        if laneIndex in (0, 1, 2) and overtakingSide is None:
            laneCentre = perception.CARRIAGEWAY_Y[0] + (laneIndex + 0.5) * perception.LANE_WIDTH_M
            if abs(pose[1] - laneCentre) < 0.6:
                if laneIndex > 0:
                    adjacentLanes.append("left_lane")
                if laneIndex < 2:
                    adjacentLanes.append("right_lane")
        unifiedSafetyState = safety.decide(now, telemetryGps.getSpeed(), assessments, reports,
                                           hazardSnapshot, adjacentLanes, safetyPolicy)
        if livePublisher and now >= nextLive - 1e-6:  # this cycle's records, as one snapshot
            nextLive += liveConfig.period_s
            liveSequence += 1
            liveSpeed = telemetryGps.getSpeed()
            liveEgo = telemetry.make_record(now, driver.getName(), telemetryGps.getValues(), liveSpeed, pose[2],
                                            driver.getSteeringAngle(), previousLive,
                                            "lane_keep" if overtakingSide is None else "lane_change", currentLane)
            previousLive = (now, liveSpeed)
            liveState = live_state.build(now, liveEgo, reports, assessments, hazardSnapshot, unifiedSafetyState,
                                         pose, liveRunId, liveSequence)
            livePublisher.publish(live_state.encode(liveState))
    if now >= nextPerception - 1e-6:
        nextPerception += PERCEPTION_PERIOD_S
        print(perception.format_summary(now, reports))
        print(risk.format_summary(now, reports, assessments))
        print(hazard_map.format_summary(hazardSnapshot))
        print(safety.format_decision(unifiedSafetyState))

# the simulation is over: tell the web server's clients, if it can be reached within a second
if livePublisher and liveState:
    livePublisher.publish(live_state.encode(live_state.finished(liveState)))
    livePublisher.close()
