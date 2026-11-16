# RoadSense 2.0 architecture

RoadSense is one edge server with one dashboard. A Webots simulation sends it raw sensor frames; the
server runs the autonomy pipeline, publishes the result to browsers, measures itself, and (optionally)
asks a local language model to word the safety decision.

## Components

```
MacBook                                         Raspberry Pi 5 (or the same Mac, for development)
┌───────────────────────────────┐               ┌────────────────────────────────────────────────────────┐
│ Webots + SUMO                 │               │ roadsense.web  (one FastAPI process, one event loop)   │
│  roadsense_ego.py controller  │  sensor frame │                                                        │
│  (Python 3.9, stdlib only)    │──────────────▶│  POST /api/sensor-frame                                │
│  LivePublisher thread:        │  5 per sim s  │   edge.SensorFrame        validate (pydantic, strict)  │
│   newest frame only, backoff  │  HTTP, LAN    │   edge.EdgeRun            one run's state              │
└───────────────────────────────┘               │   autonomy.Pipeline       track, predict, risk,        │
                                                │                           hazard map, safety decision  │
┌───────────────────────────────┐               │   live.LiveStateHub       latest state, fan-out        │
│ Browser: the dashboard        │   WS /ws/live │   metrics.EdgeMetrics     stage timings, counters      │
│  live.js   stream + status    │◀──────────────│                                                        │
│  scene.js  3D twin (three.js) │               │  ai_explainer.AIExplainer  one worker thread ──────────┼──▶ Ollama
│  system.js diagnostics, AI    │  GET, 1 per s │  metrics.Host              /proc, /sys, vcgencmd       │    127.0.0.1:11434
│  charts.js small charts       │◀──────────────│  GET /api/system  /api/ai-explanation  /api/hazards    │    qwen3:0.6b
└───────────────────────────────┘               └────────────────────────────────────────────────────────┘
```

| Package | Role | Constraints |
| ------- | ---- | ----------- |
| `roadsense.autonomy` | perception, tracking, prediction, collision risk, hazard map, safety decision, live-state snapshot, the simulation's publisher | standard library only, Python 3.9 compatible: the Webots controller imports it straight from `src/` |
| `roadsense.web` | the edge server: ingestion, publication, metrics, explanations, the dashboard's files | FastAPI, uvicorn, websockets, pydantic; nothing else (no database, no serial port, no model client library) |
| `simulation/webots` | the world, the SUMO traffic, the ego car's controller | Webots R2025b, SUMO 1.27 |
| `legacy/arduino` | the archived Arduino stack | not imported by anything above |

## Data flow of one sensor frame

1. The controller builds a `roadsense.sensors.v1` frame each 200 ms of simulation time and hands it to
   its publisher thread, which POSTs the newest one (a frame that could not be sent in time is skipped,
   and shows up on the server as a missed sequence number).
2. `POST /api/sensor-frame` reads the body (**read**), validates it and checks run and sequence
   (**validate**), runs `Pipeline.process()` (**process**), builds and encodes the `roadsense.live.v1`
   snapshot and offers it to every WebSocket client's one-slot queue (**publish**). Each stage is timed
   with `time.perf_counter()`; see [edge-metrics.md](edge-metrics.md).
3. The hub then tells its listeners. Today that is `AIExplainer.observe()`, which compares the decision
   with the previous frame's and returns.
4. The browser receives the snapshot over `/ws/live`, draws it, and asks `/api/system` and
   `/api/ai-explanation` once a second on its own schedule (at once when the decision changes).

A producer may also POST a snapshot it computed itself to `/api/live-state` (standalone Webots,
`ROADSENSE_LIVE_PUBLISH=1`). The dashboard then says the decision was made in Webots, and the edge
panel has no processing time to show.

## Routes

