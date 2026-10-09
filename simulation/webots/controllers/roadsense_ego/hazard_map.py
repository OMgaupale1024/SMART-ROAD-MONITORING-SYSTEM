"""EGO_ROADSENSE's local road-hazard map: hazards kept once detected, with RoadSense ids, queries and a snapshot.

Plain Python without Webots imports (and Python 3.9). It takes hazards.HazardDetection records from any source.
Hazards live on the ground, in the world frame, and stay in the map after EGO_ROADSENSE has passed them. Where a hazard
is relative to EGO_ROADSENSE (ego frame: x ahead, y left, origin at the rear axle; lane) is worked out from the ego
pose when asked. Positions in m, times in s of simulation time.
"""
import collections
import math

import perception

MERGE_RADIUS_M = 2.0  # a detection this close to a hazard of its type is that hazard; lanes are 3.75 m apart
# Each consistent observation closes CONFIDENCE_GAIN x its own confidence of the gap to certainty, up to
# MAX_CONFIDENCE: 0.90 after one 0.9 detection, 0.909 after two, ~0.97 after the dozen a pothole gets passing it.
CONFIDENCE_GAIN = 0.1
MAX_CONFIDENCE = 0.99
SEVERITIES = ("LOW", "MEDIUM", "HIGH")  # road-hazard severity, unrelated to risk.py's collision-risk levels
ID_PREFIXES = {"pothole": "PH"}  # other hazard types are numbered HZ_001, ...


def lane_relation(hazard_world_y, ego_world_y):
    """ego_lane, left_lane, right_lane, other_lane (further over), off_road (off EGO_ROADSENSE's carriageway) or
    unknown (EGO_ROADSENSE off it). Straight carriageway only, like perception.lane_relation."""
    hazard, ego = perception.lane_index(hazard_world_y), perception.lane_index(ego_world_y)
    if ego is None:
        return "unknown"
    if hazard is None:
        return "off_road"
    return {0: "ego_lane", -1: "left_lane", 1: "right_lane"}.get(hazard - ego, "other_lane")


def relative(world_x, world_y, pose):
    """Where a point on the ground is relative to EGO_ROADSENSE; pose is the ego's world (x, y, yaw)."""
    lon, lat = perception.world_to_ego(world_x, world_y, pose)
    return {
        "longitudinal_m": round(lon, 2),
        "lateral_m": round(lat, 2),
        "distance_m": round(math.hypot(lon, lat), 2),
        "bearing_deg": round(math.degrees(math.atan2(lat, lon)), 1),
        "lane_relation": lane_relation(world_y, pose[1]),
        "direction": "ahead" if lon >= 0 else "behind",
    }


def _valid(d):
    if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (d.world_x, d.world_y, d.confidence)):
        return False
    return bool(d.hazard_type) and d.severity in SEVERITIES and 0.0 <= d.confidence <= 1.0


