"""Collision risk for EGO_ROADSENSE: conflicts, time to collision and risk levels from predicted trajectories.

Plain Python without Webots imports (and Python 3.9). It reads a track record (perception.track_report) and its
prediction (prediction.predict). The predicted path is the record's position, then the trajectory's points, joined by
straight lines, so another prediction model can replace the constant-velocity one. Positions are in the ego frame
(x ahead, y left, origin at the rear axle), in m; times in s from the record's time.

The safety envelope is EGO_ROADSENSE's body, grown by a target's assumed half-size and a safety buffer: a vehicle's
centre inside it means the two bodies are within the buffer of each other. A vehicle is in conflict when its predicted
path enters the envelope within the trajectory (5 s), and its TTC is the time until it does; for a vehicle ahead in
the lane, the bumper gap less the buffer over the closing speed. A vehicle whose path never enters (pulling away,
keeping pace, passing in another lane) has no TTC. The closest point of approach (CPA) is where the vehicle's centre
comes closest to the ego's centre within the trajectory.
"""
import math

LEVELS = ("SAFE", "CAUTION", "HIGH", "CRITICAL")

# EGO_ROADSENSE is the world's Lincoln MKZ: a 4.9 m x 1.8 m body box centred 1.44 m ahead of its origin.
EGO_CENTRE_M = 1.44
EGO_HALF_LENGTH_M = 2.45
EGO_HALF_WIDTH_M = 0.9
# A radar target is a point near the middle of a vehicle, without a size: assume a typical car, 5 m x 1.9 m. A bus or
# truck reaches up to ~7 m from its middle, so its conflicts come later than they should.
TARGET_HALF_LENGTH_M = 2.5
TARGET_HALF_WIDTH_M = 0.95
SAFETY_BUFFER_M = 0.25  # about the radar's range noise; tracks are within ~0.4 m, so a larger buffer flags passing cars

# Any conflict within the trajectory is CAUTION; sooner is HIGH, then CRITICAL.
HIGH_TTC_S = 3.0  # forward-collision warnings typically sound 2-3 s before impact
CRITICAL_TTC_S = 1.5  # the usual critical TTC in traffic-conflict studies
# A track seen for less than this is at most HIGH: its velocity, from under a second of detections, is about twice
# as noisy (σ ~0.9 m/s at 0.4-0.6 s old, ~0.4 m/s from 1 s on, against Webots' ground truth).
MATURE_AGE_S = 1.0


def _inside(r, v, half, dt):
    """The times in [0, dt] when r + v*s is within ±half, as (first, last); None if never."""
    if abs(v) < 1e-9:
        return (0.0, dt) if abs(r) <= half else None
    first, last = sorted(((-half - r) / v, (half - r) / v))
    first, last = max(first, 0.0), min(last, dt)
    return (first, last) if first <= last else None


def _level(ttc, age):
    if ttc is None:
        return "SAFE"
    if ttc <= CRITICAL_TTC_S and age >= MATURE_AGE_S:
        return "CRITICAL"
    return "HIGH" if ttc <= HIGH_TTC_S else "CAUTION"


def assess(report, prediction):
    """The track's safety record: conflict, TTC, closest point of approach and risk level, with its trajectory."""
    record = {"track_id": report["track_id"], "prediction_model": prediction["model"], "ttc_s": None,
              "time_to_cpa_s": None, "min_separation_m": None, "closing": None, "conflict": False, "risk": "SAFE",
              "trajectory": prediction["trajectory"]}
    if not prediction["trajectory"]:
        return record  # nothing predicted, nothing to rate
    half_length = EGO_HALF_LENGTH_M + TARGET_HALF_LENGTH_M + SAFETY_BUFFER_M
    half_width = EGO_HALF_WIDTH_M + TARGET_HALF_WIDTH_M + SAFETY_BUFFER_M
    # the vehicle's centre relative to the ego's centre: now, then at each trajectory point
    p = report["relative_position"]
    path = [(0.0, p["longitudinal_m"] - EGO_CENTRE_M, p["lateral_m"])] + [
        (q["t_s"], q["longitudinal_m"] - EGO_CENTRE_M, q["lateral_m"]) for q in prediction["trajectory"]]
    (_, x_now, y_now), (_, x_next, y_next) = path[:2]
    closing = x_now * (x_next - x_now) + y_now * (y_next - y_now) < 0  # the centres are getting closer
    ttc, cpa = None, None
    for (t0, x, y), (t1, x1, y1) in zip(path, path[1:]):
        dt = t1 - t0
        vx, vy = (x1 - x) / dt, (y1 - y) / dt
        if ttc is None:
            along, across = _inside(x, vx, half_length, dt), _inside(y, vy, half_width, dt)
            if along and across and max(along[0], across[0]) <= min(along[1], across[1]):
                ttc = round(t0 + max(along[0], across[0]), 2)
        speed2 = vx * vx + vy * vy
        s = 0.0 if speed2 < 1e-12 else min(max(-(x * vx + y * vy) / speed2, 0.0), dt)
        separation = math.hypot(x + vx * s, y + vy * s)
        if cpa is None or separation < cpa[1]:
            cpa = (t0 + s, separation)
    record.update(ttc_s=ttc, time_to_cpa_s=round(cpa[0], 2), min_separation_m=round(cpa[1], 2), closing=closing,
                  conflict=ttc is not None, risk=_level(ttc, report.get("age_s") or 0.0))
    return record


def _severity(assessment):
    return LEVELS.index(assessment["risk"]), -assessment["ttc_s"]


def ego_safety(t, assessments):
    """EGO_ROADSENSE's safety state: overall risk, most critical track, soonest conflict, number of conflicts."""
    conflicts = [a for a in assessments if a["conflict"]]
    worst = max(conflicts, key=_severity, default=None)
    return {
        "timestamp": round(t, 3),
        "overall_risk": worst["risk"] if worst else "SAFE",
        "most_critical_track": worst["track_id"] if worst else None,
        "minimum_ttc_s": min((a["ttc_s"] for a in conflicts), default=None),
        "active_conflicts": len(conflicts),
    }


def format_summary(t, reports, assessments):
    """Console lines: the safety state, then one line per vehicle in conflict, most critical first."""
    s = ego_safety(t, assessments)
    lines = ["[RoadSense:SAFETY] t=%.1fs risk=%s conflicts=%d min_ttc=%s critical=%s" % (
        t, s["overall_risk"], s["active_conflicts"],
        "n/a" if s["minimum_ttc_s"] is None else "%.1fs" % s["minimum_ttc_s"], s["most_critical_track"] or "none")]
    conflicts = [(r, a) for r, a in zip(reports, assessments) if a["conflict"]]
    for r, a in sorted(conflicts, key=lambda ra: _severity(ra[1]), reverse=True):
        p, v = r["relative_position"], r["relative_velocity"]
        lines.append("  %s %-6s %5.1fm %s rel_vel=(%+.1f,%+.1f)m/s TTC=%.1fs CPA=%.1fm@%.1fs %s" % (
            r["track_id"], "ahead" if p["longitudinal_m"] >= 0 else "behind", r["distance_m"], r["lane_relation"],
            v["longitudinal_mps"], v["lateral_mps"], a["ttc_s"], a["min_separation_m"], a["time_to_cpa_s"], a["risk"]))
    return "\n".join(lines)
