"""EGO_ROADSENSE telemetry helpers (Webots digital twin): units, heading wrap, acceleration, console line."""
import importlib.util
import math
from pathlib import Path

import pytest

# The helpers live next to the Webots controller, outside the roadsense package.
_PATH = Path(__file__).resolve().parents[1] / "simulation/webots/controllers/roadsense_ego/telemetry.py"
_spec = importlib.util.spec_from_file_location("roadsense_ego_telemetry", _PATH)
telemetry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telemetry)


def _record(**overrides):
    args = dict(t=12.4, vehicle_id="EGO_ROADSENSE", position=(-210.5, 6.4, 0.4), speed_mps=22.2,
                yaw_rad=math.pi, steering_rad=0.01, previous=None, maneuver="lane_keep", lane=1)
    args.update(overrides)
    return telemetry.make_record(**args)


def test_first_record_has_units_converted_and_no_acceleration():
    assert _record(yaw_rad=-math.pi / 2) == {
        "timestamp": 12.4,
        "vehicle_id": "EGO_ROADSENSE",
        "position": {"x": -210.5, "y": 6.4, "z": 0.4},
        "speed_mps": 22.2,
        "speed_kmh": 79.92,
        "heading_deg": 270.0,
        "acceleration_mps2": None,
        "steering_rad": 0.01,
        "mode": "baseline_highway",
        "maneuver": "lane_keep",
        "lane": 1,
    }


@pytest.mark.parametrize("yaw_rad, heading", [
    (0.0, 0.0),
    (math.pi / 2, 90.0),
    (math.pi, 180.0),
    (-math.pi, 180.0),
    (-math.pi / 2, 270.0),
    (-1e-9, 0.0),  # rounds to 360.00, must wrap to 0
])
def test_heading_is_degrees_in_0_to_360(yaw_rad, heading):
    assert telemetry.heading_deg(yaw_rad) == heading


@pytest.mark.parametrize("speed, acceleration", [(22.5, 0.25), (21.0, -0.5)])
def test_acceleration_is_speed_change_since_previous_record(speed, acceleration):
    record = _record(t=12.4, speed_mps=speed, previous=(10.4, 22.0))  # 2 s earlier at 22 m/s
    assert record["acceleration_mps2"] == pytest.approx(acceleration)


def test_console_line():
    first = _record()
    later = _record(previous=(11.4, 23.2))
    assert telemetry.format_line(first) == (
        "[RoadSense:EGO] t=12.4s speed=79.9km/h pos=(-210.5,6.4,0.4) heading=180.0° accel=n/a "
        "steer=+0.010rad mode=baseline_highway/lane_keep lane=1")
    assert "accel=-1.00m/s² " in telemetry.format_line(later)
