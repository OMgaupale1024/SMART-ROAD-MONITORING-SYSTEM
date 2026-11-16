/**
 * RoadSense dashboard: the small charts, drawn as SVG without a chart library.
 *
 * One LineChart draws a few series over a sliding window (speed against its target, the minimum TTC, the edge's
 * processing time); a RiskStrip draws the risk level over the same window; a Ticker draws the time between frames.
 * Marks are thin and the grid recessive; a crosshair with a readout follows the pointer (or the arrow keys), and
 * every chart has a table of the same numbers. Colours come from the stylesheet: a mark's class names its series.
 * The geometry helpers have no DOM in them, so Node's test runner can run them (tests/js/charts.test.mjs).
 */

const NS = 'http://www.w3.org/2000/svg';
const PAD = { left: 30, right: 10, top: 8, bottom: 16 };

/** The smallest of 1, 2, 2.5, 5, 10 x a power of ten that is at least value: an axis maximum people can read. */
export function niceCeil(value) {
  if (!(value > 0)) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * power >= value - 1e-9) return step * power;
  }
  return 10 * power;
}

/** An SVG path through points ([x, y] or null); a null (no value at that time) breaks the line. */
export function linePath(points) {
  let path = '';
  let pen = false;
  for (const point of points) {
    if (!point) {
      pen = false;
    } else {
      path += `${pen ? 'L' : 'M'}${point[0].toFixed(1)} ${point[1].toFixed(1)}`;
      pen = true;
    }
  }
  return path;
}

/** The stretch of x a chart shows: the last span of it, or less while the data is younger than that (so a run's
 * first seconds fill the plot instead of leaving it empty, and no time before the data began is on the axis). */
export function windowOf(first, last, span) {
  const from = Math.max(last - span, first);
  return last > from ? [from, last] : [last - 1, last];
}

/** The index of the value in ascending xs nearest to x; -1 for an empty list. */
export function nearestIndex(xs, x) {
  if (!xs.length) return -1;
  let low = 0;
  let high = xs.length - 1;
  while (high - low > 1) {
    const mid = (low + high) >> 1;
    if (xs[mid] <= x) low = mid;
    else high = mid;
  }
  return Math.abs(xs[high] - x) < Math.abs(xs[low] - x) ? high : low;
}

/** Rows over time, bounded: push(row) drops rows older than spanX behind the newest (and beyond limit). */
export class History {
  constructor(spanX, limit = 600) {
    Object.assign(this, { spanX, limit, rows: [] });
  }

  push(row) {
    const { rows } = this;
    if (rows.length && row.x < rows.at(-1).x) rows.length = 0; // time went back: a new run
    rows.push(row);
    while (rows.length > this.limit || row.x - rows[0].x > this.spanX) rows.shift();
  }

  clear() {
    this.rows.length = 0;
  }
}

/** Consecutive rows with the same level as [{ from, to, level }], for the risk strip; a null level is a gap. */
export function runs(rows, key, lastWidth) {
  const result = [];
  rows.forEach((row, i) => {
    const to = rows[i + 1]?.x ?? row.x + lastWidth;
    const last = result.at(-1);
    if (row[key] === null || row[key] === undefined) return;
    if (last && last.level === row[key] && Math.abs(last.to - row.x) < 1e-9) last.to = to;
    else result.push({ from: row.x, to, level: row[key] });
  });
  return result;
}

const svg = (tag, attrs = {}, parent = null) => {
  const element = document.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
  parent?.append(element);
  return element;
};
const html = (tag, className, text, parent = null) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  parent?.append(element);
  return element;
};

/**
 * A line chart of rows [{ x, <series key>: number|null }] over the last spanX of x.
 *   series      [{ key, label, tone, dashed }]: tone is the CSS class that colours the mark
 *   yMax        the axis maximum, or null to fit the data (never below yFloor)
 *   thresholds  [{ value, label, tone }]: reference levels drawn across the plot
 *   xText, yText  how a position and a value read in the readout and the table
 */
