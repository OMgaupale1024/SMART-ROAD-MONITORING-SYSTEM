# RoadSense 2.0

Smart road monitoring and edge-AI driving assistance: a Webots digital twin, a Python edge
processor, and one live dashboard. Current development version: `2.0.0.dev0`.

```text
Mac: Webots + SUMO → raw sensor frames → Raspberry Pi: autonomy pipeline → browser dashboard
                                               └→ Ollama: optional explanations
```

The deterministic pipeline tracks vehicles, predicts conflicts, maps simulated potholes,
and recommends a speed or maneuver. **Recommendations are advisory research outputs, not
validated commands for a real vehicle.** They do not control the Webots driver. Ollama
explains decisions in a separate worker; safety processing does not wait for inference.

## Current implementation

- One dashboard at `/`: Three.js digital twin, Next Move, AI explanation, telemetry,
  hazards, processing and host measurements, and recent events. `/autonomy` redirects here.
- `POST /api/sensor-frame` validates raw Webots observations and executes the shared
  autonomy pipeline on the receiving server. Its output has `simulation.processing = "edge"`.
- `GET /api/live-state` and `WS /ws/live` deliver the latest processed snapshot.
- `GET /api/ai-explanation` supplies deterministic text, or optional local Ollama text with
  source run/sequence metadata and freshness. Urgent decisions always use deterministic text.
- `GET /api/system` separates pipeline timing, handler timing, host diagnostics and inference
  latency. Unavailable host readings are reported as unavailable.
- `GET /api/hazards` exports deduplicated, run-local `roadsense.hazard.v1` records.
  Positions are Webots coordinates; geographic coordinates are null.
- Firmware, serial drivers, protocol and storage code are preserved in
  [legacy/arduino](legacy/arduino/README.md). The old Arduino web UI and APIs are retired.

Persistent hazard storage, GNSS georeferencing, synchronization, Google Maps overlays and a
trained decision model are **not implemented**. Historical Pi benchmarks are not measurements
of this working tree. See [validation status](docs/validation.md) for the latest local checks.

## Run on the Mac

Use Python 3.12 here; macOS's default Python 3.9 is too old for the web application.
The supported package minimum is Python 3.10. Node 22.7+ is only needed for JavaScript tests;
the frontend has no build step. Webots and SUMO are needed for the simulation.

From the repository root, using the existing dependency directory:

```sh
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python312" \
python3.12 -m roadsense.web --no-browser
```

Open `http://127.0.0.1:8000/` in **Vivaldi** (`/Applications/Vivaldi.app`). Before a producer connects, the dashboard correctly says
**NO PRODUCER**. It does not generate synthetic readings by itself.

For a fresh setup, install into the user-owned dependency directory (no activation or changes
to Homebrew's managed installation):

```sh
python3.12 -m pip install --target "$HOME/.local/share/roadsense/python312" '.[web,dev]'
```

`pyproject.toml` defines dependencies. Keep `src` first on `PYTHONPATH` so the checkout takes
precedence over a previously installed package. The optional `vision` extra is only for
`tools/bench_detector.py`; it is not needed by the dashboard or sensor pipeline.

Launch Webots from a second terminal:

```sh
export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/share/sumo"
export PATH="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/bin:$PATH"
export WEBOTS_EXTRA_PROJECT_PATH="/Applications/Webots.app/Contents/projects/vehicles"
export ROADSENSE_EDGE_URL=http://127.0.0.1:8000/api/sensor-frame
/Applications/Webots.app/Contents/MacOS/webots simulation/webots/worlds/roadsense_highway.wbt
```

For Pi processing, replace the URL with `http://PI_IP:8001/api/sensor-frame` and open
`http://PI_IP:8001/`. Verify the current Pi address first. A standalone compatibility mode
(`ROADSENSE_LIVE_PUBLISH=1`, with `ROADSENSE_EDGE_URL` unset) processes in Webots and posts
precomputed snapshots. That mode is not evidence of Pi computation.

## Run on the Raspberry Pi

The Pi deployment is a separate directory, `/home/omgaupale/roadsense-edge`. Editing the Mac
checkout does not update it. Follow [deployment and rollback](docs/deployment.md) before
copying anything; remote deployment requires explicit authorization.

On the Pi, with its dependencies already installed:

```sh
cd ~/roadsense-edge
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python313" \
python3 -m roadsense.web --host 0.0.0.0 --port 8001 --no-browser
```

To enable explanations from an already installed `qwen3:0.6b` model, add
`ROADSENSE_AI_EXPLAIN=1` to the server environment. Ollama should listen at
`127.0.0.1:11434`. Without it, deterministic recommendations and explanations still work.
See [AI explanations](docs/ai-explanations.md) for configuration and failure behavior.

## Tests

With the `dev` dependencies in the user-owned directory:

```sh
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python312" python3.12 -m pytest -q
node --test tests/js/
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python312" python3.12 -m pytest legacy/arduino/tests -q
```

On this Mac, `venv/bin/python -m pytest -q` is also usable without activating that existing
environment. It contains the test dependencies; the current user-owned directory contains
runtime dependencies but may not contain pytest. Archived tests also need pyserial.
Socket integration tests require permission to bind local loopback ports.

For browser regression, visual verification and responsive testing, use Vivaldi:

```sh
PYTHONPATH=src venv/bin/python tests/browser_smoke.py
# Show the same checks in a visible, isolated Vivaldi window:
PYTHONPATH=src venv/bin/python tests/browser_smoke.py --headed
```

The executable defaults to `/Applications/Vivaldi.app/Contents/MacOS/Vivaldi`;
`--browser` can point to another Vivaldi installation. Each run uses a temporary
profile, disables extensions and uses `--password-store=basic` and
`--use-mock-keychain` to avoid credential access/Keychain prompts. The test closes
its browser and server and removes the profile on exit, including test failures.
Screenshots and results remain in `/tmp/roadsense-browser`. Never use a personal
profile for testing. Configure other Chromium-compatible automation/debugging
tools with the same executable and isolation flags; report incompatibility instead
of installing or switching to Chrome for Testing. See [agent browser policy](AGENTS.md).
The current Vivaldi build launches but times out during debugger initialization in
this runner; visual/responsive verification remains unconfirmed until that
compatibility issue is resolved. See [validation details](docs/validation.md).

## Guides

- [Architecture and safety boundaries](docs/architecture.md)
- [Unified dashboard](docs/dashboard.md)
- [Sensor ingestion and live state](docs/live-state.md)
- [AI explanations](docs/ai-explanations.md)
- [Edge metrics](docs/edge-metrics.md)
- [Hazard records and future persistence](docs/hazard-records.md)
- [Deployment and rollback](docs/deployment.md)
- [Webots world, sensors and simulation](simulation/webots/README.md)
- [Local validation and deployment status](docs/validation.md)
- [Archived Arduino hardware/software](legacy/arduino/README.md)

## Operational limits

Run one server process and one simulation producer: live state, tracking, hazards and metrics
are in memory. Restarting loses them. The current geometry and sensor adapter are specific
to the straight Webots highway, not a general road model.

The API has no authentication or TLS. Binding to `0.0.0.0` lets reachable machines read data
and submit frames; use a trusted LAN. The dashboard and Three.js are served locally and work
offline. `/openapi.json` describes the API; CDN-dependent Swagger and ReDoc pages are disabled.

Ollama text is filtered with rules, not formally proven correct. The deterministic safety
recommendation stays authoritative and visible. A Pi with 4 GB RAM still needs measurement
under simultaneous simulation traffic and inference before this version's performance can
be claimed.
