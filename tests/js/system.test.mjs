// The dashboard's diagnostics polling and the AI panel's rules (src/roadsense/web/static/js/system.js), without a
// browser or a server.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  POLL_MS, POLL_STALE_MS, Poller, agoText, aiPanel, clockText, explainsDecision, figure, msText, throttleText,
  uptimeText,
} from '../../src/roadsense/web/static/js/system.js';
import { view } from '../../src/roadsense/web/static/js/view.js';

const RAW = Object.fromEntries(readFileSync(new URL('./recorded_snapshots.jsonl', import.meta.url), 'utf8')
  .trim().split('\n').map((line) => JSON.parse(line)).map((s) => [s.simulation.sequence, s]));
const MODEL = view(structuredClone(RAW[102])); // HIGH, BRAKE, PH_001, run ffc51d2d138d

/** An /api/ai-explanation answer about the decision in MODEL. */
function answer(changes = {}, explanation = {}) {
  return {
    schema: 'roadsense.ai.v1', enabled: true, model: 'qwen3:0.6b', availability: 'available', status: 'ready',
    is_current: true, stale_reason: null, last_error: null, server_time_unix_s: 1794845610.0,
    stats: { latency_ms: { last: 4071.2, mean: 4000, p50: 3900.4, p95: 4710.9, max: 4710.9 } },
    explanation: {
      text: 'RoadSense recommends braking because pothole PH_001 is ahead in your lane.', origin: 'llm',
      fallback_reason: null, cached: false, latency_ms: 4071.2, generated_at_unix_s: 1794845606.5,
      source: {
        run_id: 'ffc51d2d138d', sequence: 102, sim_time_s: 20.4, risk: 'HIGH', action: 'BRAKE', threat_id: 'PH_001',
        threat_type: 'road_hazard', recommended_lane: null,
      },
      ...explanation,
    },
    ...changes,
  };
}
const live = { model: MODEL, stream: 'LIVE', pollStale: false, pollError: null };

test('an explanation is CURRENT only while it is about the live decision, from a live stream and a server that answers', () => {
  const panel = aiPanel(answer(), live);
  assert.deepEqual([panel.badge, panel.current], ['CURRENT', true]);
  assert.equal(panel.text, 'RoadSense recommends braking because pothole PH_001 is ahead in your lane.');
  assert.deepEqual([panel.origin, panel.model, panel.availability, panel.status], ['AI model', 'qwen3:0.6b', 'available', 'Ready']);
  assert.deepEqual([panel.latency, panel.source, panel.explains], ['4.07 s', 'Sequence 102, t 20.4 s', 'HIGH, BRAKE, PH_001']);
  assert.equal(panel.latencyDetail, 'median 3.90 s, 95th percentile 4.71 s');
  assert.equal(panel.generatedAgoS, 3.5); // on the server's clock, so the two computers' clocks need not agree
  assert.equal(panel.note, '');
});

test('an explanation of another decision is UPDATING, never current', () => {
  const moved = structuredClone(MODEL);
  moved.safety.action = 'EMERGENCY_BRAKE';
  moved.safety.risk = 'CRITICAL';
  for (const model of [moved, { ...MODEL, runId: 'another-run' }, { ...MODEL, safety: { ...MODEL.safety, threat: { id: 'PH_002' } } },
    { ...MODEL, safety: { ...MODEL.safety, lane: 'left_lane' } }]) {
    const panel = aiPanel(answer(), { ...live, model });
    assert.deepEqual([panel.badge, panel.current], ['UPDATING', false]);
    assert.match(panel.note, /decision just changed/);
  }
  assert.equal(explainsDecision(answer().explanation, MODEL), true);
  assert.equal(explainsDecision(answer().explanation, null), false);
  assert.equal(explainsDecision(null, MODEL), false);
});

