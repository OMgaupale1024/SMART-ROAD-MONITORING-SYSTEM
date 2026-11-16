/**
 * RoadSense dashboard: the live stream (live.js) drawn by the 3D twin (scene.js) and the panels around it, plus the
 * edge server's own measurements and the explanation of the decision (system.js), asked for apart from the stream.
 *
 * The panels show RoadSense's own values (view.js formats them); nothing here decides anything about safety.
 * Everything that arrives over the network is put on the page as text (textContent), never as markup.
 */
import { History, LineChart, RiskStrip, Ticker } from './charts.js';
import { LiveConnection, liveUrl } from './live.js';
import { Poller, agoText, aiPanel, clockText, figure, msText, throttleText, uptimeText } from './system.js';
import {
  accelText, actionMeaning, actionText, detectEvents, distanceText, egoLaneText, mergeEvents, percentText,
  primaryThreat, relationText, safetyPanel, secondsText, steeringText, view,
} from './view.js';

const WINDOW_S = 60; // simulation time the telemetry charts cover
const LANE_NAMES = { left_lane: 'left lane', right_lane: 'right lane' };

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
const setTone = (element, tone) => {
  if (tone) element.dataset.tone = tone;
  else delete element.dataset.tone;
};

let scene = null;
let sceneError = null;
let model = null; // the view model of the latest snapshot
let previous = null;
let events = [];
let system = null; // the latest /api/system answer
let decision = ''; // what the latest decision is about; a change asks for its explanation at once
let lastArrival = null; // performance.now() of the latest snapshot
let lastArrivalWall = null; // the same moment on the wall clock
let arrivals = []; // of the snapshots of the last 5 s
let intervals = []; // ms between the newest snapshots, as they reached this browser
let lastStatus = null;
let outage = null; // { at, wall, kind } while the stream is not live after having been
const interruptions = { count: 0, last: '' };
const seen = { explanation: undefined, error: undefined }; // what the AI events were last raised for

// ---- the 3D view loads on its own: without WebGL the panels still show the stream

import('./scene.js')
  .then(({ createScene }) => {
    scene = createScene($('scene'), $('labels'));
    if (model) scene.update(model, performance.now());
  })
  .catch((error) => {
    sceneError = error;
    console.error('RoadSense: the 3D view could not start:', error);
    renderStatus();
  });

// ---- charts

const simTime = (t) => `t ${t.toFixed(1)} s`;
const history = new History(WINDOW_S);
const speedChart = new LineChart($('speedChart'), {
  label: 'Speed and target speed over the last 60 seconds of simulation time',
  series: [{ key: 'speed', label: 'Speed', tone: 'info' }, { key: 'target', label: 'Target', tone: 'ink', dashed: true }],
  spanX: WINDOW_S, yFloor: 80, xText: simTime,
  yText: (kmh, axis) => (axis ? String(Math.round(kmh)) : `${kmh.toFixed(1)} km/h`),
});
const ttcChart = new LineChart($('ttcChart'), {
  label: 'Minimum time to collision over the last 60 seconds of simulation time; no line where no conflict is predicted',
  series: [{ key: 'ttc', label: 'Minimum TTC', tone: 'info' }],
  spanX: WINDOW_S, yFloor: 5, xText: simTime,
  yText: (s, axis) => (axis ? `${s} s` : `${s.toFixed(2)} s`),
});
const riskStrip = new RiskStrip($('riskStrip'), {
  label: 'Collision risk level over the last 60 seconds of simulation time', spanX: WINDOW_S, step: 0.2, xText: simTime,
});
const latencyChart = new LineChart($('latencyChart'), {
  label: 'Edge processing time of the latest frames',
  series: [{ key: 'process', label: 'Pipeline', tone: 'info' }, { key: 'handler', label: 'Whole request', tone: 'ink', dashed: true }],
  spanX: 150, yFloor: 1, xText: (sequence) => `Frame ${Math.round(sequence)}`,
  yText: (ms, axis) => (axis ? `${ms}` : msText(ms)),
});
const ticker = new Ticker($('ticker'), { count: 20, label: 'Time between the latest frames reaching this browser' });

// ---- the stream

