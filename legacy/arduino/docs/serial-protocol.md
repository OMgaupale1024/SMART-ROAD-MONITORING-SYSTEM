# RoadSense Serial Protocol (v1)

A newline-terminated CSV protocol from the Arduino UNO to the PC over USB serial.
Line-based, one-way (Arduino → PC), no acknowledgements. **Baud rate: 115200.**

Once the protocol is running, the Arduino sends **only** the messages below on this serial
line — no free-form debug text — so the desktop parser can trust the framing.

## Messages

Every message is a single line ending in `\n` (a trailing `\r` is tolerated by the parser).

### Handshake — sent once at startup

```
HELLO,ROADSENSE,1
```

- Field 2 is always `ROADSENSE`.
- Field 3 is the protocol/firmware version (`1`).

Opening the UNO's serial port restarts it, so this line arrives about 2 s after every connect.
The web dashboard treats a port as the RoadSense Arduino only once this line (or, if it went
missing, a valid `T`/`E` line) arrives within 5 s, and refuses a `HELLO` with any other
version, also if one arrives later.

### Telemetry — `T`, ~10 per second

```
T,<arduino_time_ms>,<ay>,<shock>,<distance_cm|NA>,<status>
```

Example:

```
T,123456,15600,3200,28.4,NORMAL
```

### Event — `E`, only when a condition is classified

```
E,<arduino_time_ms>,<ay>,<shock>,<distance_cm|NA>,<status>
```

Example:

```
E,123456,15600,5200,34.8,POTHOLE
```

Events are rate-limited on the Arduino (a short cooldown) so a sustained bump does not
emit an `E` on every telemetry tick. The `T` stream continues throughout.

## Field definitions

| Field             | Type            | Notes |
|-------------------|-----------------|-------|
| `message_type`    | `T` or `E`      | `T` = telemetry, `E` = classified event. Anything else is rejected. |
| `arduino_time_ms` | integer         | `millis()` since Arduino boot. Not wall-clock; the PC stamps its own receive time. |
| `ay`              | integer         | Raw MPU6050 Y-axis acceleration register value (int16). |
| `shock`           | integer         | `abs(ay - previousAY)` — the Arduino's jolt measure. |
| `distance_cm`     | number or `NA`  | HC-SR04 distance in cm, or `NA` when the echo times out. **Never fabricated.** |
| `status`          | enum            | One of `NORMAL`, `SPEED_BREAKER`, `POTHOLE`. |

## Parser rules (desktop side)

The desktop treats serial input as unreliable and applies these rules
(`src/roadsense/protocol.py`):

- Blank / whitespace-only lines are ignored.
- A line must have exactly 6 comma-separated fields (except `HELLO`).
- `arduino_time_ms`, `ay`, `shock` must be integers.
- `distance_cm` is `NA` (→ null) or a finite, non-negative number.
- `status` must be one of the three valid values.
- Unknown message types and malformed lines are **rejected, counted, and logged** to a
  bounded in-memory diagnostics buffer — they never crash the reader, which keeps reading.
- If no valid telemetry arrives for **more than 2 seconds**, the UI marks data as stale.

## Authority & honesty

- The **Arduino** is the authority for classifying `NORMAL` / `SPEED_BREAKER` / `POTHOLE`.
  The desktop app displays and stores these; it does not re-classify.
- Distance `NA` is preserved as null end-to-end (UI shows `NA`, database stores `NULL`,
  CSV export writes `NA`). No distance is ever invented.
- There are no GPS, speed, battery, or location fields in v1 — those data sources do not
  exist on this hardware.
