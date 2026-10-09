"""Motion prediction for RoadSense tracks: where each tracked vehicle will be relative to EGO_ROADSENSE.

Plain Python without Webots imports (and Python 3.9, which Webots runs controllers with): it reads a track record,
perception.track_report's ego-frame position and relative velocity, so any source of such tracks can use it. The
baseline model is constant velocity: the relative position moves on at the relative velocity, as if both vehicles
keep their current velocity over the ground. Positions are in the ego frame of the record's time (x ahead, y left,
origin at the ego origin), in m; times in s from the record's time.
"""
import math

MODEL = "constant_velocity"
HORIZONS_S = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)  # risk.py looks for conflicts as far ahead as this


def _finite(*values):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def predict(report):
    """The track's predicted relative positions at HORIZONS_S; none if its position or velocity isn't known."""
    p, v = report["relative_position"], report.get("relative_velocity") or {}
    lon, lat, v_lon, v_lat = p.get("longitudinal_m"), p.get("lateral_m"), v.get("longitudinal_mps"), v.get("lateral_mps")
    trajectory = []
    if _finite(lon, lat, v_lon, v_lat):
        trajectory = [{"t_s": t, "longitudinal_m": round(lon + v_lon * t, 2), "lateral_m": round(lat + v_lat * t, 2)}
                      for t in HORIZONS_S]
    return {"track_id": report["track_id"], "model": MODEL, "trajectory": trajectory}
