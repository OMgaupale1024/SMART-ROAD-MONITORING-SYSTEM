"""Unified decision contract and recommendation-only controller integration, without Webots."""
import ast
import copy
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

_DIR = Path(__file__).resolve().parents[1] / 'simulation/webots/controllers/roadsense_ego'
sys.path.insert(0, str(_DIR))
try:
    import safety
    import risk
    import prediction
    import hazard_map
    import hazards
finally:
    sys.path.remove(str(_DIR))


def pothole(distance=25, severity='HIGH', lane='ego_lane', confidence=.97, ident='PH_001'):
    lat = {'ego_lane': 0, 'left_lane': 3.75, 'right_lane': -3.75}.get(lane, 10)
    return {'hazard_id': ident, 'type': 'pothole', 'status': 'ACTIVE', 'severity': severity,
            'confidence': confidence, 'relative': {'longitudinal_m': distance, 'lateral_m': lat,
            'distance_m': math.hypot(distance, lat), 'lane_relation': lane,
            'direction': 'ahead' if distance >= 0 else 'behind'}}


def track(x=60, y=3.75, vx=0, vy=0, ident='TRACK_020'):
    return {'track_id': ident, 'last_seen': 42., 'age_s': 5.,
            'relative_position': {'longitudinal_m': x, 'lateral_m': y},
            'relative_velocity': {'longitudinal_mps': vx, 'lateral_mps': vy}}


def collision(ttc):
    r = track(risk.EGO_CENTRE_M + risk.EGO_HALF_LENGTH_M + risk.TARGET_HALF_LENGTH_M
              + risk.SAFETY_BUFFER_M + 5 * ttc, 0, -5)
    return risk.assess(r, prediction.predict(r))


def decide(h=(), a=(), r=(), speed=80, lanes=(), policy=safety.DEFAULT_POLICY):
    return safety.decide(42., speed / 3.6, list(a), list(r), {'hazards': list(h)}, lanes, policy)


def test_safe_baseline():
    s = decide()
    assert (s['overall_risk'], s['recommended_action'], s['recommended_speed_kmh']) == ('SAFE', 'MAINTAIN', 80)
    assert s['primary_threat'] is None and s['recommended_lane'] is None
    assert s['road_safety'] == {'relevant_hazards': 0, 'nearest_hazard': None, 'hazards': []}
    assert '[RoadSense:DECISION] t=42.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none' in safety.format_decision(s)


@pytest.mark.parametrize('severity,distance,expected', [('LOW', 300, 'SAFE'), ('LOW', 100, 'CAUTION'),
    ('MEDIUM', 25, 'HIGH'), ('HIGH', 25, 'HIGH'), ('HIGH', 150, 'CAUTION')])
def test_severity_and_approach_distance(severity, distance, expected):
    assert decide([pothole(distance, severity)])['overall_risk'] == expected


@pytest.mark.parametrize('lane', ['left_lane', 'right_lane'])
def test_adjacent_pothole_is_monitor_only(lane):
    s = decide([pothole(lane=lane)])
    assert (s['overall_risk'], s['recommended_action'], s['recommended_speed_kmh']) == ('CAUTION', 'MONITOR', 80)


@pytest.mark.parametrize('distance,lane', [(-300, 'ego_lane'), (-.01, 'ego_lane'), (0, 'ego_lane'),
    (25, 'off_road'), (25, 'other_lane'), (25, 'unknown')])
def test_irrelevant_hazards_clear(distance, lane):
    assert decide([pothole(distance, lane=lane)])['overall_risk'] == 'SAFE'


def test_low_confidence_caps_alert_and_disables_lane_change():
    s = decide([pothole(120, confidence=.2)], lanes=safety.LANES)
    assert (s['overall_risk'], s['recommended_action'], s['recommended_speed_kmh']) == ('CAUTION', 'MONITOR', 80)
    assert 'low confidence' in s['reason']


def test_speed_changes_urgency():
    h = pothole(25)
    assert decide([h], speed=15)['overall_risk'] == 'CAUTION'
    assert decide([h], speed=80)['overall_risk'] == 'HIGH'


