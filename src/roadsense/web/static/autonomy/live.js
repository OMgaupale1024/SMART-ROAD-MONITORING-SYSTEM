/**
 * RoadSense Autonomy: the live-state stream from the RoadSense web server (/ws/live, docs/live-state.md).
 *
 * One WebSocket at a time, reconnected after any loss. The server sends its latest snapshot on connecting, then each
 * new one; this module only checks that a message is a roadsense.live.v1 snapshot and tracks how fresh the stream is.
 * No DOM or Three.js here, so Node's test runner can run it (tests/js/live.test.mjs).
 */

export const SCHEMA = 'roadsense.live.v1';
export const STALE_AFTER_MS = 2000; // ten 200 ms cycles without a snapshot: not live any more
export const RETRY_AFTER_MS = 1000;

export function liveUrl(location) {
  return `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/live`;
}

/** The snapshot in a message's text; throws with the reason if it isn't a roadsense.live.v1 snapshot. Only what
 * identifies a snapshot is required: everything drawn from it is optional (see view.js). */
export function parseSnapshot(text) {
  let snapshot;
  try {
    snapshot = JSON.parse(text);
  } catch (error) {
    throw new Error(`not JSON: ${error.message}`);
  }
  if (snapshot === null || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
    throw new Error('not a snapshot object');
  }
  if (snapshot.schema !== SCHEMA) throw new Error(`schema is ${JSON.stringify(snapshot.schema)}, not ${SCHEMA}`);
  if (!Number.isFinite(snapshot.timestamp)) throw new Error('timestamp is not a number');
  const simulation = snapshot.simulation;
  if (simulation === null || typeof simulation !== 'object') throw new Error('no simulation object');
  if (typeof simulation.run_id !== 'string' || !simulation.run_id) throw new Error('simulation.run_id is missing');
  if (!Number.isInteger(simulation.sequence)) throw new Error('simulation.sequence is not an integer');
  if (typeof simulation.active !== 'boolean') throw new Error('simulation.active is not true or false');
  return snapshot;
}

/**
 * What the dashboard can say about the stream at time now (ms, the clock lastAt was taken with):
 *   CONNECTING    first connection not open yet, or open with only the server's stored latest snapshot so far
 *   DISCONNECTED  the connection was lost (or never made) and is being retried
 *   NO PRODUCER   connected, but no simulation sends: nothing received on this connection, or the run has ended
 *   STALE         the latest snapshot is older than STALE_AFTER_MS
 *   LIVE          snapshots keep arriving
 * received counts the snapshots of the current connection; active is the latest snapshot's simulation.active.
 */
export function connectionStatus({ open, lost, received, lastAt, active }, now) {
  if (!open) return lost ? 'DISCONNECTED' : 'CONNECTING';
  if (received === 0 || active === false) return 'NO PRODUCER';
  if (now - lastAt > STALE_AFTER_MS) return 'STALE';
  return received < 2 ? 'CONNECTING' : 'LIVE';
}

/** The stream: start() connects, close() stops (pagehide). onSnapshot(snapshot, arrivalTime) gets each valid snapshot,
 * onChange() is called whenever the connection's state changes. */
export class LiveConnection {
  constructor(url, {
    onSnapshot = () => {},
    onChange = () => {},
    WebSocket: Socket = globalThis.WebSocket,
    setTimeout: schedule = (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: cancel = (id) => globalThis.clearTimeout(id),
    now = () => globalThis.performance.now(),
  } = {}) {
    Object.assign(this, { url, onSnapshot, onChange, Socket, schedule, cancel, now });
    this.socket = null;
    this.retry = null;
    this.open = false;
    this.lost = false;
    this.received = 0;
    this.lastAt = null;
    this.latest = null; // kept across reconnections: the last state stays drawn, marked by the status
    this.malformed = 0;
  }

  start() {
    if (this.socket || this.retry) return;
    this.connect();
  }

  close() {
    if (this.retry) this.cancel(this.retry);
    const socket = this.socket;
    this.retry = this.socket = null;
    this.open = false;
    socket?.close();
  }

  status(now = this.now()) {
    return connectionStatus({ ...this, active: this.latest?.simulation.active ?? null }, now);
  }

  connect() {
    const socket = new this.Socket(this.url);
    this.socket = socket;
    // events of a socket that was closed or replaced are ignored
    socket.onopen = () => {
      if (socket !== this.socket) return;
      this.open = true;
      this.lost = false;
      this.received = 0;
      this.onChange();
    };
    socket.onmessage = (event) => {
      if (socket !== this.socket) return;
      let snapshot;
      try {
        snapshot = parseSnapshot(event.data);
      } catch (error) {
        if (this.malformed++ === 0) console.warn('RoadSense live state: skipping a malformed message:', error.message);
        return;
      }
      this.received += 1;
      this.lastAt = this.now();
      this.latest = snapshot;
      this.onSnapshot(snapshot, this.lastAt);
      this.onChange();
    };
    socket.onclose = () => {
      if (socket !== this.socket) return;
      this.socket = null;
      this.open = false;
      this.lost = true;
      this.retry = this.schedule(() => {
        this.retry = null;
        this.connect();
      }, RETRY_AFTER_MS);
      this.onChange();
    };
  }
}
