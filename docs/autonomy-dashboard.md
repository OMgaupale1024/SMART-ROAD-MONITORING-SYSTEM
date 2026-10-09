# RoadSense Autonomy (3D dashboard)

A live 3D view of what RoadSense perceives and recommends in the Webots digital twin, at
`http://127.0.0.1:8000/autonomy`. It draws the [live-state stream](live-state.md): every vehicle, path,
TTC, risk level, pothole and recommendation on it comes from RoadSense in that stream. The page computes no
tracking, prediction, TTC, risk, severity, target speed or action of its own; it only places, labels and
eases what it receives.

## Running it

From the repository root, in two terminals, as in [live-state.md](live-state.md#running-it):

1. The web server (the virtual environment with the `web` extra):

   ```sh
   venv/bin/python -m roadsense.web --no-browser
   ```

2. Webots with publishing on:

   ```sh
   export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/share/sumo"
   export PATH="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/bin:$PATH"
   export WEBOTS_EXTRA_PROJECT_PATH="/Applications/Webots.app/Contents/projects/vehicles"
   export ROADSENSE_LIVE_PUBLISH=1
   /Applications/Webots.app/Contents/MacOS/webots simulation/webots/worlds/roadsense_highway.wbt
   ```

3. Open `http://127.0.0.1:8000/autonomy` (or **Autonomy 3D** in the telemetry dashboard's tab bar).

Any order works: the page waits for the server and the server waits for the simulation. Add
`?presentation=1` to the address for screenshots and demos: it leaves out the navigation link. The
browser's full-screen mode does the rest.

## What it shows

- **The road** around `EGO_ROADSENSE`, from the snapshot's `road`: its lanes, dashed between driving lanes,
  the outer lane SUMO keeps for pedestrians as a darker shoulder, and the crash barriers. The markings move
  with the car's distance along the road.
- **EGO_ROADSENSE**, a light sedan that stays in place while everything else moves around it. Its outline
  is the overall risk's colour; its front wheels turn with `steering_rad`.
- **Tracked vehicles**: one car per confirmed `TRACK_xxx`, at RoadSense's size for a radar target
  (5 x 1.9 m), facing the way it moves over the ground. A car at risk is tinted and outlined in its risk
  colour. A track RoadSense drops disappears.
- **Predicted trajectories**: each track's 0.5–5 s points as RoadSense sent them, relative to the car.
  Faint dots while the track is SAFE; a ribbon in its risk colour once RoadSense predicts a conflict. The
  paths of SAFE oncoming traffic beyond the median are left out (they would streak down the other side).
- **Labels** for the primary threat and other tracks at risk (at most three, worst first, with TTC and
  risk), the track under the pointer, and each pothole ahead (id, severity, distance).
- **Potholes**: a dark patch of the hazard's real length and width with a rim and a marker in its
  severity's colour. Once passed they leave the 3D view but stay in the hazard map list and count.
- **Safety decision**: `unified_safety` as it is: overall risk, recommended action, target speed,
  recommended lane and reason. **Primary threat**: its id, with the track's TTC and distance or the
  pothole's severity and distance.
- **Instruments**: speed, target speed, minimum TTC (`vehicle_safety.minimum_ttc_s`), confirmed tracks,
  mapped hazards and the driving controller's target lane.
- **Events**, the last twelve, found by comparing each snapshot with the one before: a pothole mapped, a
  track entering a predicted conflict, a track's risk changing, a new recommendation or primary threat, a
  new run, the run's end.

Risk is always written out (SAFE, CAUTION, HIGH, CRITICAL); its colour only repeats the word.

## Connection states

| Status        | When                                                                                         |
| ------------- | -------------------------------------------------------------------------------------------- |
| `CONNECTING`  | first connection being opened; or open with only the server's stored latest snapshot so far |
| `LIVE`        | snapshots keep arriving                                                                      |
| `STALE`       | no snapshot for more than 2 s (the simulation paused or stopped without ending)             |
| `NO PRODUCER` | connected, but nothing received since connecting, or the run has ended (`active` false)     |
| `DISCONNECTED`| the server is unreachable; the page retries every second                                    |

The server sends its latest snapshot to every client that connects, so a single snapshot proves nothing
about a running simulation: the page says `LIVE` only from the second. In every state but `LIVE` the last
state stays on screen, dimmed, with a note saying what is wrong and how old it is.

## Design notes

- **Axes.** RoadSense's ego frame has x (longitudinal) ahead and y (lateral) left. The scene uses three.js's
  axes, +x right, +y up, -z ahead, so `x = -lateral`, `z = -longitudinal`, and a heading (positive left)
  is a rotation about +y by the same angle. Tracks, trajectories, potholes and lanes all go through this one
  transform (`toScene` in `view.js`).
- **Smooth motion.** Snapshots arrive about 5 times a second; the page draws at the display's rate. Each
  drawn position moves in a straight line from where it is drawn to the newest snapshot's value over a
  little more than the time between snapshots, so the view runs about 0.2 s behind the stream. Only the
  drawing is eased; values shown as text are the snapshot's.
- **Files.** `src/roadsense/web/static/autonomy/`: `live.js` (WebSocket, schema check, connection state),
  `view.js` (snapshot to view model, axes, labels, text, events, easing), `scene.js` (three.js), `main.js`
  (the page). `live.js` and `view.js` use no browser API, so `tests/js/` tests them with Node.
- **three.js** r185 (npm `three@0.185.1`, MIT, `vendor/three/LICENSE`): `build/three.module.min.js` and
  `build/three.core.min.js`, unmodified, so the page works offline like the rest of the dashboard. It uses
  WebGL 2 only: no WebGPU, shadows or post-processing.

## Limits

- Straight road only: lanes are drawn straight along `road.heading_deg`, as this world's carriageway is.
- The view runs about 0.2 s behind the stream (the easing); the stream itself is the 200 ms cycle.
- A car's direction comes from its velocity, so a car RoadSense sees as standing still faces along the road.
- One simulation at a time, as for the stream itself.
