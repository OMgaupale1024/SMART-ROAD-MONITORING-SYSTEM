# The RoadSense dashboard

One page, at the server's root: `http://127.0.0.1:8000/` on the computer that runs the edge server,
`http://192.168.0.104:8001/` for the Raspberry Pi. It shows what RoadSense perceives, recommends and
measures for the Webots digital twin, live. The old `/autonomy` address redirects to it; there is no
other page.

The page draws RoadSense's state and computes none of it: no tracking, prediction, TTC, risk, severity,
target speed or action of its own. Everything that arrives over the network is put on the page as text,
never as markup.

## What it shows

| Section | From | Content |
| ------- | ---- | ------- |
| **Status rail** | the stream's state, `/api/system` | edge server (uptime, version), simulation (streaming, silent, ended, none), processing (edge or in Webots, on which host), AI model and Ollama's availability, sensor stream (frames per second, gaps), run and sequence, time of the last data, interruptions since the page opened |
| **3D twin** | `/ws/live` | the car, the lanes, each tracked vehicle with its predicted path, the mapped potholes with labels, the risk colours, the suggested lane tinted, the recommendation over the road |
| **Next move** | `unified_safety` | risk, action and what the action means, current and target speed on a speed tape, recommended lane, minimum time to collision, primary threat, the safety processor's reason, and who decided (edge server or Webots) |
| **Explanation** | `/api/ai-explanation` | the explanation of the decision, whether it is current, who wrote it (the model or fixed rules) and why, model, status, inference time, when it was generated, which sequence and decision it is about, and the model's latest answer if that came too late for its decision ([ai-explanations.md](ai-explanations.md)) |
| **Vehicle telemetry** | `/ws/live` | speed against target speed and minimum TTC over the last 60 s, the risk level over the same time, acceleration, lane, steering angle, tracked vehicles, nearest threat, update rate |
| **Road hazards** | `hazards` | each mapped pothole once: id, severity, distance, lane, confidence, observations, first and last seen, source; those ahead, then those passed ([hazard-records.md](hazard-records.md)) |
| **Edge processing** | `/api/system` | frames in, data age, processing time of the pipeline and of the whole request, frames accepted, refused and missed, CPU, memory, temperature, throttling, the server's memory, the model's inference time, and a chart of the processing time per frame ([edge-metrics.md](edge-metrics.md)) |
| **Recent events** | all of the above | new hazards, tracks entering a predicted conflict, risk and action changes, lane-change suggestions, critical braking warnings, explanations written by the model, model failures, stream losses and recoveries |

Risk is always written out (SAFE, CAUTION, HIGH, CRITICAL); its colour only repeats the word. The
recommendations are labelled as **recommendations for driving in the simulation**: they are not
validated commands for a real vehicle, and nothing on the page controls the simulated car.

## Connection states

| State | When |
| ----- | ---- |
| `LIVE` | snapshots keep arriving |
| `CONNECTING` | the first connection is being opened; or it is open with only the server's stored latest snapshot so far |
| `STALE` | no snapshot for more than 2 s: the simulation paused, or stopped without ending |
| `DISCONNECTED` | the server is unreachable; the page retries every second |
| `NO PRODUCER` | connected, but the server has received nothing since it started |
| `SIMULATION ENDED` | the latest snapshot is the last one of a finished run |

Only `LIVE` means that what is on screen is the present. In every other state the last state stays on
screen, dimmed, with a note saying what is wrong, how old the data is and what to do. The server sends
its latest snapshot to every client that connects, so one snapshot proves nothing about a running
simulation: the page says `LIVE` only from the second. The age shown for old data is the server's own
measurement, not the time since the page was opened.

The explanation has its own marker: `CURRENT` only while the stream is `LIVE`, the explanation endpoint
answered within 5 s and calls it current, and the explanation matches the decision and its qualitative threat facts on screen;
otherwise `UPDATING` or `STALE`, dimmed.

## Events

Events are found by comparing received snapshots. A change that persists across skipped snapshots is
still detected; a transient event entirely between two received snapshots can be missed. A
changed decision is one event that names what changed (a critical braking warning, a lane-change
suggestion, a new action with the risk if it rose, or just the risk or the primary threat). Repeats
about the same thing within 5 s fold into one row with a count, so a risk level flickering five times a
second is one row, not twenty-five. The feed keeps the last 40.

## Design

- **Asphalt and road paint.** Graphite surfaces and pale ink; colour only ever says something, and it
  follows the road-sign code: green guidance (SAFE), yellow warning (CAUTION), orange work zone (HIGH),
  red stop (CRITICAL), blue information (measurements, the suggested lane). The four risk colours were
  checked for separation between neighbours under colour-vision deficiency and for contrast as text.
