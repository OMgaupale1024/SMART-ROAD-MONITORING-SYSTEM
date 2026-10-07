# RoadSense

Software for the **RoadSense car-in-a-box** road-condition monitoring prototype. It
connects directly to an **Arduino UNO over USB serial**, shows live sensor data and the
current road condition, records sessions locally, and exports data to CSV. Two front-ends
share the same protocol parser and SQLite storage:

- **Desktop app** (PySide6) — `python -m roadsense`
- **Web dashboard** (FastAPI, in the browser on the same PC) — `python -m roadsense.web`

> Local-first prototype (v0.2.1). Everything here runs on a PC wired to the Arduino by USB,
> with no internet connection needed.

## Status

**Implemented:**

- Arduino UNO firmware: MPU6050 accelerometer, HC-SR04 ultrasonic distance, SSD1306 OLED,
  status LED and buzzer; road conditions classified on the Arduino.
- Serial protocol v1 (newline-terminated CSV at 115200 baud) with a strict parser.
- Desktop app (PySide6) with live plots, session recording and event history.
- Local web dashboard (FastAPI): REST API, live WebSocket telemetry, recording, session
  history; starts with **no telemetry source** until you pick the Arduino or the simulator.
  Works offline: its scripts, styles and icons ship with RoadSense (since v0.2.1).
- Built-in simulators (clearly labelled synthetic in both front-ends).
- SQLite session recording; CSV export (desktop) and a ZIP of the same CSVs (web).

**Planned, not implemented:** GPS/location, maps, mobile app, Bluetooth or wireless links,
cloud sync, Raspberry Pi integration, camera/computer vision, ML/AI classification or
prediction, safer-route suggestions.

---

## What the desktop app does

- Lists available COM ports and connects to the Arduino (default **115200** baud).
- Receives live telemetry and shows **AY**, **shock**, **road distance** (cm or `NA`),
  current **status** (NORMAL / SPEED BREAKER / POTHOLE), connection health, packet counts,
  and live rolling plots of shock and distance.
- Records sessions and detected events to a local **SQLite** database.
- Reviews events in a filterable, sortable history table with a detail panel.
- Exports a session to `telemetry.csv`, `events.csv`, and `session_metadata.csv`.
- Never freezes: serial reading runs on a background thread and talks to the UI via signals.
- Treats serial input as unreliable — malformed lines are rejected, counted, and logged,
  never crashing the reader.

The Arduino is the authority for classifying road conditions; the app displays and stores
them. Ultrasonic `NA` (timeout) is preserved honestly everywhere — no value is ever faked.

## What the web dashboard does

- Runs at `http://127.0.0.1:8000` (`python -m roadsense.web` opens your browser).
- Starts with **no telemetry source**. You choose one, and only one runs at a time:
  - **Arduino:** *Hardware COM & Terminal* tab → pick the port → **Connect Hardware**.
  - **Simulator:** **Start Simulator** on that tab, or a *Smooth / Bumpy / Highway* profile
    on the dashboard. **Stop Simulator** returns to no source.
- The header pill always says which source is live: *NO SOURCE*, *ARDUINO: \<port\>*, or
  *SIMULATOR ACTIVE* (amber, with simulated readings labelled synthetic — never "Arduino").
- Live telemetry over a WebSocket: gauges, shock and distance charts, a road visualizer,
  the hazard event log and the raw serial lines.
- Records sessions to the same SQLite database as the desktop app; lists past sessions and
  downloads each as a ZIP of `telemetry.csv`, `events.csv`, `session_metadata.csv`.
- Works without internet access: Chart.js and the Font Awesome icons are bundled in
  `web/static/vendor/` (with their licenses) and text uses system fonts, so the page only
  ever talks to the local RoadSense server.
- A browser tab that falls behind (e.g. a busy laptop) skips stale telemetry and catches up
  to the newest reading; status changes still reach it. It never slows the Arduino or
  simulator stream, recording, or other tabs.
- REST API documentation at `http://127.0.0.1:8000/docs`.