const connection = new LiveConnection(liveUrl(window.location), {
  onSnapshot(snapshot, at) {
    const next = view(snapshot);
    if (connection.received >= 2 && lastArrival !== null) { // the first one after connecting is the stored state
      intervals = [...intervals, at - lastArrival].slice(-60);
    }
    lastArrival = at;
    lastArrivalWall = new Date();
    arrivals = [...arrivals, at].filter((t) => at - t <= 5000);
    if (model && next.runId !== model.runId) history.clear();
    if (next.t !== null) {
      history.push({
        x: next.t, speed: next.ego.speedKmh, target: next.safety?.targetKmh ?? null,
        ttc: next.safety?.minTtc ?? null, risk: next.safety?.risk ?? null,
      });
    }
    addEvents(detectEvents(previous, next));
    model = next;
    previous = next;
    scene?.update(model, at);
    const { safety } = model;
    const about = JSON.stringify([model.runId, safety?.risk, safety?.action, safety?.threat?.id, safety?.lane]);
    if (about !== decision) {
      if (decision) flash();
      decision = about;
      aiPoller.kick(); // the explanation of a new decision is asked for now, not at the next turn
    }
    renderPanels();
    renderAi();
  },
  onChange: () => renderStatus(),
});

// ---- the diagnostics, asked for apart from the stream

const systemPoller = new Poller('/api/system', {
  onData(data) {
    system = data;
  },
  onChange: () => renderSystem(),
});
const aiPoller = new Poller('/api/ai-explanation', { onChange: () => renderAi() });

connection.start();
systemPoller.start();
aiPoller.start();
window.addEventListener('pagehide', () => {
  connection.close();
  systemPoller.stop();
  aiPoller.stop();
});
window.addEventListener('pageshow', (event) => {
  if (!event.persisted) return; // back from the back-forward cache
  connection.start();
  systemPoller.start();
  aiPoller.start();
});
setInterval(() => { // STALE comes from time passing, not from a message
  renderStatus();
  renderAi();
}, 250);

