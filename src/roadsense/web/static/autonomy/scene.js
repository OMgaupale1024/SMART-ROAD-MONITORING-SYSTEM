/**
 * RoadSense Autonomy: the 3D view (three.js, WebGL 2).
 *
 * EGO_ROADSENSE stays at the scene's origin, its rear axle on the ground; the road, the tracked vehicles and the hazards
 * are placed around it from the latest snapshot through view.js's toScene (ahead -z, left -x), so all of them share one
 * transform. Between snapshots (~5 a second) their drawn positions are eased by tweens; RoadSense's values themselves are
 * never changed. Only what RoadSense sent is drawn: no extra vehicles, paths or collision shapes. A conflict shows as
 * the risk colour on both cars' outlines and on the other car's predicted path.
 */
import * as THREE from '../vendor/three/three.module.min.js';
import {
  distanceText, labelledTracks, nearestAngle, retarget, roadDistance, sample, secondsText, toScene, trackYaw, tween,
} from './view.js';

// RoadSense's own sizes (risk.py): the ego's body is 4.9 x 1.8 m with its centre 1.44 m ahead of its origin; a tracked
// vehicle is taken as 5 x 1.9 m, since a radar target has no size.
const EGO = { length: 4.9, width: 1.8, centre: 1.44 };
const OTHER = { length: 5.0, width: 1.9 };
const BEHIND_M = 45; // road drawn from 45 m behind the car to 230 m ahead, where the fog has hidden it
const AHEAD_M = 230;
const DASH_M = 3;
const DASH_PERIOD_M = 12;
const LINE_M = 0.15;
const HAZARD_LABEL_AHEAD_M = 160;
const MAX_PATH_POINTS = 16; // the car's position and up to 15 trajectory points (RoadSense sends 10)
// A track's predicted trajectory: faint dots at RoadSense's points while it is SAFE (no conflict), a ribbon in its risk
// colour once a conflict is predicted. Ribbon width (m) and opacity next to the car:
const RIBBON = { CAUTION: [0.3, 0.8], HIGH: [0.34, 0.9], CRITICAL: [0.38, 1] };
const DOT_OPACITY = 0.6;

const css = getComputedStyle(document.documentElement);
const color = (name) => new THREE.Color(css.getPropertyValue(name).trim());

