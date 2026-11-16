# Edge measurements (`GET /api/system`)

The dashboard's **Edge processing** panel and status rail show what the edge server measured about
itself. Every figure is a measurement taken on the server; none is estimated, and none is taken in the
browser. A value the host cannot supply is `null`, with the reason under `host.unavailable`; the
dashboard shows it as `n/a` with that reason as a tooltip.

Code: `src/roadsense/web/metrics.py`. Tests: `tests/test_web_metrics.py`.

## Stage timings of a sensor frame

Accepted sensor frames that execute the pipeline are timed with `time.perf_counter()` through
`POST /api/sensor-frame`:

| Stage | From | To | What it covers |
| ----- | ---- | -- | -------------- |
| `read` | the handler starts | the body is read | receiving the frame (network, for a large body) |
| `validate` | the body is read | `Pipeline.process()` is called | parsing, validation, run and sequence checks |
| `process` | `Pipeline.process()` is called | it returns | **the whole pipeline, and nothing else**: tracking, prediction, collision risk, hazard map, safety decision |
| `publish` | `Pipeline.process()` returns | every client's queue has the snapshot | building and encoding the snapshot, fan-out |
| `handler` | the handler starts | the frame is fully handled | all of the above plus the listeners, up to the metrics recording step |

`handler` excludes metrics recording and response serialization/socket transmission. It is not the time the sender waits: that adds the network and the HTTP exchange, which only the
sender can measure. The model's inference time is separate from all of these (`ai.latency_ms`): it runs
on its own thread and no frame waits for it.

The simulation's 200 ms cycle is the cadence frames are produced at, not a deadline RoadSense
guarantees: Python on Linux is not a real-time system. The numbers below say what was observed.

For each stage the server reports `last`, `mean`, `p50`, `p95` and `max` in milliseconds, from the
samples it holds: the last 1,500 processed frames **of the current run** (5 minutes at the usual
cadence). Percentiles are nearest-rank, so each is one of the samples. A new `run_id` starts a new
history, so the figures always describe one run; `latency_ms.first_sequence` and `last_sequence` say
which frames.

Recording costs the sensor-frame path one lock, a few subtractions and a `deque.append`. Percentiles are
computed when the dashboard asks, in a worker thread.

## The answer

```json
{
  "schema": "roadsense.system.v1",
  "app": {"name": "RoadSense", "version": "2.0.0.dev0", "started_at_unix_s": 1794851290.1, "uptime_s": 242.7},
  "server_time_unix_s": 1794851532.8,
  "edge": {
    "mode": "edge",
    "run": {"run_id": "efa76bdb83bc", "first_sequence": 1, "last_sequence": 1076, "frames": 1076,
            "missed_frames": 0, "sim_time_s": 215.0, "active": false, "mode": "edge",
            "started_at_unix_s": 1794851507.1},
    "frames": {"accepted": 1782, "rejected": 0, "rejected_by_status": {}, "missed": 0, "runs": 4},
    "rate_hz": 5.0,
    "interval_ms": {"mean": 200.4, "max": 213.0},
    "data_age_s": 0.12,
    "latency_ms": {
      "samples": 1075, "first_sequence": 1, "last_sequence": 1075,
      "read":     {"last": 0.013, "mean": 0.010, "p50": 0.008, "p95": 0.022, "max": 0.096},
      "validate": {"last": 0.047, "mean": 0.064, "p50": 0.052, "p95": 0.134, "max": 0.377},
      "process":  {"last": 0.263, "mean": 0.288, "p50": 0.247, "p95": 0.536, "max": 7.929},
      "publish":  {"last": 0.139, "mean": 0.171, "p50": 0.136, "p95": 0.297, "max": 16.328},
      "handler":  {"last": 0.479, "mean": 0.546, "p50": 0.456, "p95": 0.960, "max": 17.355}
    },
    "series": {"sequence": [926, 927], "process_ms": [0.241, 0.263], "handler_ms": [0.452, 0.479]},
    "clients": {"websocket": 1, "dropped_messages": 0}
  },
  "host": {
    "hostname": "raspberrypi", "system": "Linux", "release": "6.12.47+rpt-rpi-2712", "machine": "aarch64",
    "model": "Raspberry Pi 5 Model B Rev 1.0", "python": "3.13.5", "cpu_count": 4,
    "cpu_percent": 12.5, "load_avg": [0.42, 0.31, 0.2],
    "memory": {"total_mb": 4047.7, "available_mb": 3035.8, "used_percent": 25.0, "swap_used_mb": 0.0},
    "temperature_c": 48.4,
    "throttling": {"raw": "0x0", "now": [], "since_boot": []},
    "process": {"cpu_percent": 3.1, "rss_mb": 61.2, "peak_rss_mb": 63.0, "threads": 4, "uptime_s": 242.7},
    "unavailable": {}
  },
  "safety": {"critical_ttc_s": 1.5, "high_ttc_s": 3.0, "horizon_s": 5.0, "nominal_speed_kmh": 80.0},
  "ai": {"enabled": true, "model": "qwen3:0.6b", "availability": "available", "status": "ready",
         "latency_ms": {"last": 4071.2, "mean": 4070.0, "p50": 4050.0, "p95": 4710.0, "max": 4710.0}}
}
```

