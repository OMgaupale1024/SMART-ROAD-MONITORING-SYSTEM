"""Checks for the calibration laboratory's geometry (tools/webots_calibration.py), which scores sensors only."""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import webots_calibration as wc  # noqa: E402
from roadsense.autonomy import perception  # noqa: E402

IDENTITY = [0.0, 0.0, 0.0, 0.0, 1, 0, 0, 0, 1, 0, 0, 0, 1]


def test_ego_point_is_roadsense_transform():
    rng = random.Random(1)
    for name in ("radar front", "radar rear"):
        lo, hi, fov = 1.0, wc.RADARS[name][4], wc.RADARS[name][5]
        for _ in range(200):
            d, az = rng.uniform(1.5, hi - 1), rng.uniform(-fov / 2 + 0.01, fov / 2 - 0.01)
            det = perception.radar_detections(name, [(d, az, 0.0)], (lo, hi, fov), 0.0, (0.0, 0.0, 0.0))[0]
            lon, lat = wc.ego_point(name, d, az)
            assert abs(lon - det.longitudinal_m) < 1e-9 and abs(lat - det.lateral_m) < 1e-9


def place(lon, lat, z):
    """A model pose (no rotation) whose origin sits at ego-frame (lon, lat, z)."""
    return [lon, lat, z, 0.0, 1, 0, 0, 0, 1, 0, 0, 0, 1]


def test_azimuth_model_is_true_bearing_without_height_offset():
    mz = wc.RADARS["radar front"][2]
    for lat in (-5.0, -1.0, 0.5, 3.75):
        e = wc.expected("radar front", IDENTITY, "T_BOX_C", place(20.0, lat, mz))
        assert abs(e["az_webots_model"] - e["az_origin_true"]) < 1e-9
        assert (lat > 0) == (e["az_webots_model"] < 0)  # Webots: positive to the radar's right


def test_height_offset_inflates_azimuth_and_correction_inverts_it():
    # a car-like origin 1.4 m behind the body centre at the nominal heights the correction assumes
    ego = list(IDENTITY)
    ego[2] = wc.EGO_ORIGIN_Z
    mz = wc.RADARS["radar front"][2] + wc.EGO_ORIGIN_Z
    for x_origin, lat in ((1.0, 0.3), (4.0, 1.0), (12.0, 3.75)):
        model = place(wc.RADARS["radar front"][0] + x_origin, lat, wc.NOMINAL_TARGET_ORIGIN_Z)
        e = wc.expected("radar front", ego, "T_BOX_C", model)
        assert abs(e["az_webots_model"]) > abs(e["az_origin_true"])  # the leak inflates the angle
        d = math.hypot(x_origin, lat) + wc.NOMINAL_ORIGIN_BEHIND_CENTRE
        fixed = wc.corrected_azimuth("radar front", d, e["az_webots_model"])
        assert abs(fixed - e["az_origin_true"]) < 1e-6, (x_origin, lat, fixed, e["az_origin_true"])
        assert mz > wc.NOMINAL_TARGET_ORIGIN_Z


def test_wrapper_placement_leaves_the_requested_gap():
    for kind in wc.KINDS:
        lon = wc.wrapper_lon_for_gap(kind, 5.0)
        xmin = wc.model_bounds(kind)[0] + wc.KINDS[kind]["wrapper_dx"] + lon
        assert abs(xmin - (wc.EGO_FRONT_M + 5.0)) < 1e-9