export function createScene(canvas, labelLayer) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const scene = new THREE.Scene();
  scene.background = color('--scene');
  scene.fog = new THREE.Fog(scene.background, 95, AHEAD_M - 15);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 700);
  camera.position.set(0, 13, 25); // behind and above the car: ~9 m behind it stay in view (the rear radar's threats)
  camera.lookAt(0, 0, -10);
  scene.add(new THREE.HemisphereLight(0xdde5ee, 0x1b1e22, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 2.2);
  sun.position.set(-6, 14, 9);
  scene.add(sun);

  const colors = {
    SAFE: color('--safe'), CAUTION: color('--caution'), HIGH: color('--high'), CRITICAL: color('--critical'),
    LOW: color('--sev-low'), MEDIUM: color('--sev-medium'), neutral: color('--ink-3'),
  };
  const basic = (c, extra = {}) => new THREE.MeshBasicMaterial({ color: c, ...extra });
  const decal = (c, opacity = 1) => basic(c, { transparent: true, opacity, depthWrite: false, side: THREE.DoubleSide });
  const lit = (c, roughness = 0.5, metalness = 0.1) => new THREE.MeshStandardMaterial({ color: c, roughness, metalness });
  const other = color('--paint-other');
  const paints = {
    ego: lit(color('--paint-ego'), 0.38, 0.08),
    none: lit(other), // risk unknown or SAFE
    SAFE: lit(other),
    CAUTION: lit(other.clone().lerp(colors.CAUTION, 0.45)),
    HIGH: lit(other.clone().lerp(colors.HIGH, 0.6)),
    CRITICAL: lit(other.clone().lerp(colors.CRITICAL, 0.7)),
  };
  const materials = {
    glass: lit(color('--glass'), 0.22, 0.35),
    tyre: lit(0x0f1113, 0.9, 0),
    tail: basic(0x9a2a2e),
    head: basic(0xc9d1da),
    shadow: decal(0x000000, 0.55),
    frame: Object.fromEntries(['SAFE', 'CAUTION', 'HIGH', 'CRITICAL'].map((r) => [r, decal(colors[r], r === 'SAFE' ? 0.55 : 0.95)])),
    rim: Object.fromEntries(['LOW', 'MEDIUM', 'HIGH'].map((s) => [s, decal(colors[s] ?? colors.neutral)])),
    rimUnknown: decal(colors.neutral),
    pothole: basic(0x0b0c0e),
    path: new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false, side: THREE.DoubleSide }),
    dots: new THREE.PointsMaterial({
      size: 0.5, vertexColors: true, transparent: true, depthWrite: false, map: dotTexture(),
    }),
  };
  materials.shadow.map = shadowTexture();

  const cars = { ego: carGeometry(EGO), other: carGeometry(OTHER) };
  const frames = { ego: frameGeometry(EGO), other: frameGeometry(OTHER) };
  const potholeGeometry = new THREE.CircleGeometry(1, 40).rotateX(-Math.PI / 2);
  const rimGeometry = new THREE.RingGeometry(0.84, 1, 40).rotateX(-Math.PI / 2);
  const stemGeometry = new THREE.BoxGeometry(0.04, 1.3, 0.04).translate(0, 0.65, 0);
  const tipGeometry = new THREE.OctahedronGeometry(0.17).translate(0, 1.45, 0);

  // the world beyond the road: plain ground to stand vehicles on (no RoadSense data)
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(800, 900).rotateX(-Math.PI / 2), basic(color('--ground')));
  ground.position.set(0, -0.02, -300);
  scene.add(ground);

  // road: pivot turns by the road's heading relative to the car, about the car's origin; body slides across
  const roadPivot = new THREE.Group();
  const roadBody = new THREE.Group();
  const dashes = new THREE.Group();
  roadPivot.add(roadBody);
  scene.add(roadPivot);
  let roadKey = null;
  const roadTw = tween([0, 0, 0]); // body x, heading, distance along the road

  const ego = makeCar(cars.ego, paints.ego, frames.ego);
  ego.group.position.z = -EGO.centre;
  ego.group.visible = false;
  scene.add(ego.group);

  const tracks = new Map(); // track id -> its drawn objects
  const hazards = new Map(); // hazard id -> its drawn objects
  const labels = new Map(); // key -> label element
  let model = null;
  let lastUpdate = null;
  let period = 200; // ms between snapshots, smoothed
  let hoveredId = null;
  let pointer = null;
  let size = { width: 0, height: 0 }; // the canvas in CSS pixels, from the ResizeObserver
  const raycaster = new THREE.Raycaster();
  const projected = new THREE.Vector3();

  function makeCar(parts, paint, frame) {
    const group = new THREE.Group();
    const body = new THREE.Mesh(parts.body, paint);
    const roof = new THREE.Mesh(parts.roof, paint);
    const wheels = parts.wheels.map(([x, z]) => {
      const wheel = new THREE.Mesh(parts.wheel, materials.tyre);
      wheel.position.set(x, parts.wheelRadius, z);
      return wheel;
    });
    const outline = new THREE.Mesh(frame, materials.frame.SAFE);
    outline.position.y = 0.025;
    group.add(body, roof, new THREE.Mesh(parts.cabin, materials.glass), ...wheels,
      new THREE.Mesh(parts.tail, materials.tail), new THREE.Mesh(parts.head, materials.head),
      new THREE.Mesh(parts.shadow, materials.shadow), outline);
    return { group, body, roof, outline, front: wheels.filter((w) => w.position.z < 0) };
  }

  function buildRoad(road) {
    for (const child of [...roadBody.children]) {
      roadBody.remove(child);
      child.geometry?.dispose();
    }
    for (const child of [...dashes.children]) {
      dashes.remove(child);
      child.geometry.dispose();
    }
    const width = road.laneWidth ?? 3.75;
    const first = road.lanes[0].lateral;
    // lane centres in the body's frame: metres to the right of the first lane (scene +x), left to right
    const lanes = road.lanes.map((lane) => ({ x: first - lane.lateral, driving: lane.driving })).sort((a, b) => a.x - b.x);
    const left = lanes[0].x - width / 2;
    const right = lanes.at(-1).x + width / 2;
    const z0 = -AHEAD_M;
    const z1 = BEHIND_M;
    const asphalt = new THREE.Mesh(quads([[left, right, z0, z1]]), basic(color('--road')));
    const shoulders = lanes.filter((lane) => !lane.driving).map((lane) => [lane.x - width / 2, lane.x + width / 2, z0, z1]);
    const solid = [];
    const dashed = [];
    lanes.forEach((lane, i) => {
      const next = lanes[i + 1];
      if (i === 0 && lane.driving) solid.push(lane.x - width / 2);
      if (!next) return;
      const between = (lane.x + next.x) / 2;
      if (lane.driving && next.driving) dashed.push(between);
      else if (lane.driving || next.driving) solid.push(between);
    });
    const rects = (xs, from, to) => xs.map((x) => [x - LINE_M / 2, x + LINE_M / 2, from, to]);
    const paint = basic(color('--road-paint'));
    roadBody.add(asphalt, lift(new THREE.Mesh(quads(shoulders), basic(color('--road-shoulder'))), 0.004),
      lift(new THREE.Mesh(quads(rects(solid, z0, z1)), paint), 0.01));
    const stripes = [];
    for (let z = z1 + DASH_PERIOD_M; z > z0 - DASH_PERIOD_M; z -= DASH_PERIOD_M) stripes.push(...rects(dashed, z - DASH_M, z));
    dashes.add(lift(new THREE.Mesh(quads(stripes), paint), 0.01));
    roadBody.add(dashes);
    // the carriageway's crash barriers, as in the Webots world: where the road ends, not RoadSense data
    for (const x of [left - 0.45, right + 0.45]) {
      const barrier = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.55, z1 - z0).translate(0, 0.275, (z0 + z1) / 2),
        lit(color('--barrier'), 0.85));
      barrier.position.x = x;
      roadBody.add(barrier);
    }
  }

  function updateRoad(now, duration) {
    const road = model.road;
    roadPivot.visible = Boolean(road && road.lanes.length);
    if (!roadPivot.visible) return;
    const key = JSON.stringify([road.laneWidth, road.lanes.map((l) => [Math.round((l.lateral - road.lanes[0].lateral) * 20), l.driving])]);
    if (key !== roadKey) {
      buildRoad(road);
      roadKey = key;
    }
    const heading = ((road.headingDeg ?? 0) * Math.PI) / 180;
    const along = roadDistance(model) ?? sample(roadTw, now)[2];
    retarget(roadTw, [-road.lanes[0].lateral, heading, along], now, duration);
  }

  function updateTracks(now, duration) {
    const roadHeading = ((model.road?.headingDeg ?? 0) * Math.PI) / 180;
    const seen = new Set();
    for (const track of model.tracks) {
      seen.add(track.id);
      const { x, z } = toScene(track.lon, track.lat);
      const yaw = trackYaw(track, model.ego.speedMps, roadHeading);
      let drawn = tracks.get(track.id);
      if (!drawn) {
        drawn = { car: makeCar(cars.other, paints.none, frames.other), path: makePath() };
        drawn.car.body.userData.trackId = track.id;
        drawn.tw = tween([x, z, yaw]);
        scene.add(drawn.car.group, drawn.path);
        tracks.set(track.id, drawn);
      } else {
        const [, , drawnYaw] = sample(drawn.tw, now);
        retarget(drawn.tw, [x, z, nearestAngle(drawnYaw, yaw)], now, duration);
      }
      drawn.track = track;
      const atRisk = track.risk && track.risk !== 'SAFE';
      drawn.car.body.material = drawn.car.roof.material = paints[track.risk ?? 'none'];
      drawn.car.outline.visible = Boolean(atRisk);
      if (atRisk) drawn.car.outline.material = materials.frame[track.risk];
      // oncoming traffic at no risk would streak its relative path down the other carriageway: left out
      const oncoming = Math.cos(yaw - roadHeading) < 0;
      shapePath(drawn.path, track, atRisk || !oncoming);
    }
    for (const [id, drawn] of tracks) {
      if (seen.has(id)) continue; // RoadSense no longer tracks it: neither do we
      scene.remove(drawn.car.group, drawn.path);
      for (const part of drawn.path.children) part.geometry.dispose();
      tracks.delete(id);
    }
  }

  function updateHazards(now, duration) {
    const seen = new Set();
    for (const hazard of model.hazards) {
      seen.add(hazard.id);
      const { x, z } = toScene(hazard.lon, hazard.lat);
      let drawn = hazards.get(hazard.id);
      if (!drawn) {
        const group = new THREE.Group();
        const hole = lift(new THREE.Mesh(potholeGeometry, materials.pothole), 0.015);
        const rim = lift(new THREE.Mesh(rimGeometry), 0.02);
        const stem = new THREE.Mesh(stemGeometry);
        const tip = new THREE.Mesh(tipGeometry);
        group.add(hole, rim, stem, tip);
        drawn = { group, hole, rim, stem, tip, tw: tween([x, z]) };
        scene.add(group);
        hazards.set(hazard.id, drawn);
      } else {
        retarget(drawn.tw, [x, z], now, duration);
      }
      drawn.hazard = hazard;
      // its real size: length along the road, width across (a nominal 1 x 0.8 m if RoadSense gave none)
      const scale = [(hazard.width ?? 0.8) / 2, 1, (hazard.length ?? 1) / 2];
      drawn.hole.scale.set(...scale);
      drawn.rim.scale.set(...scale);
      drawn.rim.material = drawn.stem.material = drawn.tip.material = materials.rim[hazard.severity] ?? materials.rimUnknown;
    }
    for (const [id, drawn] of hazards) {
      if (seen.has(id)) continue;
      scene.remove(drawn.group);
      hazards.delete(id);
    }
  }

  function makePath() {
    const vertices = (count) => {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(count * 3), 3));
      geometry.setAttribute('color', new THREE.BufferAttribute(new Float32Array(count * 4), 4));
      return geometry;
    };
    const strip = vertices(MAX_PATH_POINTS * 2);
    const index = [];
    for (let i = 0; i < MAX_PATH_POINTS - 1; i += 1) index.push(2 * i, 2 * i + 2, 2 * i + 1, 2 * i + 1, 2 * i + 2, 2 * i + 3);
    strip.setIndex(index);
    const ribbon = new THREE.Mesh(strip, materials.path);
    const dots = new THREE.Points(vertices(MAX_PATH_POINTS), materials.dots);
    const group = new THREE.Group();
    for (const part of [ribbon, dots]) {
      part.frustumCulled = false; // its points change with every snapshot
      part.renderOrder = 1;
      group.add(part);
    }
    return group;
  }

  /** The track's predicted trajectory on the road, fading with time: from the car through each point RoadSense sent,
   * relative to the car so it travels with the car's drawn position. */
  function shapePath(group, track, shown) {
    const [ribbon, dots] = group.children;
    const points = [{ x: 0, z: 0 }, ...track.trajectory.slice(0, MAX_PATH_POINTS - 1)
      .map((p) => toScene(p.lon - track.lon, p.lat - track.lat))];
    const reach = Math.hypot(points.at(-1).x, points.at(-1).z);
    const visible = shown && points.length > 1 && reach > 0.5; // a car moving with the ego has nowhere to go
    ribbon.visible = visible && track.risk in RIBBON;
    dots.visible = visible && !ribbon.visible;
    if (dots.visible) {
      const position = dots.geometry.attributes.position;
      const tint = dots.geometry.attributes.color;
      points.slice(1).forEach((p, i) => {
        position.setXYZ(i, p.x, 0.05, p.z);
        tint.setXYZW(i, colors.neutral.r, colors.neutral.g, colors.neutral.b, DOT_OPACITY * (1 - i / (points.length - 1)));
      });
      position.needsUpdate = tint.needsUpdate = true;
      dots.geometry.setDrawRange(0, points.length - 1);
    }
    if (!ribbon.visible) return;
    const [width, alpha] = RIBBON[track.risk];
    const c = colors[track.risk];
    const position = ribbon.geometry.attributes.position;
    const tint = ribbon.geometry.attributes.color;
    points.forEach((p, i) => {
      const a = points[Math.max(0, i - 1)];
      const b = points[Math.min(points.length - 1, i + 1)];
      const length = Math.hypot(b.x - a.x, b.z - a.z) || 1;
      const nx = (-(b.z - a.z) / length) * (width / 2);
      const nz = ((b.x - a.x) / length) * (width / 2);
      position.setXYZ(2 * i, p.x + nx, 0.03, p.z + nz);
      position.setXYZ(2 * i + 1, p.x - nx, 0.03, p.z - nz);
      const fade = alpha * (1 - i / (points.length - 1)) ** 1.15;
      tint.setXYZW(2 * i, c.r, c.g, c.b, fade);
      tint.setXYZW(2 * i + 1, c.r, c.g, c.b, fade);
    });
    position.needsUpdate = tint.needsUpdate = true;
    ribbon.geometry.setDrawRange(0, (points.length - 1) * 6);
  }

  function label(key, x, y, z, parts, tone) {
    let element = labels.get(key);
    if (!element) {
      element = document.createElement('div');
      labelLayer.append(element);
      labels.set(key, element);
    }
    element.dataset.used = '1';
    const content = JSON.stringify([tone, parts]);
    if (element.dataset.content !== content) { // text only (textContent): ids and words come from the network
      element.dataset.content = content;
      element.className = `tag tone-${tone ?? 'none'}`;
      element.replaceChildren(...parts.map(([tag, text, cls]) => {
        const child = document.createElement(tag);
        child.textContent = text;
        if (cls) child.className = cls;
        return child;
      }));
    }
    projected.set(x, y, z).project(camera);
    const px = ((projected.x + 1) / 2) * size.width;
    const py = ((1 - projected.y) / 2) * size.height;
    const inside = projected.z < 1 && px > -40 && px < size.width + 40 && py > 0 && py < size.height + 20;
    element.style.display = inside ? '' : 'none';
    if (inside) element.style.transform = `translate(${px.toFixed(1)}px, ${py.toFixed(1)}px) translate(-50%, -100%)`;
  }

  function drawLabels(now) {
    for (const element of labels.values()) element.dataset.used = '';
    if (model) {
      const threat = model.safety?.threat;
      for (const id of labelledTracks(model, hoveredId)) {
        const drawn = tracks.get(id);
        if (!drawn) continue;
        const [x, z] = sample(drawn.tw, now);
        const { track } = drawn;
        const parts = [['b', id]];
        if (track.ttc !== null) parts.push(['span', `TTC ${secondsText(track.ttc)}`]);
        parts.push(['span', track.risk ?? 'risk unknown', 'tone']);
        if (id === hoveredId && track.distance !== null) parts.push(['span', distanceText(track.distance)]);
        label(`track:${id}`, x, 2.1, z, parts, track.risk);
      }
      for (const [id, drawn] of hazards) {
        const { hazard } = drawn;
        if (hazard.lon < -2 || hazard.lon > HAZARD_LABEL_AHEAD_M) continue;
        const [x, z] = sample(drawn.tw, now);
        const primary = threat?.id === id ? ' primary' : '';
        label(`hazard:${id}`, x, 1.8, z, [['b', id], ['span', hazard.severity ?? '', 'tone'],
          ['span', distanceText(hazard.distance)]], `${hazard.severity}${primary}`);
      }
    }
    for (const [key, element] of labels) {
      if (element.dataset.used) continue;
      element.remove();
      labels.delete(key);
    }
  }

  function resize() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    size = { width, height };
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    // keep at least ~56 degrees across, so the adjacent lanes stay in view on narrow screens
    camera.fov = Math.max(38, (2 * Math.atan(Math.tan((28 * Math.PI) / 180) / camera.aspect) * 180) / Math.PI);
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(canvas);
  resize();

  canvas.addEventListener('pointermove', (event) => {
    const rect = canvas.getBoundingClientRect();
    pointer = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
  });
  canvas.addEventListener('pointerleave', () => {
    pointer = null;
    hoveredId = null;
  });

  return {
    /** Draw this view model from now on; now is when its snapshot arrived (performance.now()). */
    update(next, now) {
      if (lastUpdate !== null) period = Math.min(500, Math.max(40, 0.8 * period + 0.2 * (now - lastUpdate)));
      lastUpdate = now;
      model = next;
      const duration = 1.2 * period; // a little longer than the gap, so a car keeps moving until the next snapshot
      ego.group.visible = true;
      const risk = model.safety?.risk;
      ego.outline.visible = Boolean(risk);
      if (risk) ego.outline.material = materials.frame[risk];
      for (const wheel of ego.front) wheel.rotation.y = -(model.ego.steeringRad ?? 0); // positive steers right
      updateRoad(now, duration);
      updateTracks(now, duration);
      updateHazards(now, duration);
    },

    render(now) {
      if (model) {
        const [bodyX, heading, along] = sample(roadTw, now);
        roadPivot.rotation.y = heading;
        roadBody.position.x = bodyX;
        dashes.position.z = ((along % DASH_PERIOD_M) + DASH_PERIOD_M) % DASH_PERIOD_M;
        for (const drawn of tracks.values()) {
          const [x, z, yaw] = sample(drawn.tw, now);
          drawn.car.group.position.set(x, 0, z);
          drawn.car.group.rotation.y = yaw;
          drawn.path.position.set(x, 0, z);
        }
        for (const drawn of hazards.values()) {
          const [x, z] = sample(drawn.tw, now);
          const { lon } = drawn.hazard;
          drawn.group.visible = lon > -BEHIND_M && lon < AHEAD_M; // passed ones stay mapped, just out of view
          drawn.group.position.set(x, 0, z);
          drawn.group.rotation.y = heading;
        }
        if (pointer) {
          raycaster.setFromCamera(pointer, camera);
          const [hit] = raycaster.intersectObjects([...tracks.values()].map((d) => d.car.body), false);
          hoveredId = hit?.object.userData.trackId ?? null;
        }
      }
      drawLabels(now);
      renderer.render(scene, camera);
    },
  };
}

