# RoadSense

Software for the **RoadSense car-in-a-box** road-condition monitoring prototype. It
connects directly to an **Arduino UNO over USB serial**, shows live sensor data and the
current road condition, records sessions locally, and exports data to CSV. Two front-ends
share the same protocol parser and SQLite storage:

- **Desktop app** (PySide6) — `python -m roadsense`
- **Web dashboard** (FastAPI, in the browser on the same PC) — `python -m roadsense.web`

> Local-only prototype (v0.1.0). Everything here runs on a PC wired to the Arduino by USB.

## Status

**Implemented:** Arduino firmware (MPU6050, HC-SR04, SSD1306 OLED, LED, buzzer); serial
protocol v1 at 115200 baud; desktop app; local web dashboard; built-in simulator; SQLite
session recording; CSV export (ZIP of the same CSVs from the web dashboard).

**Not implemented (possible later phases):** GPS/location, maps, mobile app, Bluetooth or
wireless links, cloud sync, Raspberry Pi integration, camera/computer vision, ML-based
classification.

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

---

## Requirements

- **Python 3.10+** (developed on 3.10 / Windows; also tested on 3.12 / macOS).
- Windows 10/11 (the app is Windows-first but not deliberately Windows-only).
- An Arduino UNO running the firmware in `firmware/roadsense_arduino.ino` (optional — a
  built-in simulator lets you run either front-end with no hardware).

## Install & run

`pyproject.toml` is the single source of dependencies. Pick the extras you need:
`desktop` (PySide6, pyqtgraph), `web` (FastAPI, Uvicorn, websockets), `dev` (pytest).

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
- **Web dashboard:** opens your browser and currently **starts its built-in simulator
  automatically** (synthetic data, shown as *SIMULATOR ACTIVE*). Pick the Arduino's port in
  the dashboard to switch to real serial data. API docs: `http://127.0.0.1:8000/docs`.

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

> **Close the Arduino IDE Serial Monitor before connecting from RoadSense Desktop** — a COM
> port can only be open in one program at a time (otherwise you'll get an "access denied"
> error, which the app reports clearly).

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

Automated tests cover the protocol parser, storage, session recording, and CSV export
(no GUI or hardware needed; the web test is skipped unless the `web` extra is installed):

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
    static/         HTML/CSS/JS front-end
tools/serial_sim.py test-only serial writer (virtual COM pair)
tests/              pytest suite
docs/serial-protocol.md
```

Separation is intentional: UI ⟂ serial I/O ⟂ parsing ⟂ storage ⟂ business logic ⟂ export.

## Limitations

- No GPS, speed, battery, or map data — that hardware does not exist in this version.
- Road-condition classification is done on the Arduino; the app does not re-classify.
- Firmware shock thresholds are un-calibrated placeholders (see Calibration above).
- The web dashboard loads fonts, icons, and Chart.js from public CDNs, so it needs an
  internet connection to render fully.
- The web API has no authentication and listens on `127.0.0.1` only — local use only.