class HazardMap:
    """The road hazards one vehicle has found; call update with each sensor cycle's detections."""

    def __init__(self, source_vehicle):
        self.source_vehicle = source_vehicle
        self._hazards = {}  # hazard_id -> record, in discovery order
        self._numbered = collections.Counter()  # hazards created per id prefix

    def update(self, detections):
        """Merge detections: each joins the nearest hazard of its type within MERGE_RADIUS_M, or becomes a new
        hazard. Malformed detections are skipped. Returns the new hazards' records."""
        new = []
        for d in filter(_valid, detections):
            gap, hazard = min(((math.hypot(h["world_position"]["x"] - d.world_x, h["world_position"]["y"] - d.world_y), h)
                               for h in self._hazards.values() if h["type"] == d.hazard_type),
                              key=lambda gh: gh[0], default=(None, None))
            if hazard is None or gap > MERGE_RADIUS_M:
                new.append(self._add(d))
            else:
                self._observe(hazard, d)
        return new

    def _add(self, d):
        prefix = ID_PREFIXES.get(d.hazard_type, "HZ")
        self._numbered[prefix] += 1
        record = {
            "hazard_id": "%s_%03d" % (prefix, self._numbered[prefix]),
            "type": d.hazard_type,
            "world_position": {"x": d.world_x, "y": d.world_y},
            "severity": d.severity,
            "confidence": d.confidence,
            "dimensions": dict(d.dimensions) if d.dimensions else None,
            "first_seen_s": round(d.timestamp, 3),
            "last_seen_s": round(d.timestamp, 3),
            "observation_count": 1,
            "source_vehicle": self.source_vehicle,
            "source": d.source,
            "status": "ACTIVE",  # nothing expires a hazard yet
        }
        self._hazards[record["hazard_id"]] = record
        return record

    @staticmethod
    def _observe(hazard, d):
        """A further detection of the hazard: the position is the mean of all, the severity the highest reported."""
        n = hazard["observation_count"] + 1
        position = hazard["world_position"]
        position["x"] += (d.world_x - position["x"]) / n
        position["y"] += (d.world_y - position["y"]) / n
        hazard["observation_count"] = n
        hazard["last_seen_s"] = max(hazard["last_seen_s"], round(d.timestamp, 3))
        hazard["confidence"] = min(MAX_CONFIDENCE,
                                   1.0 - (1.0 - hazard["confidence"]) * (1.0 - CONFIDENCE_GAIN * d.confidence))
        hazard["severity"] = max(hazard["severity"], d.severity, key=SEVERITIES.index)
        if d.dimensions:
            hazard["dimensions"] = dict(d.dimensions)
        hazard["source"] = d.source

    def get(self, hazard_id):
        """The hazard's record, or None."""
        return self._hazards.get(hazard_id)

    def active_hazards(self):
        """All the hazards' records, in discovery order."""
        return [h for h in self._hazards.values() if h["status"] == "ACTIVE"]

    @staticmethod
    def _view(hazard, pose):
        """A copy of the record, rounded, with where the hazard is relative to EGO_ROADSENSE."""
        p = hazard["world_position"]
        return dict(hazard, world_position={"x": round(p["x"], 2), "y": round(p["y"], 2)},
                    confidence=round(hazard["confidence"], 3),
                    dimensions=dict(hazard["dimensions"]) if hazard["dimensions"] else None,
                    relative=relative(p["x"], p["y"], pose))

    def nearest_hazard(self, pose):
        """The nearest hazard, ahead or behind, with its relative state; None if the map is empty."""
        return min((self._view(h, pose) for h in self.active_hazards()), key=lambda v: v["relative"]["distance_m"],
                   default=None)

    def hazards_ahead(self, pose, max_distance_m=None):
        """The hazards ahead, nearest first, with their relative state."""
        views = [self._view(h, pose) for h in self.active_hazards()]
        return sorted((v for v in views if v["relative"]["direction"] == "ahead"
                       and (max_distance_m is None or v["relative"]["distance_m"] <= max_distance_m)),
                      key=lambda v: v["relative"]["distance_m"])

    def snapshot(self, t, pose):
        """The whole map at time t, every hazard with its relative state: plain dicts, lists and numbers (JSON)."""
        return {"timestamp": round(t, 3), "source_vehicle": self.source_vehicle,
                "hazards": [self._view(h, pose) for h in self.active_hazards()]}


def format_event(t, record, pose):
    """The console line for a newly found hazard."""
    r = relative(record["world_position"]["x"], record["world_position"]["y"], pose)
    return "[RoadSense:HAZARD_EVENT] t=%.1fs discovered %s type=%s severity=%s distance=%.1fm lane=%s conf=%.2f" % (
        t, record["hazard_id"], record["type"], record["severity"], r["distance_m"], r["lane_relation"],
        record["confidence"])


def format_summary(snapshot):
    """Console lines: the map's size and the nearest hazard ahead, then one line per hazard."""
    hazards = snapshot["hazards"]
    ahead = sorted((h for h in hazards if h["relative"]["direction"] == "ahead"),
                   key=lambda h: h["relative"]["distance_m"])
    nearest = "none" if not ahead else "%s@%.1fm severity=%s lane=%s" % (
        ahead[0]["hazard_id"], ahead[0]["relative"]["distance_m"], ahead[0]["severity"],
        ahead[0]["relative"]["lane_relation"])
    lines = ["[RoadSense:HAZARD] t=%.1fs map=%d ahead=%d nearest_ahead=%s" % (
        snapshot["timestamp"], len(hazards), len(ahead), nearest)]
    for h in hazards:
        r = h["relative"]
        lines.append("  %s %s %-6s %5.1fm %-10s %-6s conf=%.2f obs=%d" % (
            h["hazard_id"], h["type"], r["direction"], r["distance_m"], r["lane_relation"], h["severity"],
            h["confidence"], h["observation_count"]))
    return "\n".join(lines)