function lift(mesh, y) {
  mesh.position.y = y;
  return mesh;
}

/** Flat rectangles on the ground, [x0, x1, z0, z1] each, facing up. */
function quads(rects) {
  const position = [];
  const index = [];
  for (const [x0, x1, z0, z1] of rects) {
    const i = position.length / 3;
    position.push(x0, 0, z0, x1, 0, z0, x1, 0, z1, x0, 0, z1);
    index.push(i, i + 2, i + 1, i, i + 3, i + 2);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(position, 3));
  geometry.setIndex(index);
  return geometry;
}

/** A plain sedan made of a few extruded side profiles, its centre at the origin, its front toward -z. Sizes in metres;
 * profile points are [fraction of the length from the rear, height]. */
function carGeometry({ length, width }) {
  const bevel = 0.06;
  const extrude = (points, across) => {
    const shape = new THREE.Shape(points.map(([f, y]) => new THREE.Vector2(bevel + f * (length - 2 * bevel), y)));
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: across - 2 * bevel, bevelEnabled: true, bevelThickness: bevel, bevelSize: bevel, bevelSegments: 2,
    });
    return geometry.translate(-length / 2, 0, -(across - 2 * bevel) / 2).rotateY(Math.PI / 2);
  };
  const wheelRadius = 0.33;
  const axle = 0.3 * length; // axles 20% of the length in from each end
  const track = width / 2 - 0.15;
  return {
    body: extrude([[0, 0.36], [0.01, 0.8], [0.06, 0.9], [0.24, 0.95], [0.67, 0.95], [0.87, 0.86], [0.97, 0.74],
      [1, 0.6], [1, 0.38], [0.97, 0.3], [0.03, 0.3]], width),
    cabin: extrude([[0.25, 0.9], [0.37, 1.33], [0.61, 1.35], [0.74, 0.9]], width * 0.84),
    roof: extrude([[0.39, 1.31], [0.4, 1.39], [0.59, 1.4], [0.6, 1.33]], width * 0.78),
    wheel: new THREE.CylinderGeometry(wheelRadius, wheelRadius, 0.26, 20).rotateZ(Math.PI / 2),
    wheelRadius,
    wheels: [[-track, axle], [track, axle], [-track, -axle], [track, -axle]],
    tail: new THREE.BoxGeometry(width * 0.82, 0.07, 0.03).translate(0, 0.8, length / 2),
    head: new THREE.BoxGeometry(width * 0.78, 0.06, 0.03).translate(0, 0.66, -length / 2),
    shadow: new THREE.PlaneGeometry(width + 0.8, length + 0.9).rotateX(-Math.PI / 2).translate(0, 0.012, 0),
  };
}

