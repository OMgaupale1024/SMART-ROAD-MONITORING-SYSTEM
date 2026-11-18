"""Stages W9-W11: the experimental sensor profile's generated world and the section 6 radar measurement model."""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import radar_measurement_model as rmm  # noqa: E402
import scenarios  # noqa: E402
import sensor_profile as sp  # noqa: E402


def test_profile_world_is_a_generated_copy_with_the_profile_robot_in_the_ego():
    original = sp.WORLD.read_text()
    wbt = sp.world(scenarios.draw("normal", 1, "highway_lht"), 100)
    assert sp.WORLD.read_text() == original
    assert "SumoInterface {\n  step 100\n" in wbt
    ego = wbt[wbt.index("DEF WEBOTS_VEHICLE0"):]
    centre = ego[ego.index("sensorsSlotCenter [\n"):]
    assert centre.index('controller "roadsense_sensor_recorder"') < centre.index('DEF RIGHT DistanceSensor')
    for name in sp.PROFILE_RADARS:
        assert wbt.count('name "%s"' % name) == 1
    assert 'IMPORTABLE EXTERNPROTO "../protos/RoadSenseAutorickshaw.proto"' in wbt
    assert 'controller "roadsense_ego"' in wbt  # RoadSense's own controller and radars untouched
    assert wbt.count('name "radar front"') == original.count('name "radar front"') == 1


def test_profile_front_and_rear_radars_copy_roadsense_mounts():
    sys.path.insert(0, str(ROOT / "src"))
    from roadsense.autonomy import perception
    for ours, theirs in (("profile radar front", "radar front"), ("profile radar rear", "radar rear")):
        x, y, _z, yaw = sp.PROFILE_RADARS[ours][:4]
        assert (x, y) == pytest.approx(perception.RADARS[theirs][1:3])
        assert yaw == pytest.approx(perception.RADARS[theirs][3], abs=1e-6)


def test_body_alignment_puts_each_body_centre_on_the_wrapper():
    for model in sp.MODEL_BOXES:
        lo, hi = sp.body_x_range(model)
        assert lo < hi
    assert sp.body_x_range("RoadSenseAutorickshaw") == pytest.approx((-0.45, 2.35))


def test_self_clutter_is_flagged_not_dropped():
    m = rmm.describe("exp radar corner fl", 2.6, -0.8, 0.0, 1.6)
    assert m["self_suspect"] and m["bearing_clamped"] and m["range"][0] > 1.5
    # the same cell with a closing speed is a vehicle, not the ego's body
    assert not rmm.describe("exp radar corner fl", 2.6, -0.8, -4.0, 1.6)["self_suspect"]
    # the other side of the corner radar's field of view is never its own body
    assert not rmm.describe("exp radar corner fl", 2.6, 0.8, 0.0, 1.6)["self_suspect"]
    m = rmm.describe("radar front", 1.0, 1.3, None, 2.6)
    assert m["self_suspect"] and m["range_clamped"] and m["range"] == (0.0, 1.0)


def test_edge_and_first_refresh_keep_the_detection():
    m = rmm.describe("radar front", 20.0, 1.3, -3.0, 2.6)
    assert m["bearing_clamped"] and not m["self_suspect"]
    assert m["bearing"][0] == pytest.approx(1.3 - 0.003) and m["bearing"][1] == pytest.approx(math.pi)
    assert m["roadsense_current_rule"] == "dropped"
    m = rmm.describe("radar front", 30.0, 0.1, 1.9e5, 2.6, first_refresh=True)
    assert m["velocity_unreliable"] and m["range_rate"] is None and m["range"][0] > 29


def test_build_self_clutter_from_target_free_samples():
    sig = rmm.build_self_clutter([(2.5, -0.8, 0.0), (2.7, -0.8, 0.0), (2.3, -0.8, 0.01)], 1.6)
    assert sig["edge_sign"] == -1 and sig["presence"] == 1.0
    assert sig["range"] == pytest.approx((2.05, 2.95))
