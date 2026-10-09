/**
 * RoadSense Autonomy: what to draw from a roadsense.live.v1 snapshot (docs/live-state.md).
 *
 * Presentation only. Tracking, prediction, TTC, risk, hazard severity and the safety decision all come from RoadSense
 * in the snapshot and are shown as they are; this module only maps them to the scene's axes, picks what to label,
 * formats text, finds what changed between two snapshots and eases drawn positions between them.
 * No DOM or Three.js here, so Node's test runner can run it (tests/js/view.test.mjs).
 *
 * Axes: RoadSense's ego frame has x (longitudinal) ahead and y (lateral) to the left, z up. The scene is three.js's
 * right-handed frame with the camera's conventions: +x right, +y up, -z ahead. So longitudinal -> -z, lateral -> -x,
 * and a heading (radians, positive to the left) is a rotation about +y by the same angle. Everything placed relative to
 * the car (tracks, trajectories, hazards, lanes) goes through toScene.
 */

export const RISKS = ['SAFE', 'CAUTION', 'HIGH', 'CRITICAL'];
const LANE_NAMES = { left_lane: 'Left lane', right_lane: 'Right lane' };
const MIN_GROUND_SPEED_MPS = 3; // slower than this, a velocity's direction is mostly noise
const MAX_YAW_OFF_ROAD = (20 * Math.PI) / 180;

const num = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const str = (value) => (typeof value === 'string' && value ? value : null);
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const obj = (value) => (isObject(value) ? value : {});
const list = (value) => (Array.isArray(value) ? value : []);
const risk = (value) => (RISKS.includes(value) ? value : null);
const wrap = (angle) => Math.atan2(Math.sin(angle), Math.cos(angle));

export function toScene(longitudinal, lateral) {
  return { x: -lateral, z: -longitudinal };
}

/** The snapshot as plain, checked values: a number is finite or null, a list is a list. Entries that can't be placed
 * (no id or no position) are left out; nothing missing is filled in with a guess. */
export function view(snapshot) {
  const ego = obj(snapshot.ego);
  const simulation = obj(snapshot.simulation);
  return {
    t: num(snapshot.timestamp),
    runId: str(simulation.run_id),
    sequence: num(simulation.sequence),
    active: simulation.active === true,
    ego: {
      speedKmh: num(ego.speed_kmh),
      speedMps: num(ego.speed_mps),
      lane: num(ego.lane), // the driving controller's target lane: 0 right, 1 middle, 2 left
      maneuver: str(ego.maneuver),
      steeringRad: num(ego.steering_rad), // positive steers right
      x: num(obj(ego.position).x), // world frame, only to keep the lane markings moving with the car
      y: num(obj(ego.position).y),
      headingDeg: num(ego.heading_deg),
    },
    road: roadView(snapshot.road),
    tracks: list(snapshot.tracks).map(trackView).filter(Boolean),
    hazards: list(snapshot.hazards).map(hazardView).filter(Boolean),
    safety: safetyView(snapshot.unified_safety),
  };
}

function roadView(road) {
  if (!isObject(road)) return null;
  return {
    laneWidth: num(road.lane_width_m),
    headingDeg: num(road.heading_deg), // the road's direction relative to the car's heading, positive left
    lanes: list(road.lanes).map(obj).filter((lane) => num(lane.center_lateral_m) !== null)
      .map((lane) => ({ relation: str(lane.relation), lateral: lane.center_lateral_m, driving: lane.driving !== false })),
  };
}

function trackView(raw) {
  const track = obj(raw);
  const position = obj(track.relative_position);
  const velocity = obj(track.relative_velocity);
  const collision = obj(track.collision);
  const id = str(track.track_id);
  const lon = num(position.longitudinal_m);
  const lat = num(position.lateral_m);
  if (!id || lon === null || lat === null) return null;
  return {
    id,
    type: str(track.object_type),
    lon,
    lat,
    vLon: num(velocity.longitudinal_mps),
    vLat: num(velocity.lateral_mps),
    distance: num(track.distance_m),
    laneRelation: str(track.lane_relation),
    risk: risk(collision.risk),
    ttc: num(collision.ttc_s),
    conflict: collision.conflict === true,
    trajectory: list(obj(track.prediction).trajectory).map(obj)
      .map((point) => ({ t: num(point.t_s), lon: num(point.longitudinal_m), lat: num(point.lateral_m) }))
      .filter((point) => point.t !== null && point.lon !== null && point.lat !== null),
  };
}

