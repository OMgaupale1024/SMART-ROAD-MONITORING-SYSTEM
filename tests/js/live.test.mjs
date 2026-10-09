// The autonomy dashboard's live connection (src/roadsense/web/static/autonomy/live.js), without a browser:
// node --test tests/js/
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  LiveConnection, RETRY_AFTER_MS, STALE_AFTER_MS, connectionStatus, liveUrl, parseSnapshot,
} from '../../src/roadsense/web/static/autonomy/live.js';

// Real snapshots as /ws/live sent them during a Webots run: sequences 5, 6, 9, 101, 102 and the run's last, 1226.
const LINES = readFileSync(new URL('./recorded_snapshots.jsonl', import.meta.url), 'utf8').trim().split('\n');

test('the stream is ws:// on http pages and wss:// on https pages, on the page\'s own host', () => {
  assert.equal(liveUrl({ protocol: 'http:', host: '127.0.0.1:8000' }), 'ws://127.0.0.1:8000/ws/live');
  assert.equal(liveUrl({ protocol: 'https:', host: 'pi.local' }), 'wss://pi.local/ws/live');
});

test('a recorded snapshot parses as it was sent', () => {
  const snapshot = parseSnapshot(LINES[4]);
  assert.equal(snapshot.schema, 'roadsense.live.v1');
  assert.equal(snapshot.timestamp, 20.4);
  assert.equal(snapshot.simulation.sequence, 102);
  assert.equal(snapshot.tracks.length, 12);
  assert.equal(snapshot.hazards[0].hazard_id, 'PH_001');
});

test('anything but a roadsense.live.v1 snapshot is refused with the reason', () => {
  const good = JSON.parse(LINES[0]);
  const variant = (change) => JSON.stringify(change(structuredClone(good)));
  assert.throws(() => parseSnapshot('{"schema":'), /not JSON/);
  assert.throws(() => parseSnapshot('[1, 2]'), /not a snapshot object/);
  assert.throws(() => parseSnapshot('null'), /not a snapshot object/);
  assert.throws(() => parseSnapshot(variant((s) => { s.schema = 'roadsense.live.v2'; return s; })), /schema/);
  assert.throws(() => parseSnapshot(variant((s) => { delete s.simulation; return s; })), /simulation/);
  assert.throws(() => parseSnapshot(variant((s) => { s.simulation.run_id = ''; return s; })), /run_id/);
  assert.throws(() => parseSnapshot(variant((s) => { s.simulation.sequence = '5'; return s; })), /sequence/);
  assert.throws(() => parseSnapshot(variant((s) => { delete s.simulation.active; return s; })), /active/);
  assert.throws(() => parseSnapshot(variant((s) => { s.timestamp = null; return s; })), /timestamp/);
});

test('optional parts may be missing: drawing them is the view\'s job', () => {
  const bare = { schema: 'roadsense.live.v1', timestamp: 3.2, simulation: { run_id: 'r1', sequence: 16, active: true } };
  assert.deepEqual(parseSnapshot(JSON.stringify(bare)), bare);
});

// --- status: what the header pill says

const NOW = 100_000;
const live = { open: true, lost: false, received: 5, lastAt: NOW - 150, active: true };

test('status is LIVE only while fresh snapshots keep arriving', () => {
  assert.equal(connectionStatus(live, NOW), 'LIVE');
  assert.equal(connectionStatus(live, NOW - 150 + STALE_AFTER_MS), 'LIVE');
});

test('status turns STALE when no snapshot came for longer than STALE_AFTER_MS', () => {
  assert.equal(connectionStatus(live, NOW - 150 + STALE_AFTER_MS + 1), 'STALE');
});

test('one snapshot after connecting is the server\'s stored latest state, not proof of a running simulation', () => {
  const replayed = { ...live, received: 1, lastAt: NOW - 50 };
  assert.equal(connectionStatus(replayed, NOW), 'CONNECTING');
  assert.equal(connectionStatus(replayed, NOW - 50 + STALE_AFTER_MS + 1), 'STALE');
  assert.equal(connectionStatus({ ...replayed, received: 2 }, NOW), 'LIVE');
});

test('NO PRODUCER while connected without snapshots, or once the run has ended', () => {
  assert.equal(connectionStatus({ open: true, lost: false, received: 0, lastAt: null, active: null }, NOW), 'NO PRODUCER');
  assert.equal(connectionStatus({ ...live, active: false }, NOW), 'NO PRODUCER');
  // reconnected to a restarted server: the old run's state is still drawn, but nothing produces now
  assert.equal(connectionStatus({ ...live, received: 0 }, NOW), 'NO PRODUCER');
});