---

## Requirements

- **Python 3.10+** (developed on 3.10 / Windows; also tested on 3.12 / macOS).
- Windows 10/11 (the app is Windows-first but not deliberately Windows-only).
- An Arduino UNO running the firmware in `firmware/roadsense_arduino.ino` (optional — a
  built-in simulator lets you run either front-end with no hardware).

## Install & run

`pyproject.toml` is the single source of dependencies. Pick the extras you need:
`desktop` (PySide6, pyqtgraph), `web` (FastAPI, Uvicorn, websockets), `dev` (pytest, httpx2).

Windows / PowerShell:

```powershell
cd W:\PROJECTS\ROADSENSE

# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install RoadSense with both front-ends (editable install)
pip install -e ".[desktop,web]"

# 3. Run one of them
python -m roadsense        # desktop app (or just:  roadsense)
python -m roadsense.web    # web dashboard at http://127.0.0.1:8000
```

macOS / Linux:

```bash
python3.12 -m venv .venv    # any Python 3.10+; macOS's built-in python3 may be older
source .venv/bin/activate
pip install -e ".[desktop,web]"
python -m roadsense        # desktop app
python -m roadsense.web    # web dashboard
```

> The editable install also puts the `roadsense` package on the path, so both
> `python -m ...` commands work from any directory.

### First run with no hardware

- **Desktop:** opens and works without an Arduino. To see it live, enable
  **Tools → Developer Simulation Mode** (off by default; a loud banner appears while it is on
  and the data is clearly labelled synthetic). Turn it off to return to real serial mode.
- **Web dashboard:** opens with *NO SOURCE* and no data. Click **Start Simulator** (or a
  simulation profile) for synthetic data, shown as *SIMULATOR ACTIVE*; connect the Arduino's
  port instead for real serial data.

---

## Using it with the Arduino (desktop app)

1. Plug the Arduino UNO into the PC by USB.
2. Open RoadSense Desktop.
3. In the **Connection** panel, click **Refresh**, pick the Arduino's COM port, keep baud at
   **115200**, and click **Connect**.
4. Watch the **Live Dashboard** update. The raw serial feed (last 20 lines) is shown in the
   Connection panel for troubleshooting.
5. Fill in a session name (a timestamped default is provided) and click **Start Recording**.
   Live data still displays when not recording — recording only controls persistence.
6. Review events under the **Event History** tab; export with **Export Session…**.
7. Click **Disconnect** (or just close the app) to stop safely.

## Uploading the firmware

1. Open `firmware/roadsense_arduino.ino` in the Arduino IDE.
2. Install libraries via **Tools → Manage Libraries…**: **Adafruit GFX Library** and
   **Adafruit SSD1306**.
3. Select **Board: Arduino UNO** and the correct **Port**.
4. Click **Upload**.

Wiring: MPU6050 on I2C (`0x68`), SSD1306 OLED on I2C (`0x3C`), HC-SR04 `TRIG=D9` `ECHO=D10`,
status LED on `D6`, buzzer on `D7`.

