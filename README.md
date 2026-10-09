# RoadSense

Software for the **RoadSense car-in-a-box** road-condition monitoring prototype. It
connects directly to an **Arduino UNO over USB serial**, shows live sensor data and the
current road condition, records sessions locally, and exports data to CSV. Two front-ends
share the same protocol parser and SQLite storage:

- **Desktop app** (PySide6) — `python -m roadsense`
- **Web dashboard** (FastAPI, in a browser) — `python -m roadsense.web`, on the PC wired to
  the Arduino, or headless on a **Raspberry Pi** in the vehicle that serves the dashboard to
  phones and laptops on the local network

> Local-first prototype. Latest release: v0.2.1. `main` adds the Raspberry Pi edge gateway
> described below, which becomes v0.3.0 once it has been validated on a real Raspberry Pi.
> Nothing needs an internet connection at runtime.

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
- Raspberry Pi edge gateway (on `main`, awaiting hardware validation): the web runtime runs
  headless on ARM64 Linux without the desktop extra, can serve the dashboard to the local
  network, accepts a serial port as the Arduino only after its handshake, reports an
  unplugged Arduino instead of claiming it is still connected, and stops cleanly on Ctrl+C or
  SIGTERM; an optional systemd unit starts it at boot.

**Planned, not implemented:** GPS/location, geospatial road events, maps, camera and
computer vision, sensor fusion, road-risk prediction, safer routing, ML/AI classification,
mobile app, Bluetooth or wireless links to the Arduino, cloud sync.

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