test('changed threat facts cannot retain a CURRENT explanation just because id and action match', () => {
  const cause = { kind: 'road_hazard', id: 'PH_001', hazard: 'pothole', severity: 'HIGH', lane: 'ego_lane' };
  const ai = answer();
  ai.explanation.source.cause = cause;
  assert.equal(aiPanel(ai, live).badge, 'CURRENT');
  for (const changes of [{ severity: 'LOW' }, { laneRelation: 'left_lane' }, { type: 'other_hazard' }]) {
    const model = structuredClone(MODEL);
    Object.assign(model.safety.roadHazards.find(h => h.id === cause.id), changes);
    assert.equal(aiPanel(ai, { ...live, model }).badge, 'UPDATING');
  }
  const model = structuredClone(MODEL);
  model.safety.threat = { id: 'TRACK_001', type: 'vehicle' };
  model.tracks = [{ id: 'TRACK_001', laneRelation: 'ego_lane', lon: 10 }];
  Object.assign(ai.explanation.source, { threat_id: 'TRACK_001', threat_type: 'vehicle',
    cause: { kind: 'vehicle', id: 'TRACK_001', lane: 'ego_lane', position: 'ahead' } });
  assert.equal(aiPanel(ai, { ...live, model }).badge, 'CURRENT');
  model.tracks[0].lon = -1;
  assert.equal(aiPanel(ai, { ...live, model }).badge, 'UPDATING');
});

test('an explanation is STALE when the stream is not live, the server says so, or the endpoint stopped answering', () => {
  for (const [stream, why] of [['STALE', /No sensor data/], ['SIMULATION ENDED', /simulation has ended/],
    ['DISCONNECTED', /disconnected/], ['CONNECTING', /Connecting/], ['NO PRODUCER', /No simulation/]]) {
    const panel = aiPanel(answer(), { ...live, stream });
    assert.deepEqual([panel.badge, panel.current], ['STALE', false], stream);
    assert.match(panel.note, why);
    assert.ok(panel.text); // still shown, marked: never as current
  }
  const paused = aiPanel(answer({ is_current: false, stale_reason: 'no_live_data' }), live);
  assert.deepEqual([paused.badge, paused.current], ['STALE', false]);
  const ended = aiPanel(answer({ is_current: false, stale_reason: 'simulation_ended' }), live);
  assert.match(ended.note, /simulation has ended/);
  const silent = aiPanel(answer(), { ...live, pollStale: true, pollError: 'no answer in time' });
  assert.deepEqual([silent.badge, silent.current], ['STALE', false]);
  assert.match(silent.note, /not answering \(no answer in time\)/);
});

test('deterministic text says why the model did not write it', () => {
  const rules = (fallback_reason, extra = {}) => aiPanel(
    answer(extra, { origin: 'deterministic', fallback_reason, latency_ms: null }), live);
  assert.match(rules('urgent', { status: 'deterministic' }).note, /Urgent alerts are never worded by the AI model/);
  assert.match(rules('disabled', { enabled: false, status: 'disabled', availability: 'disabled' }).note, /AI model is off/);
  assert.match(rules('awaiting_model', { status: 'generating' }).note, /writing its explanation/);
  assert.match(rules('timeout', { status: 'fallback' }).note, /did not answer in time/);
  assert.match(rules('unavailable', { status: 'fallback' }).note, /Ollama is not reachable/);
  assert.match(rules('invalid_output', { status: 'fallback' }).note, /refused/);
  const urgent = rules('urgent', { status: 'deterministic' });
  assert.deepEqual([urgent.badge, urgent.origin, urgent.latency, urgent.status], ['CURRENT', 'Fixed rules', '—', 'Fixed rules']);
  assert.equal(rules('disabled', { enabled: false, status: 'disabled', availability: 'disabled' }).availability, 'off');
});

test('model failures are reported in words, and only while the model is on', () => {
  const error = { kind: 'timeout', message: 'no reply from Ollama within 20 s', at_unix_s: 1794845600 };
  const failed = aiPanel(answer({ status: 'fallback', availability: 'unavailable', last_error: error },
    { origin: 'deterministic', fallback_reason: 'timeout' }), live);
  assert.deepEqual([failed.error, failed.availability, failed.status],
    ['no reply from Ollama within 20 s', 'not reachable', 'Fell back to fixed rules']);
  assert.equal(aiPanel(answer({ availability: 'model_missing' }), live).availability, 'model not installed');
  assert.equal(aiPanel(answer({ enabled: false, last_error: error }), live).error, '');
});