> **Close the Arduino IDE Serial Monitor before connecting from RoadSense** (desktop or web) —
> a COM port can only be open in one program at a time (otherwise you'll get an "access
> denied" error, which the app reports clearly).

### Calibration

The shock thresholds in the firmware (`SHOCK_SPEED_BREAKER`, `SHOCK_POTHOLE`) are
**placeholders** and must be tuned against real readings for your chassis and sensor
mounting. They are clearly marked at the top of the sketch. Classification lives entirely on
the Arduino by design.

---

## Serial protocol (summary)

Newline-terminated CSV at 115200 baud, Arduino → PC. Full spec: [`docs/serial-protocol.md`](docs/serial-protocol.md).

```
HELLO,ROADSENSE,1                                  # startup handshake
T,<ms>,<ay>,<shock>,<distance_cm|NA>,<status>      # telemetry, ~10 Hz
E,<ms>,<ay>,<shock>,<distance_cm|NA>,<status>      # a classified event
# status ∈ { NORMAL, SPEED_BREAKER, POTHOLE }
```

---

## Where data is stored

The SQLite database lives in your OS application-data directory, **not** beside the source:

- Windows: `%LOCALAPPDATA%\RoadSense\roadsense.db`
- macOS: `~/Library/Application Support/RoadSense/roadsense.db`
- Linux: `~/.local/share/RoadSense/roadsense.db`

Override with the `ROADSENSE_DATA_DIR` environment variable if needed.

---

## Testing

Automated tests need no GUI or hardware. They cover the protocol parser, storage, session
recording and CSV export, plus the web server: REST endpoints, the WebSocket stream, the
startup/shutdown lifecycle, source switching (including a pseudo-terminal standing in for
the Arduino on macOS/Linux), and concurrency regression tests for recording while packets
arrive. The web tests are skipped unless the `web` and `dev` extras are installed.

```powershell
pip install -e ".[desktop,web,dev]"
python -m pytest
```

### Testing the real serial path without an Arduino

The app's Simulation Mode bypasses pyserial. To exercise the actual serial read path, pair
two virtual COM ports (Windows: **com0com**; Linux/macOS: **socat**) and use the writer tool:

```powershell
python tools\serial_sim.py COM20        # writes the protocol to one end
# then Connect the app to the paired port (e.g. COM21) at 115200
```

---

## Project structure

```
firmware/roadsense_arduino.ino   Arduino firmware (sensors + OLED/LED/buzzer + serial)
src/roadsense/
  app.py            entry point (python -m roadsense)
  models.py         domain data types (no Qt/serial/SQL)
  protocol.py       serial line parser (reliability core)
  serial_client.py  background QThread serial reader
  simulator.py      Developer Simulation Mode source
  database.py       SQLite connection + schema
  repositories.py   data access (sessions / telemetry / events)
  session_service.py recording logic (buffer + persist)
  export_service.py CSV export
  ui/               PySide6 widgets (dashboard, panels, history, styles)
  web/              web dashboard (python -m roadsense.web)
    server.py       FastAPI app: REST API, /ws/telemetry WebSocket, static UI
    service.py      serial/simulator reader, recording, ZIP export
    static/         HTML/CSS/JS front-end; vendor/ holds bundled Chart.js + icons
tools/serial_sim.py test-only serial writer (virtual COM pair)
tests/              pytest suite
docs/serial-protocol.md
```

Separation is intentional: UI ⟂ serial I/O ⟂ parsing ⟂ storage ⟂ business logic ⟂ export.

### Architecture

Both front-ends share the parser (`protocol.py`), the SQLite layer (`database.py`,
`repositories.py`), the recording logic (`session_service.py`) and the CSV export.

- **Desktop:** the serial reader runs on a background `QThread` and hands packets to the UI
  thread through Qt signals (Simulation Mode emits the same signals from a UI-thread timer).
  All database work happens on the UI thread.
- **Web:** `server.py` is the FastAPI app (UI, REST API, `/ws/telemetry`).
  `WebTelemetryManager` in `service.py` runs at most one source thread (serial or simulator)
  and records through `SessionService`. One lock guards the database and recording state;
  a second lock only serializes source changes. Packets and status changes are pushed to the
  browser over the WebSocket through a bounded queue per client; when a client's queue is
  full its stale packets are dropped (counted in `/api/status` as `ws_dropped_messages`)
  while status messages are kept, and the source thread never waits on a client.

## Limitations

- No GPS, speed, battery, or map data — that hardware does not exist in this version.
- Road-condition classification is done on the Arduino; the app does not re-classify.
- Firmware shock thresholds are un-calibrated placeholders (see Calibration above).
- The desktop app and the web dashboard are separate programs. A serial port can be open in
  only one of them at a time; both use the same SQLite file by default.
- The API documentation page (`/docs`) is FastAPI's Swagger UI, which loads from a CDN, so
  it needs internet access. The dashboard itself does not.
- The web API has no authentication and listens on `127.0.0.1` only — local use only.