(The `edge` figures above were measured on a MacBook, not on a Raspberry Pi. The `host` and `ai` blocks
only illustrate what a Pi answers with: their values are examples, not measurements.)

### `edge`

| Field | Meaning |
| ----- | ------- |
| `mode` | `edge`: states are computed here from sensor frames. `precomputed`: the producer computes them (no stage timings). `null`: nothing received. |
| `run` | the current run: its id, first and last sequence, frames accepted, **missed frames** (sequence numbers the sender skipped because it could not send them in time), simulation time, whether it is still active |
| `frames` | since the server started: accepted, rejected (by HTTP status: 400 malformed, 409 stale or conflicting, 413 too large), missed, number of runs |
| `rate_hz` | frames accepted in the last 5 s, per second (over the run's age while it is younger). It falls to 0 when the stream stalls. |
| `interval_ms` | mean and longest gap between frames in that window |
| `data_age_s` | time since the last accepted frame, measured on the server |
| `series` | sequence, `process` and `handler` times of the last 150 processed frames, for the chart |
| `clients` | WebSocket clients connected, and snapshots skipped for clients that read too slowly |

### `host`

Read from the kernel at most once a second, however many dashboards ask (CPU use is the change between
two readings, so the first answer has none).

| Field | Source | Where it is `null` |
| ----- | ------ | ------------------ |
| `cpu_percent` | `/proc/stat`, all cores, between two readings | not Linux; the first reading |
| `memory` | `/proc/meminfo`: total, available, used %, swap used | not Linux |
| `temperature_c` | `/sys/class/thermal/thermal_zone0/temp` | no such sensor |
| `throttling` | the Raspberry Pi firmware's word: sysfs `get_throttled`, else `vcgencmd get_throttled` (at most every 5 s). `now` and `since_boot` list `under_voltage`, `frequency_capped`, `throttled`, `soft_temperature_limit`. | not a Raspberry Pi; `vcgencmd` missing or failing |
| `load_avg` | `os.getloadavg()` | platforms without it |
| `process` | this server: CPU % (`os.times()`), resident memory (`/proc/self/statm`), peak resident memory, threads, uptime | `rss_mb` outside Linux |
| `model` | `/proc/device-tree/model` | not a device-tree system |

### `safety`

The safety processor's own constants (`roadsense.autonomy.risk`, `prediction`, `safety`), so the
dashboard can draw reference lines on the TTC chart without holding thresholds of its own.

## Measured so far

On the development MacBook (Apple silicon, Python 3.12), with the real Webots simulation sending
sensor frames to the server on the same computer:

| Run | Frames | Missed | `process` p50 / p95 / max | `handler` p50 / p95 / max |
| --- | ------ | ------ | ------------------------- | ------------------------- |
| real time (5 frames/s), 55 s of simulation | 278 | 0 | 0.24 / 0.32 / 0.42 ms | 0.45 / 0.57 / 0.71 ms |
| fast mode (about 50 frames/s), 215 s of simulation | 1,076 | 0 | 0.25 / 0.54 / 7.9 ms | 0.46 / 0.96 / 17.4 ms |

**These are not Raspberry Pi numbers.** The Pi's are whatever its own dashboard shows once this version
runs there; none were taken while this was written.
