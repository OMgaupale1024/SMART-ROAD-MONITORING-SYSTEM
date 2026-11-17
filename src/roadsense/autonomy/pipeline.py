"""Turn one highway sensor frame into vehicle predictions and a driving recommendation."""

from typing import NamedTuple

from . import hazard_map, hazards, live_state, perception, prediction, risk, safety, telemetry, tracking

SENSOR_SCHEMA = "roadsense.sensors.v1"


class Cycle(NamedTuple):
    timestamp: float
    ego: dict
    pose: tuple
    reports: list
    assessments: list
    hazards: dict
    decision: dict
    discoveries: list

    def snapshot(self, run_id, sequence, active=True):
        return live_state.build(self.timestamp, self.ego, self.reports, self.assessments, self.hazards,
                                self.decision, self.pose, run_id, sequence, active)


class Pipeline:
    def __init__(self, vehicle_id, policy=None):
        self.tracker = tracking.Tracker()
        self.hazard_map = hazard_map.HazardMap(vehicle_id)
        self.policy = safety.Policy(traffic=perception.TRAFFIC) if policy is None else policy
        self.previous = None

    def process(self, frame):
        t, ego = frame["timestamp"], frame["ego"]
        pose = (*ego["position"][:2], ego["yaw_rad"])
        speed = ego["speed_mps"]
        record = telemetry.make_record(t, ego["vehicle_id"], ego["position"], speed, pose[2],
                                       ego["steering_rad"], self.previous, ego["maneuver"], ego["lane"])
        self.previous = (t, speed)
        detections = []
        for name in perception.RADARS:
            radar = frame["radars"][name]
            detections.extend(perception.radar_detections(name, radar["targets"], radar["limits"], t, pose))
        self.tracker.update(t, detections, pose=pose, ego_velocity=ego["velocity"])
        reports = [perception.track_report(track, t, pose, ego["velocity"]) for track in self.tracker.confirmed]
        assessments = [risk.assess(report, prediction.predict(report)) for report in reports]
        discoveries = self.hazard_map.update(hazards.simulated_detections(frame["objects"], t, pose))
        mapped = self.hazard_map.snapshot(t, pose)
        lanes = adjacent_lanes(pose[1], ego["maneuver"], frame["sides"])
        decision = safety.decide(t, speed, assessments, reports, mapped, lanes, self.policy)
        return Cycle(t, record, pose, reports, assessments, mapped, decision, discoveries)


def adjacent_lanes(world_y, maneuver, sides):
    lane = perception.lane_index(world_y)
    if lane not in range(perception.DRIVING_LANES) or maneuver != "lane_keep":
        return []
    centre = perception.CARRIAGEWAY_Y[0] + (lane + 0.5) * perception.LANE_WIDTH_M
    if abs(world_y - centre) >= 0.6:
        return []
    # The side sensors cover the strip alongside the car where the front and rear radars can miss it.
    candidates = []
    step = perception.right_step()
    for side, offset in (("left", -step), ("right", step)):
        if lane + offset not in range(perception.DRIVING_LANES):
            continue
        if sides is not None:
            value, maximum = sides[side]
            if value is None or maximum is None or not 0 < maximum or not 0.8 * maximum < value <= maximum:
                continue
        candidates.append(side + "_lane")
    return candidates