def test_target_speed_is_distance_dependent_and_severity_dependent():
    speeds = [decide([pothole(d)])['recommended_speed_kmh'] for d in (5, 25, 60, 150)]
    assert speeds == sorted(speeds) and len(set(speeds)) == 4
    assert decide([pothole(25, 'MEDIUM')])['recommended_speed_kmh'] > speeds[1]
    assert decide([pothole(25)])['recommended_action'] == 'BRAKE'
    assert decide([pothole(80)])['recommended_action'] == 'SLOW_DOWN'


@pytest.mark.parametrize('ttc,level,action', [(6, 'SAFE', 'MAINTAIN'), (4, 'CAUTION', 'SLOW_DOWN'),
    (2.4, 'HIGH', 'BRAKE'), (.8, 'CRITICAL', 'EMERGENCY_BRAKE')])
def test_vehicle_pipeline_integration(ttc, level, action):
    s = decide(a=[collision(ttc)])
    assert (s['overall_risk'], s['recommended_action']) == (level, action)
    if level != 'SAFE':
        assert s['primary_threat']['id'] == 'TRACK_020'
        assert 'predicted collision' in s['reason']
    if level == 'CRITICAL':
        assert s['recommended_speed_kmh'] == 0


@pytest.mark.parametrize('ttc,action', [(2.4, 'BRAKE'), (.8, 'EMERGENCY_BRAKE')])
def test_imminent_vehicle_overrides_high_pothole(ttc, action):
    s = decide([pothole()], [collision(ttc)], lanes=safety.LANES)
    assert s['primary_threat']['type'] == 'vehicle'
    assert s['recommended_action'] == action and s['recommended_lane'] is None
    assert s['road_safety']['hazards'][0]['urgency'] == 'HIGH'


def test_high_pothole_overrides_later_caution_vehicle():
    s = decide([pothole()], [collision(4)])
    assert s['overall_risk'] == 'HIGH' and s['primary_threat']['type'] == 'road_hazard'


def test_adjacent_lane_free_suggestion():
    s = decide([pothole(120)], lanes=('right_lane',))
    assert s['recommended_action'] == 'CONSIDER_LANE_CHANGE'
    assert s['recommended_lane'] == 'right_lane'


@pytest.mark.parametrize('r', [track(10, -3.75), track(-10, -3.75), track(-100, -3.75, 30),
    track(20, -7.5, 0, 2), dict(track(100), last_seen=40)])
def test_adjacent_lane_blocked_or_closing_or_stale(r):
    s = decide([pothole(95)], r=[r], lanes=('right_lane',))
    assert s['recommended_action'] == 'SLOW_DOWN' and s['recommended_lane'] is None


def test_lane_gate_unknown_lanes_collision_or_hazard():
    h = pothole(120)
    assert decide([h])['recommended_lane'] is None
    assert decide([h], a=[collision(4)], lanes=safety.LANES)['recommended_lane'] is None
    s = decide([h, pothole(80, lane='right_lane', ident='PH_002')], lanes=('right_lane',))
    assert s['recommended_lane'] is None


def test_clear_other_lane_can_be_considered_when_one_is_blocked():
    assert decide([pothole(120)], r=[track(10)], lanes=safety.LANES)['recommended_lane'] == 'right_lane'


def test_order_does_not_change_primary_and_secondary_speed_cap():
    hs = [pothole(60, ident='PH_002'), pothole(25)]
    s = decide(hs, [collision(2.4)])
    assert s == decide(list(reversed(hs)), [collision(2.4)])
    assert s['recommended_speed_kmh'] == decide([pothole(25)])['recommended_speed_kmh']
    assert s['road_safety']['nearest_hazard'] == 'PH_001'


@pytest.mark.parametrize('speed', [0, .01, 15, 80, 120, 500])
@pytest.mark.parametrize('severity', ['LOW', 'MEDIUM', 'HIGH'])
def test_speed_bounds_strict_json_and_determinism(speed, severity):
    s = decide([pothole(5, severity)], speed=speed)
    assert 0 <= s['recommended_speed_kmh'] <= 80
    assert s == json.loads(json.dumps(s, allow_nan=False))
    assert s == decide([pothole(5, severity)], speed=speed)


