"""Unified recommendations from RoadSense records; no sensing, rendering or actuation.

Academic baseline, not certified braking logic. Python 3.9, plain JSON-compatible dicts.
Malformed required inputs raise ValueError rather than silently reporting SAFE. The caller
supplies current records and known adjacent driving lanes; missing lane knowledge disables
lane suggestions. Persistent map records are never changed.
"""
import math
from enum import Enum
from typing import NamedTuple

import risk


class Action(str, Enum):
    MAINTAIN = "MAINTAIN"
    MONITOR = "MONITOR"
    SLOW_DOWN = "SLOW_DOWN"
    BRAKE = "BRAKE"
    EMERGENCY_BRAKE = "EMERGENCY_BRAKE"
    CONSIDER_LANE_CHANGE = "CONSIDER_LANE_CHANGE"


class Policy(NamedTuple):
    nominal_speed_kmh: float = 80.0
    reaction_time_s: float = 1.0
    comfortable_deceleration_mps2: float = 3.0
    strong_deceleration_mps2: float = 6.0
    lookahead_s: float = 8.0
    minimum_lookahead_m: float = 30.0
    minimum_confidence: float = 0.6
    lane_change_time_s: float = 4.0
    lane_clearance_m: float = 15.0
    lane_width_m: float = 3.75


DEFAULT_POLICY = Policy()
# Assumed crossing speeds as fractions of nominal; not measurements of a pothole's safe speed.
CROSSING_FRACTIONS = {"LOW": 0.7, "MEDIUM": 0.45, "HIGH": 0.2}
LANES = ("left_lane", "right_lane")
HAZARD_LANES = ("ego_lane",) + LANES + ("other_lane", "off_road", "unknown")