test('before any answer, and without a decision, the panel says what it is waiting for', () => {
  const none = aiPanel(null, live);
  assert.deepEqual([none.badge, none.current, none.text], ['WAITING', false, '']);
  assert.match(none.note, /Asking the server/);
  assert.match(aiPanel(null, { ...live, pollError: 'HTTP 500' }).note, /not answering \(HTTP 500\)/);
  const idle = aiPanel(answer({ explanation: null, status: 'idle', is_current: false, stale_reason: 'no_data' }), live);
  assert.deepEqual([idle.badge, idle.text, idle.status], ['WAITING', '', 'Idle']);
  assert.match(idle.note, /No safety decision has been received/);
});

test('model text is carried as plain text, whatever it contains', () => {
  const hostile = '<img src=x onerror=alert(1)> RoadSense recommends braking.';
  assert.equal(aiPanel(answer({}, { text: hostile }), live).text, hostile); // main.js puts it on the page with textContent
});

test('a reused explanation says when the model wrote it, and for what', () => {
  const same = aiPanel(answer({}, { cached: true, written_for: { run_id: 'ffc51d2d138d', sequence: 40 } }), live);
  assert.deepEqual([same.badge, same.origin], ['CURRENT', 'AI model']);
  assert.match(same.note, /same kind of decision at sequence 40; the threat named is this one/);
  const earlier = aiPanel(answer({}, { cached: true, written_for: { run_id: 'an-earlier-run', sequence: 7 } }), live);
  assert.match(earlier.note, /at sequence 7 of an earlier run/);
});

test('what the model wrote too late is shown apart, as about a past decision, never as the explanation', () => {
  const late = {
    text: 'RoadSense recommends slowing down because pothole PH_009 is ahead in your lane.', origin: 'llm',
    latency_ms: 4071.2, generated_at_unix_s: 1794845609.0,
    source: { run_id: 'ffc51d2d138d', sequence: 88, risk: 'CAUTION', action: 'SLOW_DOWN', threat_id: 'PH_009' },
  };
  const rules = { origin: 'deterministic', fallback_reason: 'urgent', latency_ms: null, text: 'PH_001 is close. RoadSense recommends braking now.' };
  const panel = aiPanel(answer({ status: 'deterministic', latest_llm: late }, rules), live);
  assert.deepEqual([panel.badge, panel.text, panel.origin], ['CURRENT', rules.text, 'Fixed rules']);
  assert.equal(panel.past.text, late.text);
  assert.match(panel.past.note, /About CAUTION, SLOW DOWN, PH_009 at sequence 88, written in 4\.07 s\. That decision has passed/);

  // when the model's newest answer is the explanation on screen, it is not repeated below it
  const current = answer();
  assert.equal(aiPanel({ ...current, latest_llm: { ...late, generated_at_unix_s: current.explanation.generated_at_unix_s } }, live).past, null);
  assert.equal(aiPanel(answer(), live).past, null);
  // and with no decision on screen at all, it is still there to read
  assert.equal(aiPanel(answer({ explanation: null, status: 'idle', is_current: false, latest_llm: late }), live).past.text, late.text);
});

// --- wording

test('times and durations read naturally', () => {
  assert.deepEqual([0.84, 12.34, 999.9, 1000, 4071.2, null, NaN].map(msText),
    ['0.84 ms', '12.3 ms', '999.9 ms', '1.00 s', '4.07 s', '—', '—']);
  assert.deepEqual([0.42, 9.94, 12.3, 200, 5400, 7300, null].map(agoText),
    ['0.4 s ago', '9.9 s ago', '12 s ago', '3 min ago', '2 h ago', '2 h ago', '—']);
  assert.deepEqual([42, 720, 11100, 187200, null].map(uptimeText), ['42 s', '12 min', '3 h 05 min', '2 d 4 h', '—']);
  assert.equal(clockText(new Date(2026, 10, 16, 9, 5, 7)), '09:05:07');
  assert.equal(clockText(null), '—');
});

test('a measurement the host could not supply reads n/a, never zero', () => {
  assert.deepEqual([figure(48.35, 1, ' °C'), figure(12, 0, '%'), figure(null, 1, ' °C'), figure(undefined), figure(NaN)],
    ['48.4 °C', '12%', 'n/a', 'n/a', 'n/a']);
  assert.equal(throttleText(null), 'n/a');
  assert.equal(throttleText({ raw: '0x0', now: [], since_boot: [] }), 'None');
  assert.equal(throttleText({ raw: '0x50005', now: ['under_voltage', 'throttled'], since_boot: ['under_voltage', 'throttled'] }),
    'Now: under voltage, throttled');
  assert.equal(throttleText({ raw: '0x50000', now: [], since_boot: ['under_voltage', 'throttled'] }), 'Earlier: under voltage, throttled');
});

