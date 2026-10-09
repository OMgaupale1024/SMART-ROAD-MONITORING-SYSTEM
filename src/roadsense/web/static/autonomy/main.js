/**
 * RoadSense Autonomy page: the live stream (live.js) drawn by the 3D scene (scene.js) and the panels around it.
 * The panels show RoadSense's own values (view.js formats them); nothing here decides anything about safety.
 */
import { LiveConnection, liveUrl } from './live.js';
import { detectEvents, distanceText, primaryThreat, safetyPanel, secondsText, view } from './view.js';

const MAX_EVENTS = 12;
const LANE_NAMES = ['right', 'middle', 'left']; // ego.lane, the driving controller's target lane

const $ = (id) => document.getElementById(id);
const setText = (element, text) => {
  if (element.textContent !== text) element.textContent = text;
};
const el = (tag, text, className) => {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
};

if (new URLSearchParams(window.location.search).has('presentation')) document.body.classList.add('presentation');

let scene = null;
let sceneError = null;
let model = null;
let previous = null;
let events = [];

// The 3D view loads on its own: without WebGL the panels still show the stream.
import('./scene.js')
  .then(({ createScene }) => {
    scene = createScene($('scene'), $('labels'));
    if (model) scene.update(model, performance.now());
  })
  .catch((error) => {
    sceneError = error;
    console.error('RoadSense Autonomy: the 3D view could not start:', error);
    renderStatus();
  });

const connection = new LiveConnection(liveUrl(window.location), {
  onSnapshot(snapshot, at) {
    model = view(snapshot);
    events = [...detectEvents(previous, model).reverse(), ...events].slice(0, MAX_EVENTS);
    previous = model;
    scene?.update(model, at);
    renderPanels();
  },
  onChange: () => renderStatus(),
});
connection.start();
window.addEventListener('pagehide', () => connection.close());
window.addEventListener('pageshow', (event) => {
  if (event.persisted) connection.start(); // back from the back-forward cache
});
setInterval(renderStatus, 250); // STALE comes from time passing, not from a message

function frame(now) {
  scene?.render(now);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

function renderStatus() {
  const status = connection.status();
  setText($('statusText'), status);
  document.body.dataset.status = status; // anything but LIVE dims the drawn state
  const shown = model && model.t !== null ? `${model.t.toFixed(1)} s` : null;
  let notice = null;
  if (status === 'CONNECTING') {
    notice = ['Connecting to RoadSense', `Opening the live stream from ${window.location.host}.`];
  } else if (status === 'DISCONNECTED') {
    notice = ['RoadSense server not reachable', 'Retrying every second. Start it with:', 'venv/bin/python -m roadsense.web --no-browser'];
  } else if (status === 'NO PRODUCER' && model && !model.active && connection.received) {
    notice = ['Simulation ended', `Showing its last state, at t ${shown}. Start the Webots world again to continue.`];
  } else if (status === 'NO PRODUCER') {
    notice = ['Waiting for the simulation', `Start the Webots world with ROADSENSE_LIVE_PUBLISH=1.${shown ? ` Showing the last state received, at t ${shown}.` : ''}`];
  } else if (status === 'STALE') {
    const age = Math.floor((performance.now() - connection.lastAt) / 1000);
    notice = [`No update for ${age} s`, `Showing the last state, at t ${shown}. Is the simulation paused?`];
  }
  if (sceneError) notice = ['3D view unavailable', `This browser could not start WebGL (${sceneError.message}). The panels still show the live state.`];
  $('notice').hidden = !notice;
  if (notice) {
    setText($('noticeTitle'), notice[0]);
    setText($('noticeText'), notice[1]);
    setText($('noticeCode'), notice[2] ?? '');
    $('noticeCode').hidden = !notice[2];
  }
}

function renderPanels() {
  setText($('simTime'), model.t === null ? '—' : model.t.toFixed(1));

  const safety = safetyPanel(model);
  $('safety').dataset.risk = model.safety?.risk ?? '';
  setText($('safetyRisk'), safety.risk);
  setText($('safetyAction'), safety.action);
  setText($('safetyTarget'), safety.targetKmh);
  setText($('safetyLane'), safety.lane);
  setText($('safetyReason'), safety.reason);

  const threat = primaryThreat(model);
  $('threat').dataset.risk = threat?.risk ?? '';
  setText($('threatId'), threat ? threat.id : 'None');
  $('threatRisk').hidden = !threat?.risk;
  setText($('threatRisk'), threat?.risk ?? '');
  $('threatRisk').className = `chip tone-${threat?.risk}`;
  setText($('threatFacts'), threat ? threat.facts.join('\n') : '');

  renderHazards();
  renderEvents();

  setText($('mSpeed'), model.ego.speedKmh === null ? '—' : String(Math.round(model.ego.speedKmh)));
  setText($('mTarget'), safety.targetKmh);
  setText($('mTtc'), secondsText(model.safety?.minTtc ?? null));
  setText($('mTracks'), String(model.tracks.length));
  setText($('mHazards'), String(model.hazards.length));
  setText($('mLane'), model.ego.lane === null ? '—' : String(model.ego.lane));
  const lane = LANE_NAMES[model.ego.lane] ?? '';
  setText($('mLaneNote'), model.ego.maneuver === 'lane_change' ? `${lane}, changing lane` : lane);
}

let hazardKey = '';
function renderHazards() {
  // ahead nearest first, then those passed, most recent first: the map keeps every hazard found
  const ahead = model.hazards.filter((h) => h.direction !== 'behind').sort((a, b) => a.distance - b.distance);
  const behind = model.hazards.filter((h) => h.direction === 'behind').sort((a, b) => a.distance - b.distance);
  const rows = [...ahead, ...behind].map((h) => [h.id, h.severity ?? '—', `${distanceText(h.distance)} ${h.direction ?? ''}`, h.direction === 'behind']);
  setText($('hazardCount'), String(rows.length));
  $('hazardEmpty').hidden = rows.length > 0;
  const key = JSON.stringify(rows);
  if (key === hazardKey) return;
  hazardKey = key;
  $('hazardList').replaceChildren(...rows.map(([id, severity, where, passed]) => {
    const row = el('li', '', passed ? 'passed' : '');
    row.append(el('span', id), el('span', severity, `chip tone-${severity}`), el('span', where, 'where'));
    return row;
  }));
}

let eventKey = '';
function renderEvents() {
  $('eventEmpty').hidden = events.length > 0;
  const key = JSON.stringify(events);
  if (key === eventKey) return;
  eventKey = key;
  $('eventList').replaceChildren(...events.map((event) => {
    const row = el('li', '', `tone-${event.tone}`);
    row.append(el('span', event.t === null ? '' : event.t.toFixed(1), 'when'), el('span', event.text, 'what'),
      el('span', event.tone === 'info' ? '' : event.tone ?? '', 'tone'));
    if (event.detail) row.append(el('span', event.detail, 'detail'));
    return row;
  }));
}

renderStatus();
