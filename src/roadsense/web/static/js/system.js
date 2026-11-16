/**
 * RoadSense dashboard: the read-only diagnostics endpoints (/api/system, /api/ai-explanation), asked on their own
 * schedule, apart from the safety stream; and what the AI panel may say about the explanation it received.
 *
 * An explanation is shown as CURRENT only while everything agrees: the stream is LIVE, the server answered a moment
 * ago and calls the explanation current, and the explanation is about the very decision on screen. Otherwise it is
 * marked UPDATING or STALE and drawn dimmed: old words are never passed off as the present. What the model last
 * wrote, when that is not the explanation on screen, is shown apart from it and named as about a past decision.
 * No DOM here, so Node's test runner can run it (tests/js/system.test.mjs).
 */

export const POLL_MS = 1000;
export const POLL_TIMEOUT_MS = 4000;
export const POLL_STALE_MS = 5000; // an answer older than this no longer describes the present

/** Asks one URL for JSON every intervalMs, one request at a time. onData(data) gets each answer, onChange() is called
 * after every attempt. kick() asks now (something on screen just changed) instead of waiting for the next turn. */
export class Poller {
  constructor(url, {
    onData = () => {},
    onChange = () => {},
    intervalMs = POLL_MS,
    timeoutMs = POLL_TIMEOUT_MS,
    fetch: get = (...args) => globalThis.fetch(...args),
    setTimeout: schedule = (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: cancel = (id) => globalThis.clearTimeout(id),
    now = () => globalThis.performance.now(),
  } = {}) {
    Object.assign(this, { url, onData, onChange, intervalMs, timeoutMs, get, schedule, cancel, now });
    this.data = null; // the last answer, kept through failures (and marked stale by the caller)
    this.lastOkAt = null;
    this.error = null; // why the last attempt failed, null after a success
    this.failures = 0;
    this.timer = null;
    this.busy = false;
    this.again = false;
    this.stopped = true;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.tick();
  }

  stop() {
    this.stopped = true;
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
  }

  kick() {
    if (this.stopped) return;
    if (this.busy) {
      this.again = true;
      return;
    }
    if (this.timer !== null) this.cancel(this.timer);
    this.tick();
  }

  /** True when no answer has come for POLL_STALE_MS (or ever). */
  stale(now = this.now()) {
    return this.lastOkAt === null || now - this.lastOkAt > POLL_STALE_MS;
  }

  async tick() {
    this.timer = null;
    this.busy = true;
    const controller = new AbortController();
    const timeout = this.schedule(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.get(this.url, { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.data = await response.json();
      this.lastOkAt = this.now();
      this.error = null;
      this.onData(this.data);
    } catch (error) {
      this.failures += 1;
      this.error = error?.name === 'AbortError' ? 'no answer in time' : error?.message || 'request failed';
    } finally {
      this.cancel(timeout);
      this.busy = false;
      if (!this.stopped) this.timer = this.schedule(() => this.tick(), this.again ? 0 : this.intervalMs);
      this.again = false;
      this.onChange();
    }
  }
}

// ---- text

const pad = (n) => String(n).padStart(2, '0');

/** A wall-clock time of day, HH:MM:SS, from a Date or Unix seconds; '—' without one. */
export function clockText(when) {
  if (when === null || when === undefined) return '—';
  const date = when instanceof Date ? when : new Date(when * 1000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** How long ago, from seconds: "0.4 s ago", "12 s ago", "3 min ago", "2 h ago". */
export function agoText(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, seconds);
  if (s < 10) return `${s.toFixed(1)} s ago`;
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

/** A duration given in milliseconds: "0.84 ms", "12.3 ms", "4.07 s". */
export function msText(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${ms.toFixed(ms < 10 ? 2 : 1)} ms`;
}

/** A running time given in seconds: "42 s", "12 min", "3 h 05 min", "2 d 4 h". */
export function uptimeText(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  if (seconds < 90) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h ${pad(minutes % 60)} min`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

/** A measured number with its unit, or "n/a" when the host could not supply it. */
export function figure(value, digits = 1, unit = '') {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'n/a';
  return `${value.toFixed(digits)}${unit}`;
}

const words = (name) => name.replaceAll('_', ' ');

/** The Raspberry Pi firmware's throttling state in words; n/a where there is none to read. */
export function throttleText(throttling) {
  if (!throttling) return 'n/a';
  if (throttling.now.length) return `Now: ${throttling.now.map(words).join(', ')}`;
  if (throttling.since_boot.length) return `Earlier: ${throttling.since_boot.map(words).join(', ')}`;
  return 'None';
}

// ---- the AI panel

const AVAILABILITY = {
  available: 'available', unavailable: 'not reachable', model_missing: 'model not installed', unknown: 'checking',
  disabled: 'off',
};
const STATUS = {
  disabled: 'Off', idle: 'Idle', queued: 'Queued', generating: 'Generating', ready: 'Ready',
  deterministic: 'Fixed rules', fallback: 'Fell back to fixed rules',
};
const RULES = 'This text is written from the safety decision by fixed rules.';
const NOTES = {
  disabled: `The AI model is off. ${RULES}`,
  urgent: 'Urgent alerts are never worded by the AI model. This text is written by fixed rules, at once.',
  nothing_to_explain: `There is nothing for the AI model to explain. ${RULES}`,
  awaiting_model: `The AI model is writing its explanation. Until then: ${RULES.toLowerCase()}`,
  timeout: `The AI model did not answer in time. ${RULES}`,
  unavailable: `Ollama is not reachable. ${RULES}`,
  http_error: `Ollama refused the request. ${RULES}`,
  invalid_reply: `Ollama's answer could not be read. ${RULES}`,
  invalid_output: `The AI model's answer was refused: it said something the safety decision does not support. ${RULES}`,
  error: `The explanation request failed. ${RULES}`,
};
const STALE_NOTES = {
  no_live_data: 'No sensor data is arriving: this explains the last decision received.',
  simulation_ended: 'The simulation has ended: this explains its last decision.',
  no_data: 'No safety decision has been received yet.',
  no_decision: 'The latest state has no safety decision.',
};
const STREAM_NOTES = {
  STALE: STALE_NOTES.no_live_data,
  'SIMULATION ENDED': STALE_NOTES.simulation_ended,
  DISCONNECTED: 'The live stream is disconnected: this explains the last decision received.',
  CONNECTING: 'Connecting to the live stream.',
  'NO PRODUCER': 'No simulation is sending.',
};

const describe = (source) => [source?.risk, source?.action?.replaceAll('_', ' '), source?.threat_id].filter(Boolean).join(', ') || '—';

/** True when the explanation is about the decision the live view shows: same run, risk, action, threat and lane. */
export function explainsDecision(explanation, model) {
  const source = explanation?.source;
  const safety = model?.safety;
  if (!source || !safety) return false;
  // Qualitative facts can change while the threat id and recommended action stay the same.
  const cause = source.cause;
  if (cause?.kind === 'road_hazard') {
    const hazard = safety.roadHazards.find((h) => h.id === cause.id);
    if (!hazard || cause.severity !== hazard.severity || cause.lane !== hazard.laneRelation
      || cause.hazard !== hazard.type) return false;
  } else if (cause?.kind === 'vehicle') {
    const track = model.tracks.find((t) => t.id === cause.id);
    const lane = ['ego_lane', 'left_lane', 'right_lane'].includes(track?.laneRelation) ? track.laneRelation : null;
    const position = Number.isFinite(track?.lon) ? (track.lon >= 0 ? 'ahead' : 'behind') : null;
    if (cause.lane !== lane || cause.position !== position) return false;
  }
  return source.run_id === model.runId && source.risk === safety.risk && source.action === safety.action
    && (source.threat_id ?? null) === (safety.threat?.id ?? null)
    && (source.threat_type ?? null) === (safety.threat?.type ?? null)
    && (source.recommended_lane ?? null) === (safety.lane ?? null);
}

/**
 * What the AI panel shows: ai is the last /api/ai-explanation answer (null before the first), model the live view
 * model on screen, stream the live stream's status, pollStale whether the endpoint stopped answering.
 * badge is CURRENT, UPDATING (the decision on screen is newer than the explanation), STALE, or WAITING.
 */
export function aiPanel(ai, { model = null, stream = 'CONNECTING', pollStale = false, pollError = null } = {}) {
  const panel = {
    badge: 'WAITING', current: false, text: '', origin: '', note: '', error: '',
    model: '—', availability: '', status: '—', latency: '—', latencyDetail: '', source: '—', explains: '—',
    generatedAt: null, generatedAgoS: null, past: null,
  };
  if (!ai) {
    panel.note = pollError ? `The explanation endpoint is not answering (${pollError}).` : 'Asking the server for the explanation.';
    return panel;
  }
  panel.model = ai.model ?? '—';
  panel.availability = AVAILABILITY[ai.availability] ?? String(ai.availability ?? '');
  panel.status = STATUS[ai.status] ?? String(ai.status ?? '—');
  const latency = ai.stats?.latency_ms;
  if (latency) panel.latencyDetail = `median ${msText(latency.p50)}, 95th percentile ${msText(latency.p95)}`;
  if (ai.last_error && ai.enabled) panel.error = ai.last_error.message ?? '';

  const explanation = ai.explanation;
  // the model's newest answer, when it is not what is on screen: it came too late for its decision
  const latest = ai.latest_llm;
  if (latest && !(explanation?.origin === 'llm' && explanation.generated_at_unix_s === latest.generated_at_unix_s)) {
    panel.past = {
      text: latest.text ?? '',
      note: `About ${describe(latest.source)} at sequence ${latest.source?.sequence ?? '—'}, written in ${msText(latest.latency_ms)}. `
        + 'That decision has passed; the words are kept for the next one of its kind.',
    };
  }
  if (!explanation) {
    panel.note = STALE_NOTES[ai.stale_reason] ?? STALE_NOTES.no_data;
    return panel;
  }
  panel.text = explanation.text ?? '';
  panel.origin = explanation.origin === 'llm' ? 'AI model' : 'Fixed rules';
  panel.latency = explanation.origin === 'llm' ? msText(explanation.latency_ms) : '—';
  const source = explanation.source ?? {};
  panel.source = `Sequence ${source.sequence ?? '—'}, t ${Number.isFinite(source.sim_time_s) ? source.sim_time_s.toFixed(1) : '—'} s`;
  panel.explains = describe(source);
  panel.generatedAt = explanation.generated_at_unix_s ?? null;
  if (Number.isFinite(ai.server_time_unix_s) && Number.isFinite(panel.generatedAt)) {
    panel.generatedAgoS = ai.server_time_unix_s - panel.generatedAt; // both on the server's clock
  }

  const notes = [];
  if (explanation.origin !== 'llm') notes.push(NOTES[explanation.fallback_reason] ?? RULES);
  else if (explanation.cached) {
    const written = explanation.written_for ?? {};
    notes.push(`The AI model wrote this for the same kind of decision at sequence ${written.sequence ?? '—'}${
      written.run_id && written.run_id !== source.run_id ? ' of an earlier run' : ''}; the threat named is this one.`);
  }

  if (pollStale) {
    panel.badge = 'STALE';
    notes.unshift(`The explanation endpoint is not answering${pollError ? ` (${pollError})` : ''}: this is the last one received.`);
  } else if (stream !== 'LIVE') {
    panel.badge = 'STALE';
    notes.unshift(STREAM_NOTES[stream] ?? STALE_NOTES.no_live_data);
  } else if (!ai.is_current) {
    panel.badge = 'STALE';
    notes.unshift(STALE_NOTES[ai.stale_reason] ?? STALE_NOTES.no_live_data);
  } else if (!explainsDecision(explanation, model)) {
    panel.badge = 'UPDATING';
    notes.unshift('The decision just changed: its explanation is on the way.');
  } else {
    panel.badge = 'CURRENT';
    panel.current = true;
  }
  panel.note = notes.join(' ');
  return panel;
}