- Runs at `http://127.0.0.1:8000` and opens your browser. Options:
  `--host` (default `127.0.0.1`, this computer only; `0.0.0.0` lets every device on the
  network in, see [Raspberry Pi edge gateway](#raspberry-pi-edge-gateway)), `--port`
  (default `8000`), `--no-browser`.
- Starts with **no telemetry source**. You choose one, and only one runs at a time:
  - **Arduino:** *Hardware COM & Terminal* tab → pick the port → **Connect Hardware**. The
    port counts as the Arduino only once the device identifies itself (its
    `HELLO,ROADSENSE,1` handshake, or valid telemetry) within 5 s; any other device is
    disconnected again with an error saying why. If the Arduino is unplugged, the source
    returns to *NO SOURCE* and the reason appears in the terminal panel.
  - **Simulator:** **Start Simulator** on that tab, or a *Smooth / Bumpy / Highway* profile
    on the dashboard. **Stop Simulator** returns to no source.
- The header pill always says which source is live: *NO SOURCE*, *ARDUINO: \<port\>*, or
  *SIMULATOR ACTIVE* (amber, with simulated readings labelled synthetic — never "Arduino").
- Live telemetry over a WebSocket: gauges, shock and distance charts, a road visualizer,
  the hazard event log and the raw serial lines.
- Records sessions to the same SQLite database as the desktop app; lists past sessions and
  downloads each as a ZIP of `telemetry.csv`, `events.csv`, `session_metadata.csv`.
- Works without internet access: Chart.js, three.js and the Font Awesome icons are bundled in
  `web/static/vendor/` (with their licenses) and text uses system fonts, so the page only
  ever talks to the local RoadSense server.
- A browser tab that falls behind (e.g. a busy laptop) skips stale telemetry and catches up
  to the newest reading; status changes still reach it. It never slows the Arduino or
  simulator stream, recording, or other tabs.
- REST API documentation at `http://127.0.0.1:8000/docs`.
- Relays the Webots digital twin's live RoadSense state: the simulation POSTs one snapshot per
  processing cycle to `/api/live-state`; `GET /api/live-state` returns the latest and the
  `/ws/live` WebSocket streams them to any number of clients. See
  [docs/live-state.md](docs/live-state.md).
- **Autonomy 3D** (`/autonomy`, also in the tab bar): that live state drawn in 3D around the
  digital twin's car: the lanes, the tracked vehicles with their predicted paths, TTC and risk,
  the mapped potholes, and RoadSense's safety decision, primary threat and recent events. It
  draws what RoadSense sends and decides nothing itself. See
  [docs/autonomy-dashboard.md](docs/autonomy-dashboard.md).

---

## Requirements

- **Python 3.10+** (developed on 3.10 / Windows; also tested on 3.12 / macOS).
- Windows 10/11 (the app is Windows-first but not deliberately Windows-only). The web
  dashboard also runs on Linux, including 64-bit Raspberry Pi OS (see
  [Raspberry Pi edge gateway](#raspberry-pi-edge-gateway)).
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

## Raspberry Pi edge gateway

A Raspberry Pi can take the PC's place in the vehicle. The Arduino plugs into the Pi by USB;
the Pi runs the web dashboard headless (no monitor, no desktop app, no internet) and records
sessions to its own SQLite database; a laptop or phone on the same network opens the
dashboard in a browser.

```
Arduino UNO ──USB serial──► Raspberry Pi: python -m roadsense.web ──Wi-Fi / LAN──► browser
```

> **Not yet validated on Raspberry Pi hardware.** This setup is tested on macOS, with
> pseudo-terminals standing in for the Arduino; it is released as v0.3.0 once it has run on
> a real Pi with the Arduino.

**1. Install** (once; this step needs internet). Use Raspberry Pi OS (64-bit) or another
ARM64 Linux with Python 3.10 or newer (`python3 --version`; Raspberry Pi OS Bookworm ships
3.11). Install only the `web` extra: the desktop extra (PySide6, pyqtgraph) is neither needed
nor installed, and every web dependency has a prebuilt ARM64 wheel, so nothing is compiled.

```bash
sudo apt install git python3-venv   # only if they are missing (administrator setup)
git clone https://github.com/OMgaupale1024/SMART-ROAD-MONITORING-SYSTEM.git roadsense
cd roadsense
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[web]"             # ".[web,dev]" to also run the test suite
```

**2. Let your user open the Arduino's serial port.** On Linux a serial port belongs to a
group, and only its members (and root) may open it:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB*    # e.g.  crw-rw---- 1 root dialout ... /dev/ttyACM0
groups                             # is that group listed (dialout on Raspberry Pi OS)?
sudo usermod -aG dialout "$USER"   # if not (administrator setup), then log out and back in
```

The first user that Raspberry Pi OS creates is normally in `dialout` already. Don't run
RoadSense with `sudo` or `chmod` the device; without the group RoadSense reports
*Permission denied* and names the fix.

**3. Connect the Arduino and find its port.** Plug the UNO into the Pi by USB. A genuine UNO
appears as `/dev/ttyACM0`, most CH340-based clones as `/dev/ttyUSB0`:

```bash
python -m serial.tools.list_ports -v
```

Other entries such as `/dev/ttyAMA0` are the Pi's own serial ports, not the Arduino.

**4. Start RoadSense for the local network:**

```bash
python -m roadsense.web --host 0.0.0.0 --no-browser
```

`--host 0.0.0.0` listens on every network interface so other devices can connect (the default
`127.0.0.1` accepts only the Pi itself); `--no-browser` skips opening a browser; `--port`
changes the default port 8000.

> **Security:** with `--host 0.0.0.0`, anyone who can reach the Pi over the network can see
> the dashboard and control RoadSense: switch the telemetry source, start and stop
> recordings, download sessions. There is no login in this version. Use a network you trust,
> such as the vehicle's own Wi-Fi hotspot, never a public one.

**5. Open the dashboard from another device.** Find the Pi's address with `hostname -I` on
the Pi (e.g. `192.168.1.42`) and open `http://192.168.1.42:8000` on a laptop or phone on the
same network (`http://<hostname>.local:8000` also works where mDNS is available). The page,
its scripts and icons, and the live WebSocket all come from the Pi, so the network needs no
internet access. Then, on the *Hardware COM & Terminal* tab: **Refresh** → pick
`/dev/ttyACM0` → **Connect Hardware**.

- The UNO restarts when its port opens and identifies itself about 2 s later with
  `HELLO,ROADSENSE,1`; only then is the source *ARDUINO*. A device that sends no RoadSense
  handshake or telemetry within 5 s, or announces another protocol version, is disconnected
  with an error that says why.
- If the Arduino is unplugged, the source returns to *NO SOURCE*, with the reason in the
  terminal panel and in `/api/status` (`"connection_status": "Connection lost"`,
  `"last_error"`). A recording in progress stays open. RoadSense does not reconnect on its
  own: plug the Arduino back in and click **Connect Hardware** again.

**6. Stop RoadSense** with **Ctrl+C**, or SIGTERM (what `systemctl stop` sends). Either one
stops the source, saves telemetry still buffered for a recording, ends that recording and
closes the database before the process exits. The database lives in the home directory of
the user running RoadSense (see [Where data is stored](#where-data-is-stored)); nothing
needs root.

**7. Start RoadSense at boot (optional).** `deploy/roadsense.service` is an example systemd
unit: it runs the command from step 4 as your (non-root) user, and restarts RoadSense only if
it crashes. Nothing installs it automatically; to install it by hand:

```bash
sudo cp deploy/roadsense.service /etc/systemd/system/roadsense.service
sudo nano /etc/systemd/system/roadsense.service   # replace <user> and <roadsense>
sudo systemctl daemon-reload
sudo systemctl enable --now roadsense             # start now and at every boot
journalctl -u roadsense -f                        # follow its log
sudo systemctl disable --now roadsense            # stop it and take it out of the boot
```

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
- Linux (including Raspberry Pi): `~/.local/share/RoadSense/roadsense.db`, or
  `$XDG_DATA_HOME/RoadSense/` when that is set

Override with the `ROADSENSE_DATA_DIR` environment variable if needed. RoadSense creates the
directory on first use; the user running it only needs write access there, never root.

---

## Testing

Automated tests need no GUI or hardware. They cover the protocol parser, storage, session
recording and CSV export, plus the web server: REST endpoints, the WebSocket stream, the
startup/shutdown lifecycle (including a real server process stopped with SIGINT and
SIGTERM), the command-line options, the serial handshake, unplugging and every source
switch (a pseudo-terminal stands in for the Arduino on macOS/Linux), a check that the web
runtime never imports PySide6, and concurrency regression tests for recording while packets
arrive. The web tests are skipped unless the `web` and `dev` extras are installed. The digital
twin's ego-car telemetry, perception, tracking, prediction, risk, road-hazard, safety and live-state
helpers are tested too, without Webots, as are the live-state routes and stream, end to end.
The 3D dashboard's JavaScript (snapshot checks, connection states, the mapping to the 3D view,
labels, the event feed, the WebSocket's life) has its own tests on recorded snapshots, run with
Node's built-in test runner: `node --test tests/js/` (Node 22.7 or later; `pytest` runs them
too when it finds Node).

```powershell
pip install -e ".[desktop,web,dev]"
python -m pytest
```

No test needs the desktop extra, so a web-only install such as the Raspberry Pi runs the
whole suite after `pip install -e ".[web,dev]"`.

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
    __main__.py     command line: --host, --port, --no-browser
    server.py       FastAPI app: REST API, /ws/telemetry and /ws/live WebSockets, static UI
    service.py      serial/simulator reader, recording, ZIP export
    live.py         the digital twin's latest live state and its WebSocket clients
    static/         HTML/CSS/JS front-end; vendor/ holds bundled Chart.js, three.js + icons
      autonomy/     the 3D dashboard (/autonomy): live.js, view.js, scene.js, main.js
deploy/roadsense.service  optional systemd unit (Raspberry Pi)
tools/serial_sim.py test-only serial writer (virtual COM pair)
tools/live_listen.py prints the live-state stream (no dashboard needed)
simulation/webots/  Webots digital twin: highway, SUMO traffic, potholes, EGO_ROADSENSE car with radar tracking, collision risk, a hazard map and unified safety recommendations, streamed live to the web server (its own README)
tests/              pytest suite; tests/js/ the 3D dashboard's Node tests
docs/serial-protocol.md
docs/live-state.md   live-state snapshot, routes and coordinates
docs/autonomy-dashboard.md  the 3D dashboard: running it, what it shows, its states
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
  a second lock only serializes source changes. A serial port becomes the source only after
  the device identifies itself; its worker thread owns the port, and if the Arduino is
  unplugged it closes the port and ends the source with the reason. Packets and status
  changes are pushed to the browser over the WebSocket through a bounded queue per client;
  when a client's queue is full its stale packets are dropped (counted in `/api/status` as
  `ws_dropped_messages`) while status messages are kept, and the source thread never waits
  on a client.
- **Live state:** `LiveStateHub` in `live.py` checks each snapshot the Webots digital twin POSTs,
  keeps the latest in memory and offers it to every `/ws/live` client through a one-slot queue,
  so a slow client skips to the newest snapshot. It runs on the event loop, with no thread or lock.

## Limitations

- No GPS, speed, battery, or map data — that hardware does not exist in this version.
- Road-condition classification is done on the Arduino; the app does not re-classify.
- Firmware shock thresholds are un-calibrated placeholders (see Calibration above).
- The desktop app and the web dashboard are separate programs, and both use the same SQLite
  file by default. Connect only one of them to the Arduino: Windows lets a serial port be
  open in one program at a time, but Linux and macOS don't, and while the web dashboard
  locks the port it opens, the desktop app does not.
- The API documentation page (`/docs`) is FastAPI's Swagger UI, which loads from a CDN, so
  it needs internet access. The dashboard itself does not.
- The web API has no authentication. It listens on `127.0.0.1` (this computer only) unless
  started with `--host 0.0.0.0`, which lets every device that can reach the computer over
  the network view and control RoadSense.
- After the Arduino is unplugged, RoadSense does not reconnect by itself; connect again from
  the dashboard.
- Most Raspberry Pi models have no battery-backed clock. Offline, with no network time, the
  Pi's clock can be wrong after a boot, and recorded timestamps follow it.
- Cutting the Pi's power (e.g. with the ignition) instead of stopping RoadSense loses up to
  the last second of buffered telemetry and leaves that recording without an end time.