test('CONNECTING before the first connection, DISCONNECTED after any loss until connected again', () => {
  assert.equal(connectionStatus({ open: false, lost: false, received: 0, lastAt: null, active: null }, NOW), 'CONNECTING');
  assert.equal(connectionStatus({ ...live, open: false, lost: true }, NOW), 'DISCONNECTED');
});

// --- the WebSocket's life

class FakeSocket {
  static all = [];
  constructor(url) { this.url = url; this.closed = false; FakeSocket.all.push(this); }
  close() { this.closed = true; }
  // what the browser would do
  opened() { this.onopen?.({}); }
  receive(data) { this.onmessage?.({ data }); }
  dropped() { this.onclose?.({}); }
}

function harness() {
  FakeSocket.all = [];
  const timers = new Map();
  let nextTimer = 1;
  const clock = { now: 1000 };
  const seen = { snapshots: [], changes: 0 };
  const connection = new LiveConnection('ws://127.0.0.1:8000/ws/live', {
    WebSocket: FakeSocket,
    setTimeout: (fn, ms) => { timers.set(nextTimer, { fn, ms }); return nextTimer++; },
    clearTimeout: (id) => timers.delete(id),
    now: () => clock.now,
    onSnapshot: (snapshot, at) => seen.snapshots.push([snapshot.simulation.sequence, at]),
    onChange: () => { seen.changes += 1; },
  });
  const fire = () => {
    const [[id, timer]] = timers;
    timers.delete(id);
    timer.fn();
    return timer.ms;
  };
  return { connection, timers, clock, seen, fire, socket: () => FakeSocket.all.at(-1) };
}

test('start opens one socket to the stream and hands over each snapshot with its arrival time', () => {
  const h = harness();
  h.connection.start();
  h.connection.start(); // already connecting: no second socket
  assert.equal(FakeSocket.all.length, 1);
  assert.equal(h.socket().url, 'ws://127.0.0.1:8000/ws/live');
  assert.equal(h.connection.status(), 'CONNECTING');
  h.socket().opened();
  assert.equal(h.connection.status(), 'NO PRODUCER');
  h.socket().receive(LINES[3]);
  h.clock.now = 1200;
  h.socket().receive(LINES[4]);
  assert.deepEqual(h.seen.snapshots, [[101, 1000], [102, 1200]]);
  assert.equal(h.connection.latest.simulation.sequence, 102);
  assert.equal(h.connection.status(), 'LIVE');
});

test('a malformed message is counted and skipped; the stream carries on', () => {
  const h = harness();
  h.connection.start();
  h.socket().opened();
  h.socket().receive('{"schema": "roadsense.live.v1"');
  h.socket().receive(JSON.stringify({ schema: 'other' }));
  h.socket().receive(LINES[3]);
  assert.equal(h.connection.malformed, 2);
  assert.deepEqual(h.seen.snapshots.map(([sequence]) => sequence), [101]);
});

test('a lost connection keeps the last state, says DISCONNECTED and retries every RETRY_AFTER_MS', () => {
  const h = harness();
  h.connection.start();
  h.socket().opened();
  h.socket().receive(LINES[3]);
  h.socket().receive(LINES[4]);
  h.socket().dropped();
  assert.equal(h.connection.status(), 'DISCONNECTED');
  assert.equal(h.connection.latest.simulation.sequence, 102);
  assert.equal(h.timers.size, 1);
  assert.equal(h.fire(), RETRY_AFTER_MS);
  assert.equal(FakeSocket.all.length, 2);
  h.socket().dropped(); // server still down: try again, still DISCONNECTED, one timer at a time
  assert.equal(h.connection.status(), 'DISCONNECTED');
  assert.equal(h.timers.size, 1);
  h.fire();
  h.socket().opened(); // server back, but it has no state yet
  assert.equal(h.connection.status(), 'NO PRODUCER');
  assert.equal(FakeSocket.all.length, 3);
});

test('close stops for good: the socket closes, no retry is left, and late events change nothing', () => {
  const h = harness();
  h.connection.start();
  const first = h.socket();
  first.opened();
  h.connection.close();
  assert.equal(first.closed, true);
  first.dropped();
  first.receive(LINES[3]);
  assert.equal(h.timers.size, 0);
  assert.deepEqual(h.seen.snapshots, []);

  h.connection.start(); // e.g. the page is shown again from the back-forward cache
  assert.equal(FakeSocket.all.length, 2);
});

test('close during a retry wait cancels the retry', () => {
  const h = harness();
  h.connection.start();
  h.socket().dropped();
  assert.equal(h.timers.size, 1);
  h.connection.close();
  assert.equal(h.timers.size, 0);
  assert.equal(FakeSocket.all.length, 1);
});
