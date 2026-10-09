// The autonomy dashboard's presentation logic (src/roadsense/web/static/autonomy/view.js), without a browser.
// Expected values are read off the recorded snapshots by hand.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  detectEvents, labelledTracks, nearestAngle, primaryThreat, retarget, roadDistance, safetyPanel, sample, toScene,
  trackYaw, tween, view,
} from '../../src/roadsense/web/static/autonomy/view.js';

const RAW = Object.fromEntries(readFileSync(new URL('./recorded_snapshots.jsonl', import.meta.url), 'utf8')
  .trim().split('\n').map((line) => JSON.parse(line)).map((s) => [s.simulation.sequence, s]));
const at = (sequence) => view(structuredClone(RAW[sequence]));
const near = (actual, expected, tolerance = 1e-3) =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not within ${tolerance} of ${expected}`);

test('RoadSense ego frame to three.js: ahead is -z, left is -x', () => {
  assert.deepEqual(toScene(59.81, -0.22), { x: 0.22, z: -59.81 }); // PH_001, slightly right, ahead
  assert.deepEqual(toScene(-7.59, -0.54), { x: 0.54, z: 7.59 }); // TRACK_007, behind
  assert.deepEqual(toScene(18.69, 13.37), { x: -13.37, z: -18.69 }); // oncoming car beyond the median, on the left
});

test('a snapshot becomes the view model the scene and panels draw', () => {
  const model = at(102);
  assert.equal(model.t, 20.4);
  assert.deepEqual([model.runId, model.sequence, model.active], ['ffc51d2d138d', 102, true]);
  assert.equal(model.ego.speedKmh, 80);
  assert.equal(model.ego.lane, 2);
  assert.equal(model.ego.maneuver, 'lane_keep');
  assert.equal(model.road.laneWidth, 3.75);
  assert.deepEqual(model.road.lanes[0], { relation: 'ego_lane', lateral: 0.07, driving: true });
  assert.deepEqual(model.road.lanes.map((lane) => lane.driving), [true, true, true, false]);
  assert.equal(model.tracks.length, 12);
});

test('lane markings move with the car: its distance along the road comes from its world position and heading', () => {
  near(roadDistance(at(101)), 405.708, 1e-9); // world x -405.708 on a carriageway toward -x
  near(roadDistance(at(102)), 410.152, 1e-9); // 4.444 m later: 0.2 s at 80 km/h
  assert.equal(roadDistance(view({ ...RAW[102], road: null })), null);
});

test('each track keeps its id, ego-frame position, velocity and collision risk', () => {
  const track = at(102).tracks.find((t) => t.id === 'TRACK_002');
  assert.deepEqual(
    [track.lon, track.lat, track.vLon, track.vLat, track.distance, track.laneRelation, track.risk, track.ttc, track.conflict],
    [4.36, -4.42, -8.32, -0.16, 6.21, 'right_lane', 'SAFE', null, false],
  );
  const threat = at(6).tracks.find((t) => t.id === 'TRACK_007');
  assert.deepEqual([threat.risk, threat.ttc, threat.conflict], ['HIGH', 1.5, true]);
});

test('a track carries its predicted trajectory exactly as RoadSense sent it', () => {
  const trajectory = at(102).tracks.find((t) => t.id === 'TRACK_002').trajectory;
  assert.equal(trajectory.length, 10);
  assert.deepEqual(trajectory[0], { t: 0.5, lon: 0.2, lat: -4.5 });
  assert.equal(trajectory.at(-1).t, 5);
});

test('each mapped hazard keeps its id, severity, size and where it is relative to the car', () => {
  const [hazard] = at(102).hazards;
  assert.deepEqual(hazard, {
    id: 'PH_001', type: 'pothole', severity: 'HIGH', confidence: 0.9, status: 'ACTIVE', lon: 59.81, lat: -0.22,
    distance: 59.81, direction: 'ahead', laneRelation: 'ego_lane', length: 1.4, width: 1.0,
  });
});

test('passed hazards stay in the map', () => {
  const model = at(1226);
  assert.deepEqual(model.hazards.map((h) => [h.id, h.direction]),
    [['PH_001', 'behind'], ['PH_002', 'behind'], ['PH_003', 'behind'], ['PH_004', 'behind']]);
});

test('the unified safety state is taken as it is', () => {
  const safety = at(102).safety;
  assert.deepEqual([safety.risk, safety.action, safety.targetKmh, safety.lane, safety.minTtc, safety.conflicts],
    ['HIGH', 'BRAKE', 60.1, null, null, 0]);
  assert.deepEqual(safety.threat, { type: 'road_hazard', id: 'PH_001', reason: 'PH_001 HIGH pothole 59.8 m ahead in ego_lane' });
  assert.equal(safety.roadHazards[0].urgency, 'HIGH');
  near(safety.roadHazards[0].timeToHazard, 2.6915);
});

test('missing or null optional fields give nulls and empty lists, never a crash or an invented value', () => {
  const bare = view({ schema: 'roadsense.live.v1', timestamp: 3.2, simulation: { run_id: 'r1', sequence: 16, active: true } });
  assert.equal(bare.road, null);
  assert.equal(bare.safety, null);
  assert.deepEqual([bare.tracks, bare.hazards], [[], []]);
  assert.equal(bare.ego.speedKmh, null);

  const raw = structuredClone(RAW[102]);
  raw.tracks[0].prediction = null; // live_state sends these when a track has no assessment
  raw.tracks[0].collision = null;
  delete raw.tracks[1].relative_position; // can't be placed: left out
  raw.tracks[2].relative_position.longitudinal_m = '12';
  raw.tracks[3].collision.risk = 'UNKNOWN';
  delete raw.hazards[0].dimensions;
  raw.unified_safety.primary_threat = null;
  raw.unified_safety.vehicle_safety = null;
  raw.road.lanes = 'four';
  const model = view(raw);
  const first = model.tracks[0];
  assert.deepEqual([first.id, first.risk, first.ttc, first.conflict, first.trajectory], ['TRACK_002', null, null, false, []]);
  assert.equal(model.tracks.length, 10);
  assert.equal(model.tracks[1].risk, null);
  assert.deepEqual([model.hazards[0].length, model.hazards[0].width], [null, null]);
  assert.deepEqual([model.safety.threat, model.safety.minTtc, model.safety.conflicts], [null, null, null]);
  assert.deepEqual(model.road.lanes, []);
});

test('a car is drawn facing the way it moves over the ground', () => {
  const egoSpeed = 22.222; // 80 km/h
  const { tracks } = at(102);
  near(trackYaw(tracks.find((t) => t.id === 'TRACK_002'), egoSpeed, 0), -0.0115086, 1e-6); // atan2(-0.16, 13.902)
  // oncoming, beyond the median: faces the ego
  near(Math.abs(trackYaw(tracks.find((t) => t.id === 'TRACK_022'), egoSpeed, 0)), Math.PI, 0.01);
});

test('a car without a usable ground speed follows the road; a sideways velocity turns it at most 20 degrees', () => {
  const still = { vLon: -22.2, vLat: 0.5 };
  assert.equal(trackYaw(still, 22.222, 0.1), 0.1);
  assert.equal(trackYaw({ vLon: null, vLat: null }, 22.222, 0.1), 0.1);
  near(trackYaw({ vLon: -8.3, vLat: 10 }, 22.222, 0), (20 * Math.PI) / 180);
  near(trackYaw({ vLon: -8.3, vLat: -10 }, 22.222, 0), (-20 * Math.PI) / 180);
});

test('labels go to the primary threat and other tracks at risk, worst first, plus the one under the pointer', () => {
  assert.deepEqual([...labelledTracks(at(9))], ['TRACK_007']);
  assert.deepEqual([...labelledTracks(at(9), 'TRACK_004')], ['TRACK_007', 'TRACK_004']);
  assert.deepEqual([...labelledTracks(at(102))], []);

  const model = at(102);
  const set = (id, risk, ttc) => Object.assign(model.tracks.find((t) => t.id === id), { risk, ttc });
  set('TRACK_002', 'CAUTION', 4.1);
  set('TRACK_018', 'HIGH', 2.5);
  set('TRACK_020', 'CAUTION', 3.2);
  set('TRACK_021', 'CRITICAL', 1.1);
  assert.deepEqual([...labelledTracks(model, null, 3)], ['TRACK_021', 'TRACK_018', 'TRACK_020']);
});

test('the safety panel shows the unified state\'s own values', () => {
  assert.deepEqual(safetyPanel(at(9)), {
    risk: 'CRITICAL', action: 'EMERGENCY BRAKE', targetKmh: '0', lane: 'None',
    reason: 'TRACK_007 predicted collision in 1.05 s',
  });
  assert.deepEqual(safetyPanel(at(102)), {
    risk: 'HIGH', action: 'BRAKE', targetKmh: '60.1', lane: 'None',
    reason: 'PH_001 HIGH pothole 59.8 m ahead in ego_lane',
  });
  const raw = structuredClone(RAW[102]);
  raw.unified_safety.recommended_action = 'CONSIDER_LANE_CHANGE';
  raw.unified_safety.recommended_lane = 'left_lane';
  assert.deepEqual([safetyPanel(view(raw)).action, safetyPanel(view(raw)).lane], ['CONSIDER LANE CHANGE', 'Left lane']);
  assert.deepEqual(safetyPanel(view({ ...raw, unified_safety: null })),
    { risk: '—', action: '—', targetKmh: '—', lane: '—', reason: '' });
});

test('the primary threat is described from its own record: a vehicle by TTC, a pothole by severity and distance', () => {
  assert.deepEqual(primaryThreat(at(9)),
    { id: 'TRACK_007', kind: 'vehicle', risk: 'CRITICAL', facts: ['TTC 1.1 s', '5.1 m behind'] });
  assert.deepEqual(primaryThreat(at(102)),
    { id: 'PH_001', kind: 'road_hazard', risk: 'HIGH', facts: ['HIGH pothole', '59.8 m ahead'] });
  assert.equal(primaryThreat(at(5)), null);
});

const texts = (events) => events.map((e) => [e.text, e.detail ?? null, e.tone]);

test('no events from the first snapshot: there is nothing to compare it with', () => {
  assert.deepEqual(detectEvents(null, at(5)), []);
});

test('a track entering a conflict and the new recommendation are events', () => {
  assert.deepEqual(texts(detectEvents(at(5), at(6))), [
    ['TRACK_007 predicted conflict, TTC 1.5 s', null, 'HIGH'],
    ['Recommended action: BRAKE', 'TRACK_007 predicted collision in 1.50 s', 'HIGH'],
  ]);
});

test('risk changes are found across skipped snapshots too', () => {
  assert.deepEqual(texts(detectEvents(at(6), at(9))), [
    ['TRACK_007 risk HIGH → CRITICAL', null, 'CRITICAL'],
    ['Recommended action: EMERGENCY BRAKE', 'TRACK_007 predicted collision in 1.05 s', 'CRITICAL'],
  ]);
});

test('a newly mapped pothole is an event', () => {
  assert.deepEqual(texts(detectEvents(at(101), at(102))), [
    ['PH_001 discovered', 'HIGH pothole, 59.8 m ahead', 'HIGH'],
    ['Recommended action: BRAKE', 'PH_001 HIGH pothole 59.8 m ahead in ego_lane', 'HIGH'],
  ]);
});

test('a new primary threat with the same action is an event, and so is the end of the run', () => {
  assert.deepEqual(texts(detectEvents(at(102), at(1226))), [
    ['PH_002 discovered', 'MEDIUM pothole, 4.1 km behind', 'MEDIUM'],
    ['PH_003 discovered', 'LOW pothole, 3.8 km behind', 'LOW'],
    ['PH_004 discovered', 'MEDIUM pothole, 3.6 km behind', 'MEDIUM'],
    ['TRACK_122 predicted conflict, TTC 2.2 s', null, 'HIGH'],
    ['Recommended action: BRAKE', 'TRACK_122 predicted collision in 2.19 s', 'HIGH'],
    ['Simulation ended', null, 'info'],
  ]);
});

test('a track that enters a conflict is one event, not a conflict and a risk change', () => {
  const before = at(101);
  const after = at(102);
  Object.assign(after.tracks.find((t) => t.id === 'TRACK_002'), { risk: 'HIGH', ttc: 2.9, conflict: true });
  assert.deepEqual(texts(detectEvents(before, after)).slice(1, 2), [['TRACK_002 predicted conflict, TTC 2.9 s', null, 'HIGH']]);
  assert.equal(detectEvents(before, after).length, 3); // PH_001, the conflict, the recommendation
});

test('a new run is announced instead of compared with the old one', () => {
  const restarted = at(5);
  restarted.runId = 'a1b2c3d4e5f6';
  assert.deepEqual(texts(detectEvents(at(1226), restarted)), [['New simulation run', null, 'info']]);
});

test('a tween moves from where it is drawn to the new target over the given time', () => {
  const tw = tween([0, 10]);
  assert.deepEqual(sample(tw, 0), [0, 10]);
  retarget(tw, [10, 20], 1000, 200);
  assert.deepEqual(sample(tw, 1100), [5, 15]);
  assert.deepEqual(sample(tw, 1200), [10, 20]);
  assert.deepEqual(sample(tw, 5000), [10, 20]);
  retarget(tw, [10, 20], 5000, 200);
  retarget(tw, [30, 20], 5100, 200); // a new target mid-way: no jump
  assert.deepEqual(sample(tw, 5100), [10, 20]);
  assert.deepEqual(sample(tw, 5200), [20, 20]);
});

test('an angle is retargeted the short way round, so a car facing backwards never spins', () => {
  near(nearestAngle(3.1, -3.1), -3.1 + 2 * Math.PI, 1e-12);
  near(nearestAngle(-3.1, 3.1), 3.1 - 2 * Math.PI, 1e-12);
  assert.equal(nearestAngle(0.2, 0.3), 0.3);
});