/** A thin rounded outline on the ground around a car: where its risk is shown. */
function frameGeometry({ length, width }) {
  const rounded = (w, l, r) => {
    const shape = new THREE.Shape();
    shape.moveTo(-w / 2 + r, -l / 2);
    shape.lineTo(w / 2 - r, -l / 2);
    shape.quadraticCurveTo(w / 2, -l / 2, w / 2, -l / 2 + r);
    shape.lineTo(w / 2, l / 2 - r);
    shape.quadraticCurveTo(w / 2, l / 2, w / 2 - r, l / 2);
    shape.lineTo(-w / 2 + r, l / 2);
    shape.quadraticCurveTo(-w / 2, l / 2, -w / 2, l / 2 - r);
    shape.lineTo(-w / 2, -l / 2 + r);
    shape.quadraticCurveTo(-w / 2, -l / 2, -w / 2 + r, -l / 2);
    return shape;
  };
  const outer = rounded(width + 0.7, length + 0.8, 0.5);
  outer.holes.push(rounded(width + 0.5, length + 0.6, 0.4));
  return new THREE.ShapeGeometry(outer, 6).rotateX(-Math.PI / 2);
}

/** A round dot with a soft edge, for predicted positions. */
function dotTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 32;
  const context = canvas.getContext('2d');
  const gradient = context.createRadialGradient(16, 16, 0, 16, 16, 16);
  gradient.addColorStop(0.55, '#fff');
  gradient.addColorStop(1, 'rgba(255,255,255,0)');
  context.fillStyle = gradient;
  context.fillRect(0, 0, 32, 32);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

/** A soft dark patch under each car, drawn once on a small canvas. */
function shadowTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = 64;
  canvas.height = 128;
  const context = canvas.getContext('2d');
  // only the blurred shadow of a shape drawn off the canvas lands on it (Safari's canvas has no filter)
  context.shadowColor = '#000';
  context.shadowBlur = 14;
  context.shadowOffsetX = 200;
  context.beginPath();
  context.roundRect(16 - 200, 16, 32, 96, 10);
  context.fill();
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}