function hazardView(raw) {
  const hazard = obj(raw);
  const relative = obj(hazard.relative);
  const dimensions = obj(hazard.dimensions);
  const id = str(hazard.hazard_id);
  const lon = num(relative.longitudinal_m);
  const lat = num(relative.lateral_m);
  if (!id || lon === null || lat === null) return null;
  return {
    id,
    type: str(hazard.type),
    severity: str(hazard.severity),
    confidence: num(hazard.confidence),
    status: str(hazard.status),
    lon,
    lat,
    distance: num(relative.distance_m),
    direction: str(relative.direction),
    laneRelation: str(relative.lane_relation),
    length: num(dimensions.length_m),
    width: num(dimensions.width_m),
  };
}

function safetyView(raw) {
  if (!isObject(raw)) return null;
  const vehicle = isObject(raw.vehicle_safety) ? raw.vehicle_safety : null;
  const threat = obj(raw.primary_threat);
  return {
    risk: risk(raw.overall_risk),
    action: str(raw.recommended_action),
    targetKmh: num(raw.recommended_speed_kmh),
    lane: str(raw.recommended_lane),
    reason: str(raw.reason),
    threat: str(threat.id) ? { type: str(threat.type), id: threat.id, reason: str(threat.reason) } : null,
    minTtc: vehicle && num(vehicle.minimum_ttc_s),
    conflicts: vehicle && num(vehicle.active_conflicts),
    roadHazards: list(obj(raw.road_safety).hazards).map(obj).filter((hazard) => str(hazard.hazard_id))
      .map((hazard) => ({
        id: hazard.hazard_id,
        type: str(hazard.type),
        severity: str(hazard.severity),
        urgency: risk(hazard.urgency),
        distance: num(hazard.distance_m),
        timeToHazard: num(hazard.time_to_hazard_s),
      })),
  };
}

/** How far along the road the car is (m, world frame): its world position on the road's world direction, which is the
 * car's heading plus the road's heading relative to it. Lane markings are drawn at this offset. */
export function roadDistance(model) {
  const { x, y, headingDeg } = model.ego;
  const roadHeading = model.road?.headingDeg ?? null;
  if (x === null || y === null || headingDeg === null || roadHeading === null) return null;
  const direction = ((headingDeg + roadHeading) * Math.PI) / 180;
  return x * Math.cos(direction) + y * Math.sin(direction);
}

/** The direction to draw a tracked car in (radians, positive left, as the road's heading): the way it moves over the
 * ground, which is its relative velocity plus the car's own. Without a usable ground speed it follows the road, and a
 * noisy sideways velocity turns it at most MAX_YAW_OFF_ROAD away from the road's direction (or its reverse). */
export function trackYaw(track, egoSpeedMps, roadHeading) {
  if (track.vLon === null || track.vLat === null || egoSpeedMps === null) return roadHeading;
  const along = track.vLon + egoSpeedMps;
  if (Math.hypot(along, track.vLat) < MIN_GROUND_SPEED_MPS) return roadHeading;
  let off = wrap(Math.atan2(track.vLat, along) - roadHeading);
  const oncoming = Math.abs(off) > Math.PI / 2;
  if (oncoming) off = wrap(off - Math.PI);
  return roadHeading + (oncoming ? Math.PI : 0) + Math.min(MAX_YAW_OFF_ROAD, Math.max(-MAX_YAW_OFF_ROAD, off));
}

/** The tracks to label: the primary threat if it is a vehicle, then the others at risk, worst risk and soonest TTC
 * first, at most max of them; and the one under the pointer. */
export function labelledTracks(model, hoveredId = null, max = 3) {
  const threatId = model.safety?.threat?.type === 'vehicle' ? model.safety.threat.id : null;
  const rank = (track) => RISKS.indexOf(track.risk);
  const ttc = (track) => track.ttc ?? Number.MAX_VALUE;
  const ids = new Set(model.tracks.filter((track) => rank(track) > 0 || track.id === threatId)
    .sort((a, b) => (b.id === threatId) - (a.id === threatId) || rank(b) - rank(a) || ttc(a) - ttc(b))
    .slice(0, max).map((track) => track.id));
  if (hoveredId && model.tracks.some((track) => track.id === hoveredId)) ids.add(hoveredId);
  return ids;
}

export const actionText = (action) => (action ? action.replaceAll('_', ' ') : '—');
export const speedText = (kmh) => (kmh === null ? '—' : Number.isInteger(kmh) ? String(kmh) : kmh.toFixed(1));
export const secondsText = (s) => (s === null ? '—' : `${s.toFixed(1)} s`);

export function distanceText(m) {
  if (m === null) return '—';
  if (m >= 1000) return `${(m / 1000).toFixed(1)} km`;
  return `${m < 100 ? m.toFixed(1) : Math.round(m)} m`;
}