def _number(value, name, low=0.0, high=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError("%s must be a finite number in [%s, %s]" % (name, low, high))
    return value


def _record(value):
    if not isinstance(value, dict):
        raise ValueError("expected a RoadSense record")
    return value


def _identifier(value):
    if not isinstance(value, str) or not value or not value.isidentifier():
        raise ValueError("expected a nonempty RoadSense identifier")
    return value


def _choice(value, choices):
    if value not in choices:
        raise ValueError("unknown value: %r" % (value,))
    return value


def _free_lane(t, speed, reports, hazards, adjacent_lanes, policy):
    """Reject lanes intersecting a conservative swept traffic box over the maneuver window."""
    gap = max(policy.lane_clearance_m, speed * 2.0)
    for lane in LANES:
        if lane not in adjacent_lanes:
            continue
        if any(h["lane_relation"] == lane for h in hazards):
            continue
        centre = policy.lane_width_m * (1 if lane == "left_lane" else -1)
        blocked = False
        for r in reports:
            # shortcut: tracked traffic cannot prove a blind spot clear; require independent sensing before control.
            if t - r["last_seen"] > 0.5:
                blocked = True
                break
            p, v = r["relative_position"], r["relative_velocity"]
            x, y = p["longitudinal_m"], p["lateral_m"]
            x1 = x + v["longitudinal_mps"] * policy.lane_change_time_s
            y1 = y + v["lateral_mps"] * policy.lane_change_time_s
            margin = risk.TARGET_HALF_WIDTH_M + risk.EGO_HALF_WIDTH_M + risk.SAFETY_BUFFER_M
            if (min(x, x1) <= gap and max(x, x1) >= -gap
                    and min(y, y1) <= max(0, centre) + margin
                    and max(y, y1) >= min(0, centre) - margin):
                blocked = True
                break
        if not blocked:
            return lane
    return None


def _hazard(h, speed, policy):
    r = h["relative"]
    lon, lane = r["longitudinal_m"], r["lane_relation"]
    if (h["status"] != "ACTIVE" or h["type"] != "pothole" or lon <= 0
            or r["direction"] != "ahead" or lane not in ("ego_lane",) + LANES
            or lon > max(policy.minimum_lookahead_m, speed * policy.lookahead_s)):
        return None
    level, action, target = "CAUTION", Action.MONITOR, policy.nominal_speed_kmh
    arrival = lon / speed if speed > 0.01 else None
    reason = "%s %s pothole %.1f m ahead in %s" % (h["hazard_id"], h["severity"], lon, lane)
    if lane != "ego_lane" or h["confidence"] < policy.minimum_confidence:
        reason += "; monitor only" + (" (low confidence)" if h["confidence"] < policy.minimum_confidence else "")
    else:
        crossing = policy.nominal_speed_kmh * CROSSING_FRACTIONS[h["severity"]] / 3.6
        a, reaction = policy.comfortable_deceleration_mps2, policy.reaction_time_s
        reduction2 = max(0.0, speed * speed - crossing * crossing)
        comfortable = speed * reaction + reduction2 / (2 * a)
        strong = speed * reaction + reduction2 / (2 * policy.strong_deceleration_mps2)
        # Solve d = u*reaction + (u² - crossing²)/(2*a) for the approach speed u.
        approach = max(crossing, math.sqrt((a * reaction) ** 2 + crossing ** 2 + 2 * a * lon) - a * reaction)
        target = min(policy.nominal_speed_kmh, speed * 3.6, approach * 3.6)
        if speed > crossing and lon <= comfortable:
            level = "HIGH" if h["severity"] != "LOW" else "CAUTION"
            action = Action.BRAKE if lon <= strong and level == "HIGH" else Action.SLOW_DOWN
        elif target < speed * 3.6:
            action = Action.SLOW_DOWN
    return {"hazard_id": h["hazard_id"], "type": h["type"], "severity": h["severity"],
            "confidence": h["confidence"], "distance_m": r["distance_m"], "longitudinal_m": lon,
            "lateral_m": r["lateral_m"], "lane_relation": lane, "urgency": level,
            "time_to_hazard_s": arrival, "recommended_action": action.value,
            "recommended_speed_kmh": min(policy.nominal_speed_kmh, round(target, 1)), "reason": reason}


def decide(t, ego_speed_mps, assessments, reports, hazard_snapshot, adjacent_lanes=(), policy=DEFAULT_POLICY):
    """Return UnifiedSafetyState from risk.assess, perception.track_report and HazardMap.snapshot.

    Current, mutually consistent inputs are required. adjacent_lanes lists known drivable lanes
    relative to a lane-centred ego (default unknown). Raises ValueError on malformed inputs.
    """
    _number(t, "timestamp")
    _number(ego_speed_mps, "ego speed", high=150)
    if not isinstance(policy, Policy):
        raise ValueError("expected Policy")
    for name, value in policy._asdict().items():
        _number(value, name, 0.001, 300)
    _number(policy.minimum_confidence, "minimum confidence", 0, 1)
    if policy.strong_deceleration_mps2 < policy.comfortable_deceleration_mps2:
        raise ValueError("strong deceleration must be at least comfortable deceleration")
    for records in (assessments, reports, _record(hazard_snapshot).get("hazards"), adjacent_lanes):
        if not isinstance(records, (list, tuple)):
            raise ValueError("expected a list or tuple of records/lanes")
    for lane in adjacent_lanes:
        _choice(lane, LANES)
    for a in assessments:
        _identifier(_record(a).get("track_id"))
        _choice(a.get("risk"), risk.LEVELS)
        if not isinstance(a.get("conflict"), bool) or a["conflict"] != (a["risk"] != "SAFE"):
            raise ValueError("inconsistent vehicle risk/conflict")
        if a["conflict"]:
            _number(a.get("ttc_s"), "TTC")
        elif a.get("ttc_s") is not None:
            raise ValueError("SAFE track must have null TTC")
    for r in reports:
        _identifier(_record(r).get("track_id"))
        _number(r.get("last_seen"), "last seen", high=t)
        for field, keys in (("relative_position", ("longitudinal_m", "lateral_m")),
                            ("relative_velocity", ("longitudinal_mps", "lateral_mps"))):
            for key in keys:
                _number(_record(r.get(field)).get(key), key, -1e6, 1e6)
    hazards = hazard_snapshot["hazards"]
    for h in hazards:
        _identifier(_record(h).get("hazard_id"))
        _identifier(h.get("type"))
        _choice(h.get("severity"), tuple(CROSSING_FRACTIONS))
        _choice(h.get("status"), ("ACTIVE", "INACTIVE"))
        _number(h.get("confidence"), "confidence", high=1)
        r = _record(h.get("relative"))
        _choice(r.get("lane_relation"), HAZARD_LANES)
        _choice(r.get("direction"), ("ahead", "behind"))
        for key in ("longitudinal_m", "lateral_m", "distance_m"):
            _number(r.get(key), key, 0 if key == "distance_m" else -1e9)
    vehicle = risk.ego_safety(t, assessments)
    threats = [v for v in (_hazard(h, ego_speed_mps, policy) for h in hazards) if v is not None]
    threats.sort(key=lambda h: (h["distance_m"], h["hazard_id"]))
    road = max(threats, key=lambda h: (risk.LEVELS.index(h["urgency"]),
                                     -h["recommended_speed_kmh"], h["lane_relation"] == "ego_lane",
                                     -h["longitudinal_m"]), default=None)
    level, action, target, primary, lane = "SAFE", Action.MAINTAIN, policy.nominal_speed_kmh, None, None
    reason = "No current vehicle conflict or relevant road hazard"
    # HIGH/CRITICAL collision predictions (normally <=3 s) override potholes, including young tracks.
    vehicle_first = vehicle["overall_risk"] in ("HIGH", "CRITICAL") or (
        vehicle["overall_risk"] == "CAUTION" and (road is None or road["urgency"] != "HIGH"))
    if vehicle_first:
        worst = next(a for a in assessments if a["track_id"] == vehicle["most_critical_track"])
        level = worst["risk"]
        action = {"CAUTION": Action.SLOW_DOWN, "HIGH": Action.BRAKE, "CRITICAL": Action.EMERGENCY_BRAKE}[level]
        decel = policy.strong_deceleration_mps2 if level == "HIGH" else policy.comfortable_deceleration_mps2
        target = 0.0 if level == "CRITICAL" else min(policy.nominal_speed_kmh,
            max(0.0, ego_speed_mps - decel * max(policy.reaction_time_s, risk.HIGH_TTC_S - worst["ttc_s"])) * 3.6)
        reason = "%s predicted collision in %.2f s" % (worst["track_id"], worst["ttc_s"])
        primary = {"type": "vehicle", "id": worst["track_id"], "reason": reason}
    elif road:
        level, action, target, reason = (road["urgency"], Action(road["recommended_action"]),
                                       road["recommended_speed_kmh"], road["reason"])
        if (vehicle["overall_risk"] == "SAFE" and road["lane_relation"] == "ego_lane"
                and road["severity"] != "LOW" and road["confidence"] >= policy.minimum_confidence
                and road["time_to_hazard_s"] is not None
                and road["time_to_hazard_s"] > policy.lane_change_time_s and action != Action.BRAKE):
            lane = _free_lane(t, ego_speed_mps, reports, threats, adjacent_lanes, policy)
            if lane:
                action = Action.CONSIDER_LANE_CHANGE
                reason += "; consider %s, tracked corridor clear" % lane
        primary = {"type": "road_hazard", "id": road["hazard_id"], "reason": reason}
    # A secondary pothole can tighten the speed cap, but never replace the collision action.
    primary_target = target
    target = min([target] + [h["recommended_speed_kmh"] for h in threats])
    if primary and target < primary_target:
        reason += "; target also bounded by current threats"
        primary["reason"] = reason
    return {"timestamp": round(t, 3), "overall_risk": level, "primary_threat": primary,
            "recommended_action": action.value,
            "recommended_speed_kmh": min(policy.nominal_speed_kmh, round(target, 1)),
            "recommended_lane": lane, "reason": reason, "vehicle_safety": vehicle,
            "road_safety": {"relevant_hazards": len(threats),
                            "nearest_hazard": threats[0]["hazard_id"] if threats else None,
                            "hazards": threats}}


def format_decision(state):
    """One console line; the controller owns the 1 Hz schedule."""
    return "[RoadSense:DECISION] t=%.1fs risk=%s action=%s target=%.1fkm/h threat=%s reason=%s" % (
        state["timestamp"], state["overall_risk"], state["recommended_action"], state["recommended_speed_kmh"],
        state["primary_threat"]["id"] if state["primary_threat"] else "none", state["reason"])
