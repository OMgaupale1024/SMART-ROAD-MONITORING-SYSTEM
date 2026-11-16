// The dashboard's chart geometry (src/roadsense/web/static/js/charts.js): the parts without a DOM.
import assert from 'node:assert/strict';
import test from 'node:test';

import { History, linePath, nearestIndex, niceCeil, runs, windowOf } from '../../src/roadsense/web/static/js/charts.js';

test('an axis maximum is a round number at or above the data', () => {
  assert.deepEqual([80, 81, 100, 3.2, 0.42, 0.07, 1250].map(niceCeil), [100, 100, 100, 5, 0.5, 0.1, 2000]);
  assert.deepEqual([0, -3, NaN].map(niceCeil), [1, 1, 1]);
});

test('a line breaks where there is no value: nothing is drawn across a gap', () => {
  assert.equal(linePath([[0, 10], [5, 12.25], null, null, [20, 8], [25, 9]]), 'M0.0 10.0L5.0 12.3M20.0 8.0L25.0 9.0');
  assert.equal(linePath([null, [3, 4]]), 'M3.0 4.0');
  assert.equal(linePath([]), '');
});

test('a chart shows the last minute, or all there is while the run is younger: never a time before the data', () => {
  assert.deepEqual(windowOf(0.2, 34.4, 60), [0.2, 34.4]); // 34 s into a run: the axis starts where the data does
  assert.deepEqual(windowOf(0.2, 95, 60), [35, 95]); // later: the last minute
  assert.deepEqual(windowOf(5, 300, 60), [240, 300]);
  const [from, to] = windowOf(0.2, 0.2, 60); // one sample: still a stretch to draw on
  assert.ok(from < to && to === 0.2);
});

test('the readout snaps to the nearest sample', () => {
  const xs = [0.2, 0.4, 0.6, 1.4, 1.6];
  assert.deepEqual([0, 0.29, 0.31, 0.95, 1.05, 9].map((x) => nearestIndex(xs, x)), [0, 0, 1, 2, 3, 4]);
  assert.equal(nearestIndex([], 1), -1);
  assert.equal(nearestIndex([7], 1), 0);
});

test('history keeps a sliding window and starts again when time goes back', () => {
  const history = new History(60);
  for (let i = 1; i <= 400; i += 1) history.push({ x: i * 0.2, speed: i });
  assert.equal(history.rows.at(-1).x, 80);
  assert.ok(history.rows[0].x >= 20 - 1e-9 && history.rows[0].x < 20.21); // the last 60 s
  history.push({ x: 0.2, speed: 0 }); // a new run begins at t = 0.2
  assert.deepEqual(history.rows, [{ x: 0.2, speed: 0 }]);

  const capped = new History(1e9, 50);
  for (let i = 0; i < 500; i += 1) capped.push({ x: i });
  assert.equal(capped.rows.length, 50);
});

test('the risk strip joins consecutive samples of one level and leaves gaps where there is none', () => {
  const rows = [
    { x: 1.0, risk: 'SAFE' }, { x: 1.2, risk: 'SAFE' }, { x: 1.4, risk: 'HIGH' }, { x: 1.6, risk: null },
    { x: 1.8, risk: 'HIGH' }, { x: 2.0, risk: 'CRITICAL' },
  ];
  assert.deepEqual(runs(rows, 'risk', 0.2).map((run) => [run.from, Number(run.to.toFixed(1)), run.level]),
    [[1.0, 1.4, 'SAFE'], [1.4, 1.6, 'HIGH'], [1.8, 2.0, 'HIGH'], [2.0, 2.2, 'CRITICAL']]);
  assert.deepEqual(runs([], 'risk', 0.2), []);
});