| Route | Method | Changes state | Purpose |
| ----- | ------ | ------------- | ------- |
| `/api/sensor-frame` | POST | yes | a sensor frame to process ([live-state.md](live-state.md)) |
| `/api/live-state` | POST | yes | a precomputed snapshot |
| `/api/live-state` | GET | no | the latest snapshot |
| `/ws/live` | WebSocket | no | every snapshot; clients send nothing |
| `/api/system` | GET | no | edge and host measurements ([edge-metrics.md](edge-metrics.md)) |
| `/api/ai-explanation` | GET | no | the explanation of the latest decision ([ai-explanations.md](ai-explanations.md)) |
| `/api/hazards` | GET | no | the hazard map as records ([hazard-records.md](hazard-records.md)) |
| `/` | GET | no | the dashboard ([dashboard.md](dashboard.md)); `/autonomy`, its old address, redirects here |
| `/static/…` | GET | no | the dashboard's files, all served locally |
| `/openapi.json` | GET | no | the route list (Swagger UI and ReDoc are off: they load from a CDN) |

Nothing else exists: the Arduino dashboard's REST routes and `/ws/telemetry` were removed with it.

## Safety boundaries

- **The safety processor is the only source of decisions.** `roadsense.autonomy.safety.decide()` produces
  the action, risk, target speed, lane and reason. Nothing downstream changes them.
- **The frontend decides nothing.** It draws the snapshot. The only thresholds it shows (the TTC chart's
  reference lines) come from `/api/system`, which reads them from the safety processor's own constants.
- **The model only words a decision.** It is called from one background thread, never from the
  sensor-frame path. It receives a prompt and no tools, and its answer is shown as text only. An answer
  that fails validation, arrives after a timeout, or arrives for a run that is over is replaced by a
  deterministic sentence. HIGH and CRITICAL risk never reach the model.
- **Recommendations are advisory and for the simulation.** They do not feed the Webots driver, and no
  route can actuate anything. The dashboard says so next to every recommendation.
- **Old data is never shown as live.** The stream has six states (LIVE, CONNECTING, STALE,
  DISCONNECTED, NO PRODUCER, SIMULATION ENDED); only LIVE draws at full strength. An explanation is
  marked CURRENT only while the stream is live, the server calls it current, and it is about the
  decision on screen.
- **Malformed input changes nothing.** A frame that fails validation, repeats a sequence number, goes
  back in time or follows its run's end is refused (400/409/413) and counted; the latest state stays.

## Threads and what may block

| Thread | Does | May block on |
| ------ | ---- | ------------ |
| event loop | sensor frames, snapshots, WebSockets | nothing: the pipeline is pure computation (well under a millisecond on a laptop; measured live, see the dashboard) |
| worker pool (FastAPI) | `GET /api/system`, `/api/ai-explanation`, `/api/hazards`, static files | file reads in `/proc` and `/sys`; `vcgencmd` at most every 5 s |
| `RoadSenseAIExplainer` | one request to Ollama at a time | the model, up to the timeout; cancelled when the decision turns urgent, a new run starts or the server stops |

The explainer and frame metrics use locks that are never held across I/O; the sensor-frame path
takes each for a few field updates. Host sampling has its own lock in the worker pool.

## Memory on a 4 GB Raspberry Pi

| Item | Bound |
| ---- | ----- |
| latest snapshot | one, about 20 kB |
| stage timings | 1,500 frames per run (5 minutes), about 150 kB |
| WebSocket queues | one snapshot per client |
| explanations | 64 kinds of decision, a sentence each; one request waiting, one in flight |
| charts | in the browser, 60 s of history |
| Ollama | one model (`qwen3:0.6b`, about 1 GB loaded), unloaded after `keep_alive` (2 minutes) without requests |

The server keeps no database and writes nothing to disk.

## Security

There is no login and no encryption: serve RoadSense to a trusted local network only. The server listens
on `127.0.0.1` unless started with `--host 0.0.0.0`. Ollama stays bound to localhost on the Pi; the
browser never talks to it, only the server does. The page embeds no secrets and loads nothing from the
internet.
