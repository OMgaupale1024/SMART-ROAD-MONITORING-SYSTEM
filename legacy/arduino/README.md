# RoadSense Arduino stack (archived)

The first RoadSense: a USB-serial road-condition monitor for the car-in-a-box prototype. An Arduino
UNO (MPU6050 accelerometer, HC-SR04 ultrasonic sensor, OLED, LED, buzzer) classifies the road and
sends telemetry; a desktop app or a serial gateway shows and records it.

RoadSense 2.0 is the edge server and dashboard for the Webots digital twin (`src/roadsense`, see the
[top-level README](../../README.md)). Nothing there imports anything here. This directory keeps the
hardware-facing code so it can be reused when real sensors are connected to the Raspberry Pi; it is
frozen at RoadSense v0.2.1 plus the Raspberry Pi serial-gateway preparation that followed it.

| Path | What it is |
| ---- | ---------- |
| `firmware/roadsense_arduino.ino` | the Arduino UNO firmware |
| `docs/serial-protocol.md` | serial protocol v1 (newline-terminated CSV at 115200 baud) |
| `src/roadsense_arduino/protocol.py`, `models.py` | the strict protocol parser and its records |
| `src/roadsense_arduino/gateway.py` | thread-based serial manager without Qt: exclusive port, handshake before a port counts as the Arduino, unplug detection, recording. It was `roadsense/web/service.py`. |
| `src/roadsense_arduino/database.py`, `repositories.py`, `session_service.py`, `export_service.py`, `paths.py` | SQLite session recording and CSV export |
| `src/roadsense_arduino/app.py`, `ui/`, `serial_client.py`, `simulator.py` | the PySide6 desktop app |
| `tools/serial_sim.py` | a fake Arduino on a serial port, for testing without hardware |
| `tests/` | the tests of all of the above |

What is not here: the old web dashboard (its page, script, styles, Chart.js and Font Awesome) and its
REST and WebSocket routes. They were the Arduino monitoring UI that RoadSense 2.0 replaced, and are in
the Git history: `git show 50cd9b2:src/roadsense/web/server.py`, and
`git show 50cd9b2:src/roadsense/web/static/app.js` (likewise `index.html`, `styles.css`). The whole
application as released is the tag `v0.2.1`.

## Using it

The package is named `roadsense_arduino` and is not installed with RoadSense. From the repository root:

```sh
# the tests (pyserial needed; PySide6 only for the desktop app itself)
python -m pytest legacy/arduino/tests -q

# the desktop app
python -m pip install pyserial PySide6 pyqtgraph
PYTHONPATH=legacy/arduino/src python -m roadsense_arduino

# a fake Arduino for the serial path
python legacy/arduino/tools/serial_sim.py --help
```

## Reusing it for real sensors

The pieces a future sensor integration on the Pi would start from are `protocol.py` (parsing that never
trusts the wire), `gateway.py` (the port handshake and the handling of an unplugged device) and the
firmware. A real IMU or GPS would feed the edge server as another producer of sensor frames, next to
Webots; see [docs/hazard-records.md](../../docs/hazard-records.md) for how real positions are meant to
enter the hazard records.