function frame(now) {
  scene?.render(now);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---- events

function addEvents(found) {
  if (!found.length) return;
  events = mergeEvents(events, found.map((event) => ({ ...event, wall: new Date() })), performance.now());
  renderEvents();
}

function flash() {
  const panel = $('next');
  delete panel.dataset.flash;
  void panel.offsetWidth; // restart the animation
  panel.dataset.flash = '1';
}

// ---- status: the state word, the notice over the twin, the status rail

function streamRate(now) {
  const recent = arrivals.filter((t) => now - t <= 5000);
  if (!recent.length) return 0;
  return recent.length / Math.max(0.2, Math.min(5, (now - recent[0]) / 1000 + 0.2));
}

/** How old the state on screen is, in seconds: from its arrival here, or, for the stored state the server sent on
 * connecting, from the server's own measurement. */
function dataAge(now) {
  if (connection.received >= 2 && lastArrival !== null) return (now - lastArrival) / 1000;
  const age = system?.edge?.data_age_s;
  if (age === null || age === undefined || systemPoller.lastOkAt === null) return null;
  return age + (now - systemPoller.lastOkAt) / 1000;
}

function renderStatus() {
  const now = performance.now();
  const status = connection.status(now);
  if (status !== lastStatus) statusChanged(status, now);
  setText($('statusText'), status);
  document.body.dataset.status = status; // anything but LIVE dims the drawn state
  const shown = model && model.t !== null ? `t ${model.t.toFixed(1)} s` : null;
  const age = dataAge(now);

  let notice = null;
  if (status === 'CONNECTING') {
    notice = ['Connecting to RoadSense', `Opening the live stream from ${window.location.host}.`];
  } else if (status === 'DISCONNECTED') {
    notice = ['Edge server not reachable', 'Retrying every second. On the edge device, start it with:',
      `python3 -m roadsense.web --host 0.0.0.0 --port ${window.location.port || 80} --no-browser`];
  } else if (status === 'SIMULATION ENDED') {
    notice = ['Simulation ended', `Showing its last state${shown ? `, at ${shown}` : ''}. Start the Webots world again to continue.`];
  } else if (status === 'NO PRODUCER') {
    notice = ['Waiting for the simulation', 'No sensor frames have reached this server yet. Start the Webots world with:',
      `ROADSENSE_EDGE_URL=${window.location.origin}/api/sensor-frame`];
  } else if (status === 'STALE') {
    notice = [`No sensor data${age === null ? '' : ` for ${agoText(age).replace(' ago', '')}`}`,
      `Showing the last state received${shown ? `, at ${shown}` : ''}. Is the simulation paused?`];
  } else if (sceneError) {
    notice = ['3D view unavailable', `This browser could not start WebGL (${sceneError.message}). The panels still show the live state.`];
  }
  $('notice').hidden = !notice;
  if (notice) {
    setText($('noticeTitle'), notice[0]);
    setText($('noticeText'), notice[1]);
    setText($('noticeCode'), notice[2] ?? '');
    $('noticeCode').hidden = !notice[2];
  }
  renderHud(status);

  // the status rail
  const live = status === 'LIVE';
  setText($('railSim'), { LIVE: 'Streaming', STALE: 'Silent', 'SIMULATION ENDED': 'Ended', 'NO PRODUCER': 'No producer',
    CONNECTING: 'Connecting', DISCONNECTED: 'Unknown' }[status]);
  setText($('railSimNote'), model ? `Webots, ${model.active ? 'run in progress' : 'run finished'}` : 'Nothing received yet');
  setTone($('railSim').parentElement, live ? '' : status === 'STALE' ? 'warn' : 'off');

  const rate = streamRate(now);
  setText($('railStream'), live ? `${rate.toFixed(1)} per s` : status === 'STALE' ? 'Stalled' : 'No frames');
  setText($('railStreamNote'), intervals.length
    ? `Gap ${Math.round(intervals.at(-1))} ms, longest ${Math.round(Math.max(...intervals))} ms`
    : 'No frames yet');
  setTone($('railStream').closest('.cell'), live ? '' : status === 'STALE' ? 'warn' : 'off');
  ticker.update(intervals);
  setText($('tRate'), live ? rate.toFixed(1) : '0.0');
  setText($('tRateNote'), 'at this browser');

  setText($('railRun'), model?.runId ?? '—');
  setText($('railRunNote'), model ? `Sequence ${model.sequence ?? '—'}` : '');
  setText($('railLast'), age === null ? '—' : clockText(new Date(Date.now() - age * 1000)));
  setText($('railLastNote'), age === null ? 'No data yet' : agoText(age));
  setTone($('railLast').parentElement, live || age === null ? '' : 'warn');

  const failures = systemPoller.failures + aiPoller.failures;
  setText($('railErrors'), interruptions.count ? String(interruptions.count) : outage ? 'Now' : 'None');
  setText($('railErrorsNote'), [outage ? `${outage.kind} since ${clockText(outage.wall)}` : interruptions.last,
    failures ? `${failures} failed status requests` : ''].filter(Boolean).join('; ') || 'Since this page opened');
  setTone($('railErrors').parentElement, outage ? 'bad' : '');
}

function statusChanged(status, now) {
  const before = lastStatus;
  lastStatus = status;
  const lost = status === 'STALE' || status === 'DISCONNECTED';
  if (lost && (before === 'LIVE' || (outage && outage.kind !== status))) {
    if (!outage) interruptions.count += 1;
    outage = { at: outage?.at ?? now, wall: outage?.wall ?? new Date(), kind: status };
    addEvents([status === 'STALE'
      ? { key: 'stream', text: 'Sensor stream stalled', detail: 'No frames from the simulation', tone: 'CAUTION', label: 'STREAM' }
      : { key: 'stream', text: 'Edge server connection lost', detail: 'Retrying every second', tone: 'CRITICAL', label: 'LINK' }]);
  } else if (status === 'LIVE' && outage) {
    const seconds = (now - outage.at) / 1000;
    interruptions.last = `Last: ${clockText(outage.wall)}, ${seconds.toFixed(1)} s`;
    addEvents([{ key: 'stream', text: `Live again after ${seconds.toFixed(1)} s`, tone: 'info', label: 'STREAM' }]);
    outage = null;
  } else if (!lost && status !== 'CONNECTING' && outage) {
    // connected again, to a server that has no live run yet (it restarted, or the run ended meanwhile)
    const seconds = (now - outage.at) / 1000;
    interruptions.last = `Last: ${clockText(outage.wall)}, ${seconds.toFixed(1)} s`;
    addEvents([{ key: 'stream', text: `Edge server reachable again after ${seconds.toFixed(1)} s`, tone: 'info', label: 'LINK' }]);
    outage = null;
  }
}

/** The recommendation over the twin. At HIGH and CRITICAL risk, while the state is live, it is the alert: the
 * safety processor's own reason, word for word (never the model's). */
function renderHud(status) {
  const safety = model?.safety;
  $('hud').hidden = !safety;
  if (!safety) return;
  const urgent = status === 'LIVE' && (safety.risk === 'HIGH' || safety.risk === 'CRITICAL');
  $('hud').dataset.risk = safety.risk ?? '';
  if (urgent) $('hud').dataset.urgent = '1';
  else delete $('hud').dataset.urgent;
  $('hudRisk').hidden = !urgent;
  setText($('hudRisk'), urgent ? `${safety.risk} RISK` : '');
  setText($('hudAction'), actionText(safety.action));
  const target = safety.targetKmh === null ? '' : `Target ${Math.round(safety.targetKmh)} km/h`;
  const lane = safety.lane ? `suggested: ${LANE_NAMES[safety.lane] ?? safety.lane}` : '';
  setText($('hudDetail'), urgent ? [safety.reason, target].filter(Boolean).join('. ') : [target, lane].filter(Boolean).join(', '));
}

// ---- the panels fed by the stream

function renderPanels() {
  setText($('simTime'), model.t === null ? '—' : model.t.toFixed(1));
  const safety = model.safety;
  const text = safetyPanel(model);
  const threat = primaryThreat(model);

  $('next').dataset.risk = safety?.risk ?? '';
  setText($('nextRisk'), text.risk);
  setText($('nextAction'), text.action);
  setText($('nextMeaning'), actionMeaning(safety?.action));
  setText($('nextSpeed'), model.ego.speedKmh === null ? '—' : String(Math.round(model.ego.speedKmh)));
  setText($('nextTarget'), safety?.targetKmh === null || !safety ? '—' : String(Math.round(safety.targetKmh)));
  setText($('nextLane'), text.lane);
  setText($('nextTtc'), !safety ? '—' : safety.minTtc === null ? 'No conflict' : secondsText(safety.minTtc));
  setText($('nextThreat'), threat ? threat.id : 'None');
  setText($('nextThreatFacts'), threat ? threat.facts.join(', ') : '');
  setText($('nextReason'), text.reason || '—');
  setText($('nextProcessor'), model.processing === 'edge' ? 'Decided by the edge server from the raw sensor frames.'
    : model.processing === 'webots' ? 'Decided in Webots by the simulation\'s controller.' : '');

  // the speed tape: the car's speed, with what is above the target in the risk colour
  const speed = model.ego.speedKmh;
  const target = safety?.targetKmh ?? null;
  const full = Math.max(100, 20 * Math.ceil(Math.max(speed ?? 0, target ?? 0) / 20));
  $('tapeFill').style.setProperty('--w', `${(100 * (speed ?? 0)) / full}%`);
  $('tapeFill').style.setProperty('--t', speed && target !== null && target < speed ? `${(100 * target) / speed}%` : '100%');
  $('tapeTarget').hidden = target === null;
  if (target !== null) $('tapeTarget').style.setProperty('--x', `${(100 * target) / full}%`);

  renderHud(connection.status());

  // telemetry
  setText($('tSpeed'), speed === null ? '—' : speed.toFixed(1));
  setText($('tTtc'), !safety ? '—' : safety.minTtc === null ? 'No conflict' : secondsText(safety.minTtc));
  $('tRisk').parentElement.dataset.risk = safety?.risk ?? '';
  setText($('tRisk'), text.risk);
  setText($('tAccel'), accelText(model.ego.accel));
  const lane = egoLaneText(model.ego);
  setText($('tLane'), lane.name);
  setText($('tLaneNote'), lane.note);
  const steering = steeringText(model.ego.steeringRad);
  setText($('tSteer'), steering.degrees);
  setText($('tSteerNote'), steering.side);
  setText($('tTracks'), String(model.tracks.length));
  const conflicts = model.tracks.filter((track) => track.conflict).length;
  setText($('tTracksNote'), conflicts ? `${conflicts} in predicted conflict` : 'none in conflict');
  setText($('tThreat'), threat ? threat.id : 'None');
  setText($('tThreatNote'), threat ? threat.facts.at(-1) ?? '' : '');

  speedChart.update(history.rows);
  ttcChart.update(history.rows);
  riskStrip.update(history.rows);
  renderHazards();
}

let hazardKey = '';
function renderHazards() {
  // ahead nearest first, then those passed, most recent first: the map keeps every hazard found, each once
  const ahead = model.hazards.filter((h) => h.direction !== 'behind').sort((a, b) => a.distance - b.distance);
  const passed = model.hazards.filter((h) => h.direction === 'behind').sort((a, b) => a.distance - b.distance);
  setText($('hazardCount'), String(model.hazards.length));
  $('hazardEmpty').hidden = model.hazards.length > 0;
  const rows = [...ahead, ...passed].map((h) => [h.id, h.severity ?? '—', h.direction === 'behind',
    `${distanceText(h.distance)} ${h.direction === 'behind' ? 'behind' : 'ahead'}`, relationText(h.laneRelation),
    [`${h.type ?? 'hazard'}, confidence ${percentText(h.confidence)}`,
      h.observations === null ? '' : `${h.observations} observation${h.observations === 1 ? '' : 's'}`,
      h.firstSeen === null ? '' : `first seen t ${h.firstSeen.toFixed(1)} s, last t ${(h.lastSeen ?? h.firstSeen).toFixed(1)} s`,
      h.source ? `source ${h.source}` : ''].filter(Boolean).join(', ')]);
  const key = JSON.stringify(rows);
  if (key === hazardKey) return;
  hazardKey = key;
  const items = [];
  rows.forEach(([id, severity, behind, where, lane, more], i) => {
    if (i === 0 && !behind) items.push(el('li', `Ahead (${ahead.length})`, 'group'));
    if (i === ahead.length) items.push(el('li', `Passed (${passed.length})`, 'group'));
    const row = el('li', '', behind ? 'hazard passed' : 'hazard');
    row.append(el('span', id, 'hazard-id'), el('span', severity, `chip tone-${severity}`), el('span', where, 'hazard-where'),
      el('span', lane, 'hazard-lane'), el('span', more, 'hazard-more'));
    items.push(row);
  });
  $('hazardList').replaceChildren(...items);
}

let eventKey = '';
function renderEvents() {
  $('eventEmpty').hidden = events.length > 0;
  const key = JSON.stringify(events.map((event) => [event.wall, event.text, event.detail, event.count]));
  if (key === eventKey) return;
  eventKey = key;
  $('eventList').replaceChildren(...events.map((event) => {
    const row = el('li', '', 'event');
    const chip = event.label ?? (event.tone === 'info' ? '' : event.tone ?? '');
    row.append(el('span', clockText(event.wall), 'event-when'), el('span', event.text, 'event-what'),
      el('span', chip, `event-tone chip tone-${event.tone}`));
    const detail = [event.detail, event.t === undefined || event.t === null ? '' : `t ${event.t.toFixed(1)} s`,
      event.count > 1 ? `${event.count} changes in a row` : ''].filter(Boolean).join(', ');
    if (detail) row.append(el('span', detail, 'event-detail'));
    return row;
  }));
}

// ---- the edge server's own measurements

/** A measured value, or "n/a" with the server's reason for it as the tooltip. */
function measured(element, text, reason) {
  setText(element, text);
  if (text === 'n/a') {
    element.dataset.na = '1';
    element.title = reason ?? 'not measured on this host';
  } else {
    delete element.dataset.na;
    element.removeAttribute('title');
  }
}

const AI_AVAILABILITY = {
  available: 'Ollama available', unavailable: 'Ollama not reachable', model_missing: 'Model not installed',
  unknown: 'Checking Ollama',
};

function renderSystem() {
  const stale = systemPoller.stale();
  const edgePanel = $('edgeTitle').parentElement;
  if (stale) edgePanel.dataset.stale = '1';
  else delete edgePanel.dataset.stale;
  const answering = system && !stale;
  setText($('railServer'), answering ? `Up ${uptimeText(system.app.uptime_s)}` : system || systemPoller.error ? 'Not answering' : '—');
  setText($('railServerNote'), system ? `RoadSense ${system.app.version}` : systemPoller.error ?? 'Asking the server');
  setTone($('railServer').parentElement, answering ? '' : 'bad');
  if (!system) return;
  const { edge, host, ai, safety } = system;
  const latency = edge.latency_ms;
  const machine = host.model ?? `${host.system} ${host.machine}`;

  // the TTC chart's reference levels are the safety processor's own, as the server reports them
  if (safety && !ttcChart.thresholds.length) {
    ttcChart.yMax = safety.horizon_s;
    ttcChart.thresholds = [
      { value: safety.high_ttc_s, label: `High below ${safety.high_ttc_s} s`, tone: 'HIGH' },
      { value: safety.critical_ttc_s, label: `Critical below ${safety.critical_ttc_s} s`, tone: 'CRITICAL' },
    ];
    ttcChart.draw();
  }

  // the status rail: where the decisions are computed, and the model
  const hostName = host.hostname.replace(/\.local$/, '');
  const mode = { edge: `Edge, ${hostName}`, precomputed: 'In Webots' }[edge.mode] ?? `Idle, ${hostName}`;
  const modeNote = {
    edge: latency ? `95% under ${msText(latency.process.p95)}` : 'No frame processed yet',
    precomputed: 'Computed by the simulation\'s controller',
  }[edge.mode] ?? 'No frames yet';
  setText($('railEdge'), mode);
  setText($('railEdgeNote'), modeNote);
  $('railEdge').title = `${host.hostname}: ${machine}`;
  setText($('railAi'), ai.enabled ? ai.model : 'Off');
  setText($('railAiNote'), ai.enabled ? AI_AVAILABILITY[ai.availability] ?? ai.availability : 'Explanations by fixed rules');
  const aiFine = ai.availability === 'available' || ai.availability === 'unknown';
  setTone($('railAi').parentElement, !ai.enabled ? 'off' : aiFine ? '' : 'warn');

  renderEdge(system, machine);
}

/** The edge panel: every figure is the server's own measurement; what it could not measure reads n/a. */
function renderEdge({ app, edge, host, ai }, machine) {
  const na = host.unavailable ?? {};
  const latency = edge.latency_ms;
  const none = 'No sensor frame processed in this run yet';
  setText($('edgeHost'), [host.hostname, machine, `${host.cpu_count} cores`, `Python ${host.python}`,
    `up ${uptimeText(app.uptime_s)}`].join(', '));
  setText($('eRate'), edge.rate_hz === null ? '—' : edge.rate_hz.toFixed(1));
  setText($('eAge'), edge.data_age_s === null ? '—' : agoText(edge.data_age_s).replace(' ago', ''));

  measured($('eProcess'), latency ? msText(latency.process.p50) : 'n/a',
    edge.mode === 'precomputed' ? 'These states were computed by the simulation, not here' : none);
  setText($('eProcessNote'), latency
    ? `median; 95% under ${msText(latency.process.p95)}, max ${msText(latency.process.max)}`
    : 'Pipeline.process() per frame');
  measured($('eHandler'), latency ? msText(latency.handler.p50) : 'n/a', none);
  setText($('eStages'), latency
    ? `95% under ${msText(latency.handler.p95)}; read ${msText(latency.read.p50)}, `
      + `validate ${msText(latency.validate.p50)}, publish ${msText(latency.publish.p50)}`
    : 'read, validate, process, publish');

  setText($('eFrames'), String(edge.frames.accepted));
  const refused = Object.entries(edge.frames.rejected_by_status).map(([status, count]) => `${count} with ${status}`);
  setText($('eFramesNote'), edge.frames.rejected ? `${edge.frames.rejected} refused (${refused.join(', ')})` : 'none refused');
  setText($('eMissed'), String(edge.run?.missed_frames ?? 0));

  measured($('eCpu'), figure(host.cpu_percent, 0, '%'), na.cpu_percent);
  $('eCpuBar').style.setProperty('--w', `${host.cpu_percent ?? 0}%`);
  const memory = host.memory;
  measured($('eMem'), figure(memory?.used_percent, 0, '%'), na.memory);
  $('eMemBar').style.setProperty('--w', `${memory?.used_percent ?? 0}%`);
  setText($('eMemNote'), memory ? `${Math.round(memory.total_mb - memory.available_mb)} of ${Math.round(memory.total_mb)} MB` : '');
  measured($('eTemp'), figure(host.temperature_c, 1, ' °C'), na.temperature_c);
  measured($('eThrottle'), throttleText(host.throttling), na.throttling);

  const { rss_mb: rss, peak_rss_mb: peak } = host.process;
  measured($('eRss'), figure(rss ?? peak, 0, ' MB'), na['process.rss_mb']);
  const dashboards = edge.clients.websocket;
  setText($('eClients'), `${rss === null ? 'peak; ' : ''}${dashboards} dashboard${dashboards === 1 ? '' : 's'} connected`);
  measured($('eLlm'), ai.latency_ms ? msText(ai.latency_ms.last) : 'n/a',
    ai.enabled ? 'The model has not answered yet' : 'The AI model is off');
  setText($('eLlmNote'), ai.latency_ms
    ? `last; median ${msText(ai.latency_ms.p50)}, 95% under ${msText(ai.latency_ms.p95)}`
    : 'Ollama, measured apart from the pipeline');
  setText($('edgeNote'), `Measured on ${host.hostname}, the edge server, not in this browser. `
    + 'Values marked n/a cannot be read on this host; hover for why.');

  const series = edge.series;
  latencyChart.update(!series ? [] : series.sequence.map((x, i) => (
    { x, process: series.process_ms[i], handler: series.handler_ms[i] })));
}

// ---- the explanation

function renderAi() {
  const ai = aiPoller.data;
  const panel = aiPanel(ai, { model, stream: connection.status(), pollStale: aiPoller.stale(), pollError: aiPoller.error });
  $('ai').dataset.badge = panel.badge;
  setText($('aiBadge'), panel.badge);
  const writer = panel.origin === 'AI model' ? `AI model ${panel.model}` : 'fixed rules';
  setText($('aiOrigin'), panel.origin ? `Written by: ${writer}` : '');
  $('aiOrigin').dataset.origin = panel.origin;
  setText($('aiText'), panel.text || 'No explanation yet.');
  setText($('aiNote'), panel.note);
  const failedAt = clockText(ai?.last_error?.at_unix_s ?? null);
  setText($('aiError'), panel.error ? `Last model error, ${failedAt}: ${panel.error}` : '');
  $('aiError').hidden = !panel.error;
  setText($('aiModel'), panel.availability ? `${panel.model}, ${panel.availability}` : panel.model);
  setText($('aiStatus'), panel.status);
  setText($('aiLatency'), panel.latency);
  $('aiLatency').title = panel.latencyDetail;
  setText($('aiSource'), panel.source);
  setText($('aiExplains'), panel.explains);
  setText($('aiGenerated'), panel.generatedAt === null ? '—'
    : `${clockText(panel.generatedAt)}${panel.generatedAgoS === null ? '' : `, ${agoText(panel.generatedAgoS)}`}`);
  $('aiPast').hidden = !panel.past;
  setText($('aiPastText'), panel.past?.text ?? '');
  setText($('aiPastNote'), panel.past?.note ?? '');

  if (!ai) return;
  // events: a new answer from the model, a new model failure (nothing is raised for what was there on opening)
  const latest = ai.latest_llm;
  const written = latest?.generated_at_unix_s ?? null;
  const failed = ai.last_error?.at_unix_s ?? null;
  if (seen.explanation !== undefined && written !== null && written !== seen.explanation) {
    const about = [latest.source?.risk, latest.source?.action?.replaceAll('_', ' '), latest.source?.threat_id].filter(Boolean).join(', ');
    addEvents([{ key: 'ai', text: `Explanation written by the AI model in ${msText(latest.latency_ms)}`,
      detail: `About ${about}, sequence ${latest.source?.sequence ?? '—'}`, tone: 'info', label: 'AI' }]);
  }
  if (seen.error !== undefined && failed !== null && failed !== seen.error) {
    addEvents([{ key: 'ai-error', text: `AI explanation failed: ${ai.last_error.kind.replaceAll('_', ' ')}`,
      detail: ai.last_error.message, tone: 'CAUTION', label: 'AI' }]);
  }
  if (written !== null || seen.explanation === undefined) seen.explanation = written;
  seen.error = failed;
}

renderStatus();
renderSystem();
renderAi();