- **A cockpit that does not move.** The twin and the telemetry fill the window's height beside the
  decision column; the column takes its height from them and scrolls inside when its text is longer, so
  the 3D view never resizes while the page is open. Hazards, edge processing and events are below.
- **Charts** are SVG drawn by `charts.js`, without a chart library: thin lines, a recessive grid, one
  axis per chart, a crosshair readout on hover or with the arrow keys, and a table of the same numbers
  under each. The TTC chart's reference levels are the safety processor's own, sent by the server.
- **Tablets and narrow screens.** Up to 1180 px the status rail wraps to two rows and the lower panels
  pair up; up to 820 px everything is one column: the twin, the decision, the telemetry, the rest.
- **Without WebGL** the twin says so and the panels carry on: the 3D module is loaded on its own.
- **Reduced motion** is respected: the few animations (a waiting dot, a tint when the decision changes,
  the pulse of a critical alert) are off.

### The 3D twin

- **Axes.** RoadSense's ego frame has x (longitudinal) ahead and y (lateral) left. The scene uses three.js's
  axes, +x right, +y up, -z ahead, so `x = -lateral`, `z = -longitudinal`, and a heading (positive left)
  is a rotation about +y by the same angle. Tracks, trajectories, potholes and lanes all go through this
  one transform (`toScene` in `view.js`).
- **Smooth motion.** Snapshots arrive about 5 times a second; the page draws at the display's rate. Each
  drawn position moves in a straight line from where it is drawn to the newest snapshot's value over a
  little more than the time between snapshots, so the view runs about 0.2 s behind the stream. Only the
  drawing is eased; values shown as text are the snapshot's.
- **Only what RoadSense sent.** One car per confirmed track, at RoadSense's size for a radar target,
  facing the way it moves; a predicted path as faint dots while safe and as a ribbon in the risk colour
  once a conflict is predicted; a pothole at its real size with its severity's colour. A track RoadSense
  drops disappears. The suggested lane is `unified_safety.recommended_lane`, tinted.
- **three.js** r185 (npm `three@0.185.1`, MIT, `vendor/three/LICENSE`): `build/three.module.min.js` and
  `build/three.core.min.js`, unmodified, served locally. WebGL 2 only: no WebGPU, shadows or
  post-processing.

### Files

`src/roadsense/web/static/`: `index.html`, `dashboard.css`, and `js/`:

| File | Role | Tested by |
| ---- | ---- | --------- |
| `live.js` | the WebSocket, the snapshot check, the six connection states | `tests/js/live.test.mjs` |
| `view.js` | snapshot to view model, text, events and their folding, easing | `tests/js/view.test.mjs` |
| `system.js` | the two polled endpoints, what the explanation panel may say | `tests/js/system.test.mjs` |
| `charts.js` | the charts and their geometry | `tests/js/charts.test.mjs` |
| `scene.js` | the 3D twin (three.js) | in a browser |
| `main.js` | the page: wiring and rendering | `tests/test_web.py` (every element it looks up exists; no markup sinks) |

There is no build step and no framework: plain ES modules, loaded as they are.

## Checking it in a browser

`tests/browser_smoke.py` checks the rendered page with an isolated browser profile. The following is the browser checklist. For dated results from the current working tree, see
[validation.md](validation.md). Use isolated headless Chromium driven
over the DevTools protocol, the real server, and either a scripted sensor-frame scenario or the real
Webots simulation:

- 1440 x 900, 1024 x 768, 820 x 1180 and 430 x 932: no horizontal overflow; the single-column order.
- each connection state: `NO PRODUCER` on a fresh server, `STALE` after the producer vanished (also when
  the page is opened later), `SIMULATION ENDED`, `DISCONNECTED` with the server killed and restarted.
- the explanation: deterministic for urgent decisions, the model's words when reused for a decision of
  the same kind, a late answer shown as past, a refused answer reported.
- WebGL disabled: the notice, with the panels live.
- hostile strings in ids, reasons and sources: drawn as text, nothing executed.

## Limits

- Straight road only: lanes are drawn straight along `road.heading_deg`, as this world's carriageway is.
- The view runs about 0.2 s behind the stream (the easing); the stream itself is the 200 ms cycle.
- A car's direction comes from its velocity, so a car RoadSense sees as standing still faces along the road.
- One simulation at a time, as for the stream itself.
- The charts hold what arrived while the page was open (60 s); they do not ask the server for history.
