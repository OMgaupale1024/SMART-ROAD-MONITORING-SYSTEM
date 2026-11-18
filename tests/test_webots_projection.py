"""Stage W8: the Webots R2025b (838bb6f) vectorProjection sign error and the radar reference point, reproduced with the
Python port in tools/webots_calibration.py and checked against measurements from the installed and patched builds
(docs/evidence/webots-w8/measured-fixture.json, noise-free diagnostic radars)."""
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import webots_calibration as wc  # noqa: E402

VARIANT = {"installed": "orig", "none": "orig", "A": "A", "AB": "AB"}


def project(v, n, fixed):
    """WbAffinePlane::vectorProjection for a plane through the origin with unit normal n: installed (+) or fixed (-)."""
    k = -wc.dot(v, n) if fixed else wc.dot(v, n)
    return tuple(a + b * k for a, b in zip(v, n))


@pytest.mark.parametrize("v", [(3, 1, 0.5), (10, -2, -0.4), (0.5, 0, 2), (-1, 4, 0.1)])
@pytest.mark.parametrize("n", [(0, 0, 1), wc._unit((0.1, 0.2, 0.97)), wc._unit((1, -1, 0.3))])
def test_projection_lies_on_plane_only_when_fixed(v, n):
    fixed, installed = project(v, n, True), project(v, n, False)
    assert wc.dot(fixed, n) == pytest.approx(0, abs=1e-12)
    assert wc.dot(installed, n) == pytest.approx(2 * wc.dot(v, n))  # pushed further off the plane, not onto it


def radar_azimuth(v, fixed):
    """WbRadar azimuth of a radar-frame vector v (x forward, y left, z up): angle to x of its projection on the
    radar's horizontal plane, negative to the left."""
    p = project(v, (0, 0, 1), fixed)
    a = math.acos(p[0] / math.sqrt(wc.dot(p, p)))
    return -a if v[1] > 0 else a


@pytest.mark.parametrize("dz", [-1.2, -0.3, 0.3, 1.2])  # target origin below / above the radar
@pytest.mark.parametrize("x", [3.0, 40.0])  # short and long distances
def test_vertical_offset_leaks_into_azimuth_only_in_installed(dz, x):
    y = 0.0
    assert radar_azimuth((x, y, dz), fixed=True) == pytest.approx(0, abs=1e-12)
    leak = math.acos(x / math.sqrt(x * x + 4 * dz * dz))
    assert abs(radar_azimuth((x, y, dz), fixed=False)) == pytest.approx(leak)  # same for +dz and -dz
    y = 1.5
    assert radar_azimuth((x, y, dz), fixed=True) == pytest.approx(-math.atan2(y, x))
    assert abs(radar_azimuth((x, y, dz), fixed=False)) > abs(math.atan2(y, x))  # always away from the axis


def car_pose(lon, lat=0.0, yaw=0.0, z=0.32):
    """A logged pose (x, y, z, yaw, row-major rotation) in the lab's ego frame (ego at the origin, yaw 0)."""
    c, s = math.cos(yaw), math.sin(yaw)
    return [lon, lat, z, yaw, c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]


EGO = car_pose(0.0, z=0.31)


@pytest.mark.parametrize("radar", ["diag radar front", "diag radar front low", "diag radar rear"])  # 2 heights, both ends
@pytest.mark.parametrize("yaw", [0.0, 0.6, math.pi / 2, math.pi])  # target orientations
@pytest.mark.parametrize("gap", [4.0, 60.0])
def test_patched_azimuth_bias_is_removed(radar, yaw, gap):
    ahead = wc.RADARS[radar][3] == 0.0
    mount = wc.RADARS[radar][0]
    lon = mount + gap if ahead else mount - gap
    pose = car_pose(lon, yaw=yaw)
    orig, a, ab = (wc.webots_measure(radar, EGO, "T_CAR", pose, v) for v in ("orig", "A", "AB"))
    assert orig and a and ab
    # A: bearing of the model origin with no vertical leak; AB: bearing of the clipped box centre (dead ahead here)
    ox, oy, _ = a["origin"]
    assert a["az"] == pytest.approx(-math.atan2(oy, ox), abs=1e-9)
    assert ab["az"] == pytest.approx(-math.atan2(ab["point"][1], ab["point"][0]), abs=1e-9)
    if yaw in (0.0, math.pi):  # symmetric about the radar's axis: the clipped box centre is dead ahead
        assert abs(ab["az"]) < 1e-6
    # the installed build is biased away from the axis by the origin's height difference
    assert abs(orig["az"]) >= abs(a["az"]) - 1e-12


def test_truck_range_residual_comes_from_vertical_clipping():
    """The installed projection inflates the clipped box vertically, which moves the box centre and the range."""
    pose = car_pose(4.054 + 10.0, z=0.5)
    orig = wc.webots_measure("diag radar front", EGO, "T_TRUCK", pose, "orig")
    fixed = wc.webots_measure("diag radar front", EGO, "T_TRUCK", pose, "A")
    assert orig["d"] - fixed["d"] > 0.2
    assert abs(fixed["point"][2]) < abs(orig["point"][2])


FIXTURE = json.loads((ROOT / "docs/evidence/webots-w8/measured-fixture.json").read_text())["cases"]


@pytest.mark.parametrize("case", FIXTURE, ids=lambda c: "%s-%s-%s-%s" % (c["variant"], c["case"], c["radar"], c["target"]))
def test_port_reproduces_each_build(case):
    p = wc.webots_measure(case["radar"], case["ego"], case["target"], case["model"], VARIANT[case["variant"]])
    assert case["d"] == pytest.approx(p["d"], abs=1e-3)
    az = case["az"]
    if VARIANT[case["variant"]] == "orig" and abs(p["origin"][1]) < 0.01:
        az = math.copysign(az, p["az"])  # dead ahead the installed sign comes from float noise in the origin's y
    assert az == pytest.approx(p["az"], abs=math.radians(0.01))


def test_fixture_distinguishes_installed_from_patched():
    """At least some measured installed returns are inconsistent with the patched model (and vice versa)."""
    def misfit(case, variant):
        p = wc.webots_measure(case["radar"], case["ego"], case["target"], case["model"], variant)
        return abs(case["d"] - p["d"]) > 0.01 or abs(case["az"] - p["az"]) > math.radians(0.05)
    assert sum(misfit(c, "AB") for c in FIXTURE if c["variant"] == "installed") >= 5
    assert sum(misfit(c, "orig") for c in FIXTURE if c["variant"] == "AB") >= 5
