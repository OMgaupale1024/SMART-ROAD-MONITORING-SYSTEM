"""EGO_ROADSENSE telemetry helpers: plain Python without Webots imports, so tests/ can run them.

Record units: timestamp in s of simulation time; position in m in the Webots world frame (z up);
speed_mps in m/s, speed_kmh in km/h; heading_deg in degrees [0, 360), counter-clockwise from the
world +x axis (the Webots yaw); acceleration_mps2 in m/s², the speed change since the previous
record divided by the time between them (None on the first record); steering_rad in rad, the
Webots Driver steering angle (positive steers right).
"""
import math

MODE = "baseline_highway"


def heading_deg(yaw_rad):
    """Webots yaw (rad) as degrees rounded to 0.01, in [0, 360)."""
    return round(math.degrees(yaw_rad), 2) % 360.0


def make_record(t, vehicle_id, position, speed_mps, yaw_rad, steering_rad, previous, maneuver, lane):
    """One telemetry record. previous is the (timestamp, speed_mps) of the last record, or None."""
    x, y, z = position
    acceleration = None if previous is None else round((speed_mps - previous[1]) / (t - previous[0]), 3)
    return {
        "timestamp": round(t, 3),
        "vehicle_id": vehicle_id,
        "position": {"x": round(x, 3), "y": round(y, 3), "z": round(z, 3)},
        "speed_mps": round(speed_mps, 3),
        "speed_kmh": round(speed_mps * 3.6, 2),
        "heading_deg": heading_deg(yaw_rad),
        "acceleration_mps2": acceleration,
        "steering_rad": round(steering_rad, 4),
        "mode": MODE,
        "maneuver": maneuver,
        "lane": lane,
    }


def format_line(r):
    """The record as one console line."""
    p = r["position"]
    accel = "n/a" if r["acceleration_mps2"] is None else "%+.2fm/s²" % r["acceleration_mps2"]
    return ("[RoadSense:EGO] t=%.1fs speed=%.1fkm/h pos=(%.1f,%.1f,%.1f) heading=%.1f° accel=%s "
            "steer=%+.3frad mode=%s/%s lane=%d"
            % (r["timestamp"], r["speed_kmh"], p["x"], p["y"], p["z"], r["heading_deg"], accel,
               r["steering_rad"], r["mode"], r["maneuver"], r["lane"]))
