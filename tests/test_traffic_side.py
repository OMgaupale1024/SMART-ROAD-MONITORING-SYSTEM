"""Traffic handedness: right-hand (the world as built) and left-hand (Indian roads) driving of one carriageway."""
import math

import pytest

from roadsense.autonomy import hazard_map, live_state, perception, pipeline, safety
from test_ego_safety import collision, decide, pothole, track

PROFILES = {"highway": (math.pi, "right"), "highway_lht": (0.0, "left")}
LANE_Y = [perception.CARRIAGEWAY_Y[0] + (i + 0.5) * perception.LANE_WIDTH_M for i in range(4)]


@pytest.fixture(params=sorted(PROFILES))
def road(request):
    perception.use_road(request.param)
    yield request.param
    perception.use_road("highway")


def side_of(target_y, pose):
    """The driver's physical side of a point, from the ego frame (lateral positive to the left)."""
    lateral = perception.world_to_ego(pose[0] + 20, target_y, pose)[1]
    return "left_lane" if lateral > 0 else "right_lane"


def test_profiles_are_the_same_carriageway_driven_both_ways():
    assert perception.ROAD_PROFILES == PROFILES
    assert perception.ROAD_PROFILE == "highway" and perception.TRAFFIC == "right"  # the default is unchanged
    with pytest.raises(ValueError):
        perception.use_road("india")
    assert perception.ROAD_PROFILE == "highway"


def test_lane_names_are_the_drivers_physical_sides(road):
    heading = perception.CARRIAGEWAY_HEADING
    for ego in range(perception.DRIVING_LANES):
        pose = (0.0, LANE_Y[ego], heading)
        for other in range(perception.DRIVING_LANES):
            if abs(other - ego) != 1:
                continue
            physical = side_of(LANE_Y[other], pose)
            assert perception.lane_relation(LANE_Y[other], LANE_Y[ego]) == physical
            assert hazard_map.lane_relation(LANE_Y[other], LANE_Y[ego]) == physical


def test_the_median_is_on_the_side_opposite_the_traffic(road):
    # right-hand traffic: the median (lane 0's side) is on the left; left-hand traffic: on the right
    expected = {"right": "left_lane", "left": "right_lane"}[perception.TRAFFIC]
    assert perception.lane_relation(LANE_Y[0], LANE_Y[1]) == expected


def test_adjacent_lanes_follow_the_physical_sides(road):
    free = {"left": (10.0, 10.0), "right": (10.0, 10.0)}
    median_side = "left_lane" if perception.TRAFFIC == "right" else "right_lane"
    kerb_side = "right_lane" if median_side == "left_lane" else "left_lane"
    assert pipeline.adjacent_lanes(LANE_Y[0], "lane_keep", free) == [kerb_side]  # next to the median
    assert pipeline.adjacent_lanes(LANE_Y[2], "lane_keep", free) == [median_side]  # the outer driving lane
    assert sorted(pipeline.adjacent_lanes(LANE_Y[1], "lane_keep", free)) == ["left_lane", "right_lane"]
    blocked = dict(free, left=(2.0, 10.0))  # a car alongside on the driver's left
    assert pipeline.adjacent_lanes(LANE_Y[1], "lane_keep", blocked) == ["right_lane"]


def test_the_dashboard_road_puts_each_lane_on_its_physical_side(road):
    pose = (-1000.0, LANE_Y[1], perception.CARRIAGEWAY_HEADING)
    r = live_state.road(pose)
    assert r["traffic"] == perception.TRAFFIC and r["heading_deg"] == 0
    for lane in r["lanes"]:
        if lane["relation"] in ("left_lane", "right_lane"):
            assert (lane["center_lateral_m"] > 0) == (lane["relation"] == "left_lane")


def test_a_pipeline_takes_its_traffic_side_from_the_road_profile(road):
    assert pipeline.Pipeline("EGO").policy.traffic == perception.TRAFFIC
    assert pipeline.Pipeline("EGO", safety.DEFAULT_POLICY).policy.traffic == "right"


@pytest.mark.parametrize("traffic,preferred", [("right", "left_lane"), ("left", "right_lane")])
def test_overtaking_is_preferred_away_from_the_traffic_side(traffic, preferred):
    s = decide([pothole(120)], lanes=safety.LANES, policy=safety.Policy(traffic=traffic))
    assert (s["recommended_action"], s["recommended_lane"]) == ("CONSIDER_LANE_CHANGE", preferred)


@pytest.mark.parametrize("traffic", ["right", "left"])
@pytest.mark.parametrize("only", safety.LANES)
def test_either_side_is_still_permitted_when_it_is_the_only_free_one(traffic, only):
    s = decide([pothole(120)], lanes=(only,), policy=safety.Policy(traffic=traffic))
    assert s["recommended_lane"] == only


@pytest.mark.parametrize("traffic", ["right", "left"])
def test_the_same_checks_block_the_preferred_side(traffic):
    policy = safety.Policy(traffic=traffic)
    preferred_lat = 3.75 if traffic == "right" else -3.75  # left is positive lateral
    other = "right_lane" if traffic == "right" else "left_lane"
    # a car alongside in the preferred lane: the other side is chosen
    assert decide([pothole(120)], r=[track(5, preferred_lat)], lanes=safety.LANES, policy=policy)[
        "recommended_lane"] == other
    # both sides occupied, a collision, or a stale track: no lane at all
    both = [track(5, 3.75, ident="TRACK_001"), track(5, -3.75, ident="TRACK_002")]
    assert decide([pothole(120)], r=both, lanes=safety.LANES, policy=policy)["recommended_lane"] is None
    assert decide([pothole(120)], a=[collision(4)], lanes=safety.LANES, policy=policy)["recommended_lane"] is None
    stale = dict(track(100, preferred_lat), last_seen=40)
    assert decide([pothole(120)], r=[stale], lanes=safety.LANES, policy=policy)["recommended_lane"] is None


@pytest.mark.parametrize("traffic", ["right", "left"])
def test_emergency_braking_does_not_depend_on_the_traffic_side(traffic):
    s = decide(a=[collision(0.8)], lanes=safety.LANES, policy=safety.Policy(traffic=traffic))
    assert s["recommended_action"] == "EMERGENCY_BRAKE" and s["recommended_lane"] is None
    assert s == decide(a=[collision(0.8)], lanes=safety.LANES)


def test_an_unknown_traffic_side_is_rejected():
    with pytest.raises(ValueError):
        decide(policy=safety.Policy(traffic="centre"))
