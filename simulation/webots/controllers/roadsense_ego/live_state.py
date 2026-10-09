"""RoadSense live state: one processing cycle as a single roadsense.live.v1 JSON snapshot.

Plain Python without Webots imports (and Python 3.9). It only gathers records the RoadSense modules already made in
the cycle (telemetry, track reports, risk assessments, the hazard map snapshot, the unified decision); it computes
nothing new except where this world's lanes are relative to EGO_ROADSENSE, for drawing. Relative positions are in the
ego frame of the snapshot's time: x (longitudinal) ahead, y (lateral) left, origin at EGO_ROADSENSE's rear axle;
world positions are Webots world coordinates. Unavailable numbers are null: the JSON never holds NaN or Infinity.
"""
import json
import math
import time
import uuid

import hazard_map
import perception

SCHEMA = "roadsense.live.v1"
_COLLISION = ("ttc_s", "time_to_cpa_s", "min_separation_m", "closing", "conflict", "risk")


def new_run_id():
    """An id for one simulation run, so the backend can tell a restart from a stale snapshot."""
    return uuid.uuid4().hex[:12]


def road(pose):
    """This world's lanes relative to EGO_ROADSENSE: each lane's centre line, its distance across the road from
    the ego origin (positive left), and the road's direction relative to the ego heading (positive left)."""
    x, y, yaw = pose
    heading = math.degrees(perception.CARRIAGEWAY_HEADING - yaw)
    low, high = perception.CARRIAGEWAY_Y
    lanes = []
    for i in range(int(round((high - low) / perception.LANE_WIDTH_M))):
        centre = low + (i + 0.5) * perception.LANE_WIDTH_M
        # across the road, positive to the left of its direction: for this carriageway, toward -x, left is -y
        across = math.cos(perception.CARRIAGEWAY_HEADING) * (centre - y)
        lanes.append({"relation": hazard_map.lane_relation(centre, y), "center_lateral_m": round(across, 2),
                      "driving": i < perception.DRIVING_LANES})
    return {"lane_width_m": perception.LANE_WIDTH_M, "heading_deg": round((heading + 180.0) % 360.0 - 180.0, 2),
            "lanes": lanes}


def build(t, ego, reports, assessments, hazard_snapshot, unified_safety, pose, run_id, sequence, active=True):
    """The snapshot of one cycle at time t. ego is telemetry.make_record's record, reports perception.track_report's,
    assessments risk.assess's, hazard_snapshot HazardMap.snapshot's and unified_safety safety.decide's, all made in
    this cycle; pose is the ego's world (x, y, yaw). Each track carries its prediction and collision risk."""
    by_track = {a["track_id"]: a for a in assessments}
    tracks = []
    for report in reports:
        a = by_track.get(report["track_id"])
        if a is None:
            tracks.append(dict(report, prediction=None, collision=None))
        else:
            tracks.append(dict(report, prediction={"model": a["prediction_model"], "trajectory": a["trajectory"]},
                               collision={k: a[k] for k in _COLLISION}))
    return {
        "schema": SCHEMA,
        "timestamp": round(t, 3),
        "ego": ego,
        "road": road(pose),
        "tracks": tracks,
        "hazards": hazard_snapshot["hazards"],
        "unified_safety": unified_safety,
        "simulation": {"source": "webots", "active": active, "run_id": run_id, "sequence": sequence,
                       "generated_at_unix_s": round(time.time(), 3)},
    }


def finished(state):
    """The last snapshot of a run: the same state, marked inactive, with the next sequence number."""
    simulation = dict(state["simulation"], active=False, sequence=state["simulation"]["sequence"] + 1,
                      generated_at_unix_s=round(time.time(), 3))
    return dict(state, simulation=simulation)


def _finite(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def encode(state):
    """The snapshot as compact JSON bytes, NaN and infinities as null."""
    return json.dumps(_finite(state), allow_nan=False, separators=(",", ":")).encode("utf-8")