/** The unified safety state's own values, as text for the safety panel. */
export function safetyPanel(model) {
  const safety = model.safety;
  if (!safety) return { risk: '—', action: '—', targetKmh: '—', lane: '—', reason: '' };
  return {
    risk: safety.risk ?? '—',
    action: actionText(safety.action),
    targetKmh: speedText(safety.targetKmh),
    lane: safety.lane ? LANE_NAMES[safety.lane] ?? safety.lane : 'None',
    reason: safety.reason ?? '',
  };
}

/** The unified state's primary threat, described from its own record: a vehicle's track (TTC, distance) or the
 * road hazard's entry in the safety state (severity, distance). */
export function primaryThreat(model) {
  const threat = model.safety?.threat;
  if (!threat) return null;
  if (threat.type === 'vehicle') {
    const track = model.tracks.find((t) => t.id === threat.id);
    return {
      id: threat.id,
      kind: 'vehicle',
      risk: track?.risk ?? model.safety.risk,
      facts: track ? [`TTC ${secondsText(track.ttc)}`, `${distanceText(track.distance)} ${track.lon < 0 ? 'behind' : 'ahead'}`] : [],
    };
  }
  const hazard = model.safety.roadHazards.find((h) => h.id === threat.id);
  return {
    id: threat.id,
    kind: threat.type,
    risk: hazard?.urgency ?? model.safety.risk,
    facts: hazard ? [`${hazard.severity ?? ''} ${hazard.type ?? 'hazard'}`.trim(), `${distanceText(hazard.distance)} ahead`] : [],
  };
}

/** What changed from the previous snapshot drawn to the next, for the event feed: newly mapped hazards, tracks entering
 * a predicted conflict, track risk changes, a new recommendation or primary threat, and the run's end. Snapshots in
 * between may have been skipped; the comparison doesn't depend on them. tone is a risk level, a hazard severity or
 * 'info'. */
export function detectEvents(previous, next) {
  if (!previous) return [];
  const t = next.t;
  if (previous.runId !== next.runId) return [{ t, text: 'New simulation run', tone: 'info' }];
  const events = [];
  const mapped = new Set(previous.hazards.map((hazard) => hazard.id));
  for (const hazard of next.hazards) {
    if (!mapped.has(hazard.id)) {
      events.push({
        t,
        text: `${hazard.id} discovered`,
        detail: `${hazard.severity ?? ''} ${hazard.type ?? 'hazard'}, ${distanceText(hazard.distance)} ${hazard.direction ?? ''}`.trim(),
        tone: hazard.severity,
      });
    }
  }
  const before = new Map(previous.tracks.map((track) => [track.id, track]));
  const entering = (track) => track.conflict && !before.get(track.id)?.conflict;
  for (const track of next.tracks.filter(entering)) {
    events.push({ t, text: `${track.id} predicted conflict, TTC ${secondsText(track.ttc)}`, tone: track.risk });
  }
  for (const track of next.tracks) {
    const old = before.get(track.id);
    if (old?.risk && track.risk && old.risk !== track.risk && !entering(track)) {
      events.push({ t, text: `${track.id} risk ${old.risk} → ${track.risk}`, tone: track.risk });
    }
  }
  const [a, b] = [previous.safety, next.safety];
  if (b && (!a || a.risk !== b.risk || a.action !== b.action || a.threat?.id !== b.threat?.id)) {
    events.push({ t, text: `Recommended action: ${actionText(b.action)}`, detail: b.reason ?? undefined, tone: b.risk });
  }
  if (previous.active && !next.active) events.push({ t, text: 'Simulation ended', tone: 'info' });
  return events;
}

/** Easing drawn values (positions, headings) between snapshots, which arrive ~5 times a second while the scene is drawn
 * ~60 times: a tween goes in a straight line from what is drawn now to the newest target over the given time. */
export function tween(values) {
  return { from: [...values], to: [...values], start: 0, duration: 0 };
}

export function sample(tw, now) {
  const k = tw.duration > 0 ? Math.min(1, Math.max(0, (now - tw.start) / tw.duration)) : 1;
  return tw.to.map((to, i) => tw.from[i] + (to - tw.from[i]) * k);
}

export function retarget(tw, values, now, duration) {
  tw.from = sample(tw, now);
  tw.to = [...values];
  tw.start = now;
  tw.duration = duration;
}

/** to, plus or minus whole turns, as close as possible to from: for tweening a heading across ±π. */
export function nearestAngle(from, to) {
  return to + 2 * Math.PI * Math.round((from - to) / (2 * Math.PI));
}