export class LineChart {
  constructor(host, { series, spanX, yMax = null, yFloor = 1, thresholds = [], xText, yText, label, height = 96 }) {
    Object.assign(this, { host, series, spanX, yMax, yFloor, thresholds, xText, yText, height });
    this.rows = [];
    this.pointer = null; // the x the reader points at, in data units; null when not pointing
    host.classList.add('chart');
    if (series.length > 1) {
      const legend = html('div', 'chart-legend', undefined, host);
      for (const s of series) {
        const item = html('span', 'chart-legend-item', undefined, legend);
        html('i', `key tone-${s.tone}${s.dashed ? ' dashed' : ''}`, undefined, item);
        html('span', '', s.label, item);
      }
    }
    this.plot = html('div', 'chart-plot', undefined, host);
    this.svg = svg('svg', { role: 'img', tabindex: '0', 'aria-label': label, height }, this.plot);
    this.grid = svg('g', { class: 'chart-grid' }, this.svg);
    this.levels = svg('g', { class: 'chart-levels' }, this.svg);
    this.paths = series.map((s) => svg('path', { class: `chart-line tone-${s.tone}${s.dashed ? ' dashed' : ''}` }, this.svg));
    this.ends = series.map((s) => svg('circle', { class: `chart-end tone-${s.tone}`, r: 4 }, this.svg));
    this.cross = svg('line', { class: 'chart-cross', visibility: 'hidden' }, this.svg);
    this.dots = series.map((s) => svg('circle', { class: `chart-end tone-${s.tone}`, r: 4, visibility: 'hidden' }, this.svg));
    this.tip = html('div', 'chart-tip', undefined, this.plot);
    this.tip.hidden = true;
    this.details = html('details', 'chart-table', undefined, host);
    html('summary', '', 'Table', this.details);
    this.table = html('table', '', undefined, this.details);
    this.details.addEventListener('toggle', () => this.drawTable());

    this.svg.addEventListener('pointermove', (event) => this.point(this.toX(event.clientX - this.svg.getBoundingClientRect().left)));
    this.svg.addEventListener('pointerleave', () => this.point(null));
    this.svg.addEventListener('focus', () => this.point(this.rows.at(-1)?.x ?? null));
    this.svg.addEventListener('blur', () => this.point(null));
    this.svg.addEventListener('keydown', (event) => {
      const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (event.key === 'Escape') this.point(null);
      if (!step || !this.rows.length) return;
      event.preventDefault();
      const xs = this.rows.map((row) => row.x);
      const index = Math.min(xs.length - 1, Math.max(0, nearestIndex(xs, this.pointer ?? xs.at(-1)) + step));
      this.point(xs[index]);
    });
    new ResizeObserver(() => this.draw()).observe(this.plot);
  }

  update(rows) {
    this.rows = rows;
    this.draw();
    if (this.details.open) this.drawTable();
  }

  point(x) {
    this.pointer = x;
    this.drawReadout();
  }

  toX(px) {
    const [x0, x1] = this.domain;
    return x0 + ((px - PAD.left) / Math.max(1, this.width - PAD.left - PAD.right)) * (x1 - x0);
  }

  draw() {
    const width = this.plot.clientWidth;
    if (!width) return;
    this.width = width;
    const { height, rows, series } = this;
    this.svg.setAttribute('width', width);
    this.svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const last = rows.at(-1)?.x ?? this.spanX;
    this.domain = windowOf(rows[0]?.x ?? last - this.spanX, last, this.spanX);
    const peak = Math.max(this.yFloor, ...this.thresholds.map((t) => t.value),
      ...rows.flatMap((row) => series.map((s) => row[s.key] ?? 0)));
    const top = this.yMax ?? niceCeil(peak);
    const sx = (x) => PAD.left + ((x - this.domain[0]) / (this.domain[1] - this.domain[0])) * (width - PAD.left - PAD.right);
    const sy = (y) => height - PAD.bottom - (Math.min(y, top) / top) * (height - PAD.top - PAD.bottom);
    Object.assign(this, { sx, sy, top });

    this.grid.replaceChildren();
    for (const value of [0, top / 2, top]) {
      svg('line', { x1: PAD.left, x2: width - PAD.right, y1: sy(value), y2: sy(value) }, this.grid);
      svg('text', { x: PAD.left - 6, y: sy(value) + 3.5, 'text-anchor': 'end' }, this.grid).textContent = this.yText(value, true);
    }
    svg('text', { x: PAD.left, y: height - 3 }, this.grid).textContent = this.xText(this.domain[0]);
    svg('text', { x: width - PAD.right, y: height - 3, 'text-anchor': 'end' }, this.grid).textContent = this.xText(last);

    this.levels.replaceChildren();
    for (const level of this.thresholds) {
      svg('line', { class: `tone-${level.tone}`, x1: PAD.left, x2: width - PAD.right, y1: sy(level.value), y2: sy(level.value) }, this.levels);
      svg('text', { x: width - PAD.right - 2, y: sy(level.value) - 3, 'text-anchor': 'end' }, this.levels).textContent = level.label;
    }

    series.forEach((s, i) => {
      const points = rows.map((row) => (row[s.key] === null || row[s.key] === undefined || row.x < this.domain[0]
        ? null : [sx(row.x), sy(row[s.key])]));
      this.paths[i].setAttribute('d', linePath(points));
      const end = points.at(-1);
      this.ends[i].setAttribute('visibility', end ? 'visible' : 'hidden');
      if (end) {
        this.ends[i].setAttribute('cx', end[0]);
        this.ends[i].setAttribute('cy', end[1]);
      }
    });
    this.drawReadout();
  }