def test_configurable_nominal_and_deceleration():
    p = safety.Policy(nominal_speed_kmh=60, reaction_time_s=1.5)
    assert decide(policy=p)['recommended_speed_kmh'] == 60
    assert decide([pothole()], policy=p)['recommended_speed_kmh'] <= 60
    fractional = safety.Policy(nominal_speed_kmh=79.95)
    assert decide(policy=fractional)['recommended_speed_kmh'] <= 79.95
    assert decide([pothole(150)], policy=fractional)['recommended_speed_kmh'] <= 79.95


def test_map_persists_and_input_is_not_mutated():
    m = hazard_map.HazardMap('EGO_ROADSENSE')
    m.update([hazards.HazardDetection(0, 'test', 'pothole', -100, 3.2, 'HIGH', .9, None)])
    states = []
    for x in (50, -75, -110):
        snap = m.snapshot(42, (x, 3.2, math.pi))
        before = copy.deepcopy(snap)
        states.append(safety.decide(42, 80/3.6, [], [], snap))
        assert snap == before
    assert [s['overall_risk'] for s in states] == ['CAUTION', 'HIGH', 'SAFE']
    assert len(m.active_hazards()) == 1


@pytest.mark.parametrize('speed', [float('nan'), float('inf'), -1, '80', None, True])
def test_invalid_speed_rejected(speed):
    with pytest.raises(ValueError):
        safety.decide(42, speed, [], [], {'hazards': []})


@pytest.mark.parametrize('h', [None, {}, {'relative': None}, dict(pothole(), confidence=float('nan')),
    dict(pothole(), confidence=2), dict(pothole(), severity='CRITICAL'),
    dict(pothole(), relative=dict(pothole()['relative'], longitudinal_m=float('inf')))])
def test_malformed_hazard_rejected(h):
    with pytest.raises(ValueError):
        decide([h])


@pytest.mark.parametrize('a', [None, {}, dict(collision(2), ttc_s=float('inf')),
    dict(collision(2), conflict=False), dict(collision(2), risk='SEVERE')])
def test_malformed_assessment_rejected(a):
    with pytest.raises(ValueError):
        decide(a=[a])


@pytest.mark.parametrize('policy', [safety.Policy(reaction_time_s=0), safety.Policy(nominal_speed_kmh=float('nan')),
    safety.Policy(strong_deceleration_mps2=1)])
def test_invalid_policy_rejected(policy):
    with pytest.raises(ValueError):
        decide(policy=policy)


def test_controller_actuation_and_old_pipeline_statements_unchanged():
    path = 'simulation/webots/controllers/roadsense_ego/roadsense_ego.py'
    original = ast.parse(subprocess.check_output(['git', 'show', '2ad9551:' + path], text=True))
    current = ast.parse((_DIR / 'roadsense_ego.py').read_text())
    def driving(tree):
        loop = next(n for n in tree.body if isinstance(n, ast.While))
        return [ast.dump(n) for n in loop.body[:next(i for i, n in enumerate(loop.body)
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'now' for t in n.targets))]]
    assert driving(current) == driving(original)
    assert not any(isinstance(n, ast.ImportFrom) and n.module in ('vehicle', 'controller')
                   for n in ast.walk(ast.parse((_DIR / 'safety.py').read_text())))
    loop = next(n for n in current.body if isinstance(n, ast.While))
    printed = loop.body[-1]
    assert 'nextPerception' in ast.unparse(printed.test)
    assert 'safety.format_decision(unifiedSafetyState)' in ast.unparse(printed)


@pytest.mark.parametrize('r', [track(10, 0), track(10, -5.7)])
def test_lane_gate_covers_swept_ego_path_and_vehicle_width(r):
    assert decide([pothole(120)], r=[r], lanes=('right_lane',))['recommended_lane'] is None


def test_monitor_only_hazard_cannot_hide_actionable_caution():
    s = decide([pothole(10, confidence=.2), pothole(50, 'LOW', ident='PH_002')])
    assert s['primary_threat']['id'] == 'PH_002'