// --- polling

function harness(replies) {
  const timers = new Map();
  let nextId = 1;
  const clock = { now: 0 };
  const seen = { data: [], changes: 0, urls: [] };
  const pending = [];
  const poller = new Poller('/api/system', {
    onData: (data) => seen.data.push(data),
    onChange: () => { seen.changes += 1; },
    fetch: (url, options) => {
      seen.urls.push(url);
      return new Promise((resolve, reject) => {
        const reply = replies.shift();
        pending.push({ resolve, reject, options });
        if (reply === 'hang') {
          options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        } else if (reply instanceof Error) reject(reply);
        else resolve({ ok: reply.status === undefined, status: reply.status ?? 200, json: async () => reply });
      });
    },
    setTimeout: (fn, ms) => { timers.set(nextId, { fn, ms }); return nextId++; },
    clearTimeout: (id) => timers.delete(id),
    now: () => clock.now,
  });
  const fire = (ms) => {
    const [id, timer] = [...timers].find(([, t]) => t.ms === ms) ?? [];
    assert.ok(timer, `no timer of ${ms} ms among ${[...timers.values()].map((t) => t.ms)}`);
    timers.delete(id);
    timer.fn();
  };
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  return { poller, timers, clock, seen, fire, settle };
}

test('the poller asks once per turn, keeps the last answer through failures and says when it is stale', async () => {
  const h = harness([{ n: 1 }, new Error('connection refused'), { status: 503 }, { n: 2 }]);
  assert.equal(h.poller.stale(), true); // nothing yet
  h.poller.start();
  h.poller.start(); // already running: no second request
  await h.settle();
  assert.deepEqual([h.seen.data, h.seen.urls.length, h.poller.error], [[{ n: 1 }], 1, null]);
  assert.equal(h.poller.stale(), false);

  h.clock.now = 1000;
  h.fire(POLL_MS);
  await h.settle();
  assert.deepEqual([h.poller.error, h.poller.failures, h.poller.data], ['connection refused', 1, { n: 1 }]);
  h.clock.now = 2000;
  h.fire(POLL_MS);
  await h.settle();
  assert.deepEqual([h.poller.error, h.poller.failures], ['HTTP 503', 2]);
  assert.equal(h.poller.stale(), false);
  h.clock.now = POLL_STALE_MS + 1;
  assert.equal(h.poller.stale(), true); // the last good answer is too old to describe the present

  h.fire(POLL_MS);
  await h.settle();
  assert.deepEqual([h.poller.data, h.poller.error, h.poller.stale()], [{ n: 2 }, null, false]);
  assert.equal(h.seen.changes, 4);
});

test('a request that gets no answer is given up after the timeout, and the next turn still comes', async () => {
  const h = harness(['hang', { n: 1 }]);
  h.poller.start();
  await h.settle();
  h.fire(h.poller.timeoutMs);
  await h.settle();
  assert.deepEqual([h.poller.error, h.poller.failures], ['no answer in time', 1]);
  h.fire(POLL_MS);
  await h.settle();
  assert.deepEqual(h.seen.data, [{ n: 1 }]);
});

test('kick asks now, never twice at once, and stop ends the turns', async () => {
  const h = harness([{ n: 1 }, { n: 2 }, { n: 3 }, { n: 4 }]);
  h.poller.start();
  await h.settle();
  h.poller.kick(); // a decision changed: do not wait for the next turn
  await h.settle();
  assert.equal(h.seen.urls.length, 2);
  h.poller.kick();
  h.poller.kick(); // while the request is out: one more turn right after it, not a second request
  assert.equal(h.seen.urls.length, 3);
  await h.settle();
  h.fire(0);
  await h.settle();
  assert.equal(h.seen.urls.length, 4);
  h.poller.stop();
  assert.equal(h.timers.size, 0);
  h.poller.kick();
  assert.equal(h.seen.urls.length, 4);
});
