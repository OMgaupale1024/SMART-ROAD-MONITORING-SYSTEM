"""EGO_ROADSENSE nearby-vehicle perception: radar targets to detections, ego/world frames, lanes, track reports.

Plain Python without Webots imports, so tests/ can run it (and Python 3.9, which Webots runs controllers with).
The ego frame has its origin at EGO_ROADSENSE's origin (the centre of the rear axle, where `telemetry gps` sits),
x (longitudinal) forward and y (lateral) left; bearings are in degrees, positive to the left. Distances in m, speeds
in m/s, times in s of simulation time.
"""
import math
from typing import NamedTuple, Optional

# The RoadSense radars on EGO_ROADSENSE in roadsense_highway.wbt: device name -> (source, ego-frame x, y, facing yaw)
RADARS = {
    "radar front": ("radar_front", 4.054, 0.0, 0.0),  # sensorsSlotFront (x 3.944) + 0.11, facing forward
    "radar rear": ("radar_rear", -1.16, 0.0, math.pi),  # sensorsSlotRear (x -1.06) - 0.1, facing backward
}
MAX_RANGE_RATE_MPS = 100.0  # a faster Webots range rate is an artefact: the first refresh, or a car SUMO teleports
EDGE_MARGIN_RAD = 0.005  # azimuths this close to the edge of the field of view are clamped: 5x the angular noise

# roadsense_highway.wbt only: EGO_ROADSENSE's carriageway (road "0") runs toward -x between world y = 1.25 (the
# median, on the driver's left) and y = 16.25, in four 3.75 m lanes. SUMO keeps the outer lane (y > 12.5) for
# pedestrians; its cars drive at y = 10.87, 7.03 and 3.18.
CARRIAGEWAY_Y = (1.25, 16.25)
CARRIAGEWAY_HEADING = math.pi  # EGO_ROADSENSE's direction of travel on it, toward -x
LANE_WIDTH_M = 3.75
DRIVING_LANES = 3  # lanes 0 to 2; SUMO keeps lane 3, the outer one, for pedestrians


class Detection(NamedTuple):
    """One radar return: a vehicle relative to the ego, plus its position on the ground for the tracker."""
    timestamp: float
    source: str  # "radar_front" or "radar_rear"
    longitudinal_m: float
    lateral_m: float
    distance_m: float  # from the ego origin
    bearing_deg: float
    range_rate_mps: Optional[float]  # radar-measured distance change rate, negative = closing; None if implausible
    world_x: float
    world_y: float


def ego_to_world(longitudinal, lateral, pose):
    """World (x, y) of an ego-frame point; pose is the ego's world (x, y, yaw)."""
    x, y, yaw = pose
    c, s = math.cos(yaw), math.sin(yaw)
    return x + longitudinal * c - lateral * s, y + longitudinal * s + lateral * c


def world_to_ego(world_x, world_y, pose):
    """Ego-frame (longitudinal, lateral) of a world point; pose is the ego's world (x, y, yaw)."""
    x, y, yaw = pose
    dx, dy = world_x - x, world_y - y
    c, s = math.cos(yaw), math.sin(yaw)
    return dx * c + dy * s, dy * c - dx * s


def radar_detections(name, targets, limits, t, pose):
    """Detections from one radar's targets, each (distance, azimuth, speed) as Webots reports them.

    Webots measures the distance to the centre of the target's bounding box and the azimuth, positive to the radar's
    right, to the target's origin. limits is the radar's (min range, max range, horizontal field of view). Webots
    clamps what it can't place, and those targets are dropped: the ego's own body to the min range, a vehicle
    reaching into range to the max range, a vehicle alongside whose origin is outside the field of view to its edge.
    """
    source, mount_x, mount_y, mount_yaw = RADARS[name]
    min_range, max_range, fov = limits
    detections = []
    for distance, azimuth, speed in targets:
        if not min_range < distance < max_range or abs(azimuth) > fov / 2 - EDGE_MARGIN_RAD:
            continue
        a = mount_yaw - azimuth
        lon, lat = mount_x + distance * math.cos(a), mount_y + distance * math.sin(a)
        range_rate = speed if math.isfinite(speed) and abs(speed) <= MAX_RANGE_RATE_MPS else None
        detections.append(Detection(t, source, lon, lat, math.hypot(lon, lat), math.degrees(math.atan2(lat, lon)),
                                    range_rate, *ego_to_world(lon, lat, pose)))
    return detections


def lane_index(world_y):
    """Lane index on EGO_ROADSENSE's carriageway, 0 next to the median and counting rightwards; None off it."""
    low, high = CARRIAGEWAY_Y
    return int((world_y - low) // LANE_WIDTH_M) if low <= world_y < high else None


def lane_relation(target_world_y, ego_world_y):
    """ego_lane, left_lane, right_lane, or other (further away, off the carriageway, or oncoming)."""
    target, ego = lane_index(target_world_y), lane_index(ego_world_y)
    if target is None or ego is None:
        return "other"
    return {0: "ego_lane", -1: "left_lane", 1: "right_lane"}.get(target - ego, "other")


def track_report(track, t, pose, ego_velocity):
    """The ego-centric record of a confirmed track at time t; ego_velocity is the ego's world (vx, vy), from GPS."""
    world_x, world_y = track.position_at(t)
    lon, lat = world_to_ego(world_x, world_y, pose)
    # the vehicle's velocity over the ground minus the ego's, along the ego's axes
    v_lon, v_lat = world_to_ego(track.velocity[0] - ego_velocity[0], track.velocity[1] - ego_velocity[1],
                                (0.0, 0.0, pose[2]))
    return {
        "track_id": track.track_id,
        "object_type": "vehicle",  # in this world only vehicles have a radar cross-section
        "timestamp": round(t, 3),
        "relative_position": {"longitudinal_m": round(lon, 2), "lateral_m": round(lat, 2)},
        "distance_m": round(math.hypot(lon, lat), 2),
        "bearing_deg": round(math.degrees(math.atan2(lat, lon)), 1),
        "relative_velocity": {"longitudinal_mps": round(v_lon, 2), "lateral_mps": round(v_lat, 2)},
        "relative_speed_mps": None if track.range_rate is None else round(track.range_rate, 2),
        "lane_relation": lane_relation(world_y, pose[1]),
        "source": track.source,
        "age_s": round(t - track.first_seen, 2),
        "last_seen": round(track.last_seen, 3),
    }


def format_summary(t, reports):
    """Console lines: a summary, then one line per track, nearest first."""
    reports = sorted(reports, key=lambda r: r["distance_m"])
    nearest = "%s@%.1fm" % (reports[0]["track_id"], reports[0]["distance_m"]) if reports else "none"
    lines = ["[RoadSense:PERCEPTION] t=%.1fs tracks=%d nearest=%s" % (t, len(reports), nearest)]
    for r in reports:
        p, v, rate = r["relative_position"], r["relative_velocity"], r["relative_speed_mps"]
        line = "  %s %-6s %5.1fm lon=%+.1f lat=%+.1f rel_vel=(%+.1f,%+.1f)m/s rel_speed=%s %s %s age=%.1fs" % (
            r["track_id"], "ahead" if p["longitudinal_m"] >= 0 else "behind", r["distance_m"], p["longitudinal_m"],
            p["lateral_m"], v["longitudinal_mps"], v["lateral_mps"], "n/a" if rate is None else "%+.1fm/s" % rate,
            r["lane_relation"], r["source"], r["age_s"])
        if t - r["last_seen"] > 1e-6:
            line += " unseen=%.1fs" % (t - r["last_seen"])
        lines.append(line)
    return "\n".join(lines)