  drawReadout() {
    const { rows, series } = this;
    const index = this.pointer === null ? -1 : nearestIndex(rows.map((row) => row.x), this.pointer);
    const row = rows[index];
    const shown = Boolean(row) && Boolean(this.sx) && row.x >= this.domain[0];
    this.cross.setAttribute('visibility', shown ? 'visible' : 'hidden');
    this.tip.hidden = !shown;
    this.dots.forEach((dot) => dot.setAttribute('visibility', 'hidden'));
    if (!shown) return;
    const x = this.sx(row.x);
    for (const [name, value] of [['x1', x], ['x2', x], ['y1', PAD.top], ['y2', this.height - PAD.bottom]]) {
      this.cross.setAttribute(name, value);
    }
    this.tip.replaceChildren(html('div', 'chart-tip-when', this.xText(row.x)));
    series.forEach((s, i) => {
      const value = row[s.key];
      const line = html('div', 'chart-tip-row', undefined, this.tip);
      html('i', `key tone-${s.tone}${s.dashed ? ' dashed' : ''}`, undefined, line);
      html('b', '', value === null || value === undefined ? 'none' : this.yText(value), line);
      html('span', '', s.label, line);
      if (value === null || value === undefined) return;
      this.dots[i].setAttribute('visibility', 'visible');
      this.dots[i].setAttribute('cx', x);
      this.dots[i].setAttribute('cy', this.sy(value));
    });
    const tipWidth = this.tip.offsetWidth;
    this.tip.style.left = `${Math.min(this.width - tipWidth - 4, Math.max(4, x > this.width / 2 ? x - tipWidth - 10 : x + 10))}px`;
  }

  /** The same numbers as text: the newest 12 rows, newest first, only while the table is open. */
  drawTable() {
    if (!this.details.open) return;
    const head = html('tr');
    html('th', '', 'When', head);
    for (const s of this.series) html('th', '', s.label, head);
    const body = this.rows.slice(-12).reverse().map((row) => {
      const line = html('tr');
      html('td', '', this.xText(row.x), line);
      for (const s of this.series) html('td', '', row[s.key] === null || row[s.key] === undefined ? 'none' : this.yText(row[s.key]), line);
      return line;
    });
    this.table.replaceChildren(head, ...body);
  }
}

/** The risk level over the last spanX of x, as a strip of blocks: rows are [{ x, risk }]. */
export class RiskStrip {
  constructor(host, { spanX, step, xText, label }) {
    Object.assign(this, { host, spanX, step, xText, rows: [] });
    host.classList.add('strip');
    this.svg = svg('svg', { role: 'img', 'aria-label': label, height: 14 }, host);
    this.tip = html('div', 'chart-tip', undefined, host);
    this.tip.hidden = true;
    this.svg.addEventListener('pointermove', (event) => {
      const width = this.svg.clientWidth;
      const last = this.rows.at(-1)?.x;
      if (!width || last === undefined) return;
      const px = event.clientX - this.svg.getBoundingClientRect().left;
      const [from, to] = windowOf(this.rows[0].x, last, this.spanX);
      const row = this.rows[nearestIndex(this.rows.map((r) => r.x), from + (px / width) * (to - from))];
      this.tip.hidden = !row;
      if (!row) return;
      this.tip.replaceChildren(html('div', 'chart-tip-when', this.xText(row.x)), html('b', '', row.risk ?? 'none'));
      this.tip.style.left = `${Math.min(width - this.tip.offsetWidth - 4, Math.max(4, px + 8))}px`;
    });
    this.svg.addEventListener('pointerleave', () => { this.tip.hidden = true; });
    new ResizeObserver(() => this.draw()).observe(host);
  }

  update(rows) {
    this.rows = rows;
    this.draw();
  }

  draw() {
    const width = this.host.clientWidth;
    if (!width) return;
    this.svg.setAttribute('width', width);
    const last = (this.rows.at(-1)?.x ?? this.spanX) + this.step; // the newest sample's block ends a step later
    const [from, to] = windowOf(this.rows[0]?.x ?? last - this.spanX, last, this.spanX);
    const sx = (x) => ((Math.max(x, from) - from) / (to - from)) * width;
    // a 2px gap in the surface colour separates neighbouring blocks: no outlines
    this.svg.replaceChildren(...runs(this.rows, 'risk', this.step).filter((run) => run.to > from).map((run) => svg('rect', {
      class: `tone-${run.level}`, x: sx(run.from), y: 2, height: 10, rx: 2,
      width: Math.max(1, Math.min(width, sx(run.to)) - sx(run.from) - 2),
    })));
  }
}

/** The time between the newest frames as thin columns, newest at the right: a gap in the stream is a tall one. */
export class Ticker {
  constructor(host, { count = 36, fullMs = 600, label }) {
    Object.assign(this, { count, fullMs });
    this.svg = svg('svg', { role: 'img', 'aria-label': label, width: count * 4, height: 18, class: 'ticker' }, host);
    this.bars = Array.from({ length: count }, (_, i) => svg('rect', { x: i * 4, width: 3, y: 17, height: 1, rx: 1 }, this.svg));
  }

  update(intervals) {
    const recent = intervals.slice(-this.count);
    this.bars.forEach((bar, i) => {
      const ms = recent[i - (this.count - recent.length)];
      const height = ms === undefined ? 1 : Math.max(2, Math.min(1, ms / this.fullMs) * 18);
      bar.setAttribute('y', 18 - height);
      bar.setAttribute('height', height);
    });
  }
}
