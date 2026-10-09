# RoadSense Live State (`roadsense.live.v1`)

The Webots digital twin can stream RoadSense's state to the RoadSense web server, which keeps the
latest state and passes it on to any number of WebSocket clients, such as the future 3D dashboard.
Each message is one **snapshot**: everything RoadSense worked out in one 200 ms processing cycle —
the car, the tracked vehicles with their predicted paths and collision risk, the hazard map and the
unified safety recommendation. A client only draws it; it computes no tracking, prediction, TTC,
hazard severity or decision of its own.

## Architecture

```
Webots: EGO_ROADSENSE's controller (simulation/webots/controllers/roadsense_ego)
  every 200 ms: radars -> tracks -> predictions, collision risk -> hazard map -> safety decision
  live_state.py      gathers that cycle's records into one snapshot, as JSON
  live_publisher.py  background thread: HTTP POST, newest snapshot only, backoff when unreachable
        |
        |  POST /api/live-state
        v
RoadSense web server (python -m roadsense.web, src/roadsense/web/live.py)
  checks the snapshot, keeps the latest one in memory
        |
        |  GET /api/live-state  (latest)      WS /ws/live  (latest at once, then each new one)
        v
dashboards, tools/live_listen.py, ... (any number)
```

- **The simulation is a producer, the clients are consumers.** The controller doesn't know or care
  how many clients there are; the server sends each one the snapshots on its own.
- **HTTP POST from the simulation.** Webots runs controllers with the system Python 3.9 and only its
  standard library, which has an HTTP client but no WebSocket client. Each POST stands alone, so
  there is no connection to keep alive or re-establish, and on this computer one takes about 2 ms.
- **The existing web server.** It already runs FastAPI with WebSockets on the same event loop; the
  stream adds two routes and a WebSocket to it rather than a second server.
- **Optional.** The simulation runs exactly as before without the server. Publishing is off unless
  asked for; when it is on and the server is down, the controller prints one line and carries on.
- **Memory only.** The server keeps the latest snapshot, nothing older, and writes nothing to the
  database.

## Running it

From the repository root, in two terminals:

1. Start the web server (the virtual environment with the `web` extra):

   ```sh
   venv/bin/python -m roadsense.web --no-browser
   ```

2. Start Webots with publishing on:

   ```sh
   export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/share/sumo"
   export PATH="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/bin:$PATH"
   export WEBOTS_EXTRA_PROJECT_PATH="/Applications/Webots.app/Contents/projects/vehicles"
   export ROADSENSE_LIVE_PUBLISH=1
   /Applications/Webots.app/Contents/MacOS/webots simulation/webots/worlds/roadsense_highway.wbt
   ```

Either may start first. The Webots console says `[RoadSense:LIVE] publishing live state to
http://127.0.0.1:8000/api/live-state` once snapshots get through.

Check the latest snapshot:

```sh
curl -s http://127.0.0.1:8000/api/live-state | python3 -m json.tool | head -40
```

Watch the stream, one line per snapshot (`--json` prints each snapshot instead):

```sh
venv/bin/python tools/live_listen.py
```

```
seq=102 t=20.4s speed=80.0km/h tracks=13 nearest=TRACK_002@6.5m conflicts=0 hazards=1 next_hazard=PH_001@59.8m risk=HIGH action=BRAKE target=60.1km/h threat=PH_001
```

Several listeners (or browser tabs) can watch at once.

## Publisher settings

Environment variables of the shell that starts Webots:

| Variable                   | Default                                | Meaning                                                    |
| -------------------------- | -------------------------------------- | ---------------------------------------------------------- |
| `ROADSENSE_LIVE_PUBLISH`   | off                                    | `1`, `true`, `yes` or `on`: publish                        |
| `ROADSENSE_LIVE_URL`       | `http://127.0.0.1:8000/api/live-state` | where to POST snapshots                                    |
| `ROADSENSE_LIVE_PERIOD_S`  | `0.2`                                  | simulated seconds between snapshots, at most one per cycle |
| `ROADSENSE_LIVE_TIMEOUT_S` | `0.5`                                  | longest wait for one POST                                  |

An unusable value is reported once and the default used.

- `publish()` only hands the snapshot to a background thread and returns at once. The thread sends
  one snapshot at a time; a newer one replaces one that hasn't gone yet.
- Encoding a snapshot takes about 0.25 ms; sending happens off the control loop.
- If the server can't be reached or refuses a snapshot, the controller prints **one** line
  (`[RoadSense:LIVE] cannot reach … retrying with backoff, newest snapshot only`) and tries again
  after 1, 2, 4, 8 and then every 10 s, each time with the newest snapshot. When a POST gets
  through again it prints one more line with how many attempts failed and snapshots were skipped.
- When the simulation ends, the controller sends a last snapshot with `simulation.active` false.

## Routes

| Route                    | Answer                                                                                     |
| ------------------------ | ------------------------------------------------------------------------------------------ |
| `POST /api/live-state`   | `200 {"accepted": true, "run_id": …, "sequence": …}`; `400` malformed; `409` not newer; `413` over 1 MB |
| `GET /api/live-state`    | the latest snapshot; `404 {"detail": "No live state received yet"}` before the first one   |
| `WS /ws/live`            | text frames, one snapshot each                                                             |

**Accepted snapshots.** The server refuses (400) anything that isn't one JSON object with the
`roadsense.live.v1` schema, all top-level fields, a `track_id` on every track and a `hazard_id` on
every hazard, a `run_id`, an integer `sequence` from 1 and an `active` flag, or that holds `NaN`,
`Infinity` or a number too large to be finite. Within one `run_id`, `sequence` must increase: a
repeated or older snapshot is refused with 409 and the latest state stays. A different `run_id` (the
simulation restarted) replaces it. A refused snapshot reaches no client.

**WebSocket.** A client sends nothing. On connecting it gets the latest snapshot at once, if there
is one, then each new snapshot as the server accepts it. Before any producer it simply waits. A
client that reads slower than snapshots arrive skips the ones it missed and gets the newest; it
never holds up the simulation or another client, and one that disconnects or fails is dropped
without affecting the others. After the simulation ends, the last snapshot (`active` false) stays
available until a new run starts.

**Access.** This is a local development prototype without a login. The server listens on
`127.0.0.1` by default, so only this computer can post or read; don't run it with
`--host 0.0.0.0` on a network you don't trust.

## The snapshot

| Field            | Content                                                                                          |
| ---------------- | ------------------------------------------------------------------------------------------------ |
| `schema`         | `"roadsense.live.v1"`. A client should check it; an incompatible change gets a new version.     |
| `timestamp`      | simulation time of the processing cycle, s                                                      |
| `ego`            | the car (telemetry record)                                                                       |
| `road`           | this world's lanes relative to the car                                                           |
| `tracks`         | the confirmed tracks, each with its predicted trajectory and collision risk                     |
| `hazards`        | the hazard map: every hazard found so far, also those already passed                            |
| `unified_safety` | the unified safety recommendation (`safety.decide`)                                              |
| `simulation`     | where the snapshot came from                                                                     |

Numbers that aren't available are `null`, never `NaN` or `Infinity`: for example `ttc_s` without a
conflict, `ego.acceleration_mps2` in a run's first snapshot, `time_to_hazard_s` at standstill.

**`ego`** — `vehicle_id` (`EGO_ROADSENSE`), `timestamp`, `position` (`x`, `y`, `z`, world frame,
m), `speed_mps`, `speed_kmh`, `heading_deg` (0–360, counter-clockwise from world +x),
`acceleration_mps2` (speed change since the previous snapshot), `steering_rad` (positive steers
right), `mode`, `maneuver` (`lane_keep` or `lane_change`) and `lane`, the driving controller's
target lane: 0 right, 1 middle, 2 left.

**`road`** — `lane_width_m`, `heading_deg` (the road's direction relative to the car's heading,
positive left) and `lanes`, from the median outwards: each lane's `relation` to the car
(`ego_lane`, `left_lane`, `right_lane`, `other_lane`, or `unknown` when the car is off the
carriageway), `center_lateral_m` (its centre line's distance from the car's origin across the road,
positive left) and `driving` (false for the outer lane, which SUMO keeps for pedestrians). Lane
lines run in the road's direction. They come from this world's straight carriageway; a real car
would report its detected lanes here.

**`tracks[]`** — the perception record, unchanged: `track_id` (`TRACK_001`…, stable while the
vehicle is tracked), `object_type`, `timestamp`, `relative_position` (`longitudinal_m`,
`lateral_m`), `distance_m`, `bearing_deg`, `relative_velocity` (`longitudinal_mps`,
`lateral_mps`), `relative_speed_mps` (radar range rate, negative closing), `lane_relation`,
`source`, `age_s`, `last_seen`; plus

- `prediction`: `model` (`constant_velocity`) and `trajectory`, positions `t_s` = 0.5 … 5 s ahead;
- `collision`: `ttc_s` (null without a conflict), `time_to_cpa_s`, `min_separation_m`, `closing`,
  `conflict`, `risk` (`SAFE`, `CAUTION`, `HIGH`, `CRITICAL`).

**`hazards[]`** — the hazard map's records: `hazard_id` (`PH_001`…), `type`, `world_position`,
`severity` (`LOW`, `MEDIUM`, `HIGH`), `confidence`, `dimensions`, `first_seen_s`, `last_seen_s`,
`observation_count`, `source_vehicle`, `source`, `status`, and `relative`: `longitudinal_m`,
`lateral_m`, `distance_m`, `bearing_deg`, `lane_relation`, `direction` (`ahead` or `behind`).

**`unified_safety`** — `safety.decide`'s state as it is: `overall_risk`, `primary_threat`
(`type`, `id`, `reason`, or null), `recommended_action`, `recommended_speed_kmh`,
`recommended_lane`, `reason`, `vehicle_safety` (`overall_risk`, `most_critical_track`,
`minimum_ttc_s`, `active_conflicts`) and `road_safety` (the potholes that matter now, each with its
urgency and recommended response). It is advisory: nothing in the simulation acts on it.

**`simulation`** — `source` (`webots`), `active` (false in a run's last snapshot), `run_id` (new
each time the controller starts), `sequence` (1, 2, 3… within a run; a gap means skipped
snapshots) and `generated_at_unix_s` (the computer's clock when the snapshot was made).

### Coordinates

- **Ego frame** — origin at `EGO_ROADSENSE`'s origin, the centre of its rear axle on the ground;
  **x (longitudinal) positive ahead, y (lateral) positive left**, in metres; bearings in degrees,
  positive left. Relative velocities are the other vehicle's velocity over the ground minus the
  car's, along these axes. The frame is the car's at the snapshot's `timestamp`.
  Used by `tracks[].relative_position`, `relative_velocity`, `bearing_deg`, every
  `tracks[].prediction.trajectory` point (`t_s` seconds after `timestamp`), `hazards[].relative`,
  `unified_safety.road_safety.hazards[]` and `road.lanes[].center_lateral_m`.
- **World frame** (Webots, z up) — only `ego.position`, `ego.heading_deg` and
  `hazards[].world_position`. A client can draw everything in the ego frame and needs these only
  for a map.
- `EGO_ROADSENSE`'s body is a 4.9 m × 1.8 m box whose centre is 1.44 m ahead of its origin.

### One cycle per snapshot

Everything in a snapshot comes from the same processing cycle: the car, the tracks and their
predictions and risks, where each hazard is relative to the car, and the decision were all worked
out at `timestamp`, and `ego.timestamp`, `tracks[].timestamp` and `unified_safety.timestamp` all
equal it. Older values are history by meaning: a track not seen this cycle has an earlier
`last_seen` and a position moved on from where it was last seen, and a hazard keeps its
`first_seen_s` and `last_seen_s`.

### Example

A real snapshot, the one in which `EGO_ROADSENSE` first saw `PH_001`, with 12 of its 13 tracks and
9 of each trajectory's 10 points left out:

```json
{
  "schema": "roadsense.live.v1",
  "timestamp": 20.4,
  "ego": {"timestamp": 20.4, "vehicle_id": "EGO_ROADSENSE", "position": {"x": -410.152, "y": 3.199, "z": 0.313},
          "speed_mps": 22.222, "speed_kmh": 80.0, "heading_deg": 180.0, "acceleration_mps2": -0.0,
          "steering_rad": -0.0, "mode": "baseline_highway", "maneuver": "lane_keep", "lane": 2},
  "road": {"lane_width_m": 3.75, "heading_deg": 0.0, "lanes": [
    {"relation": "ego_lane", "center_lateral_m": 0.07, "driving": true},
    {"relation": "right_lane", "center_lateral_m": -3.68, "driving": true},
    {"relation": "other_lane", "center_lateral_m": -7.43, "driving": true},
    {"relation": "other_lane", "center_lateral_m": -11.18, "driving": false}]},
  "tracks": [
    {"track_id": "TRACK_002", "object_type": "vehicle", "timestamp": 20.4,
     "relative_position": {"longitudinal_m": 4.44, "lateral_m": -4.74}, "distance_m": 6.5, "bearing_deg": -46.8,
     "relative_velocity": {"longitudinal_mps": -8.62, "lateral_mps": -0.24}, "relative_speed_mps": -3.23,
     "lane_relation": "right_lane", "source": "radar_front", "age_s": 19.6, "last_seen": 20.2,
     "prediction": {"model": "constant_velocity",
                    "trajectory": [{"t_s": 0.5, "longitudinal_m": 0.13, "lateral_m": -4.86}]},
     "collision": {"ttc_s": null, "time_to_cpa_s": 0.33, "min_separation_m": 4.82, "closing": true,
                   "conflict": false, "risk": "SAFE"}}],
  "hazards": [
    {"hazard_id": "PH_001", "type": "pothole", "world_position": {"x": -469.96, "y": 3.42}, "severity": "HIGH",
     "confidence": 0.9, "dimensions": {"length_m": 1.4, "width_m": 1.0, "depth_m": 0.08},
     "first_seen_s": 20.4, "last_seen_s": 20.4, "observation_count": 1, "source_vehicle": "EGO_ROADSENSE",
     "source": "webots_simulated_road_sensor", "status": "ACTIVE",
     "relative": {"longitudinal_m": 59.81, "lateral_m": -0.22, "distance_m": 59.81, "bearing_deg": -0.2,
                  "lane_relation": "ego_lane", "direction": "ahead"}}],
  "unified_safety": {
    "timestamp": 20.4, "overall_risk": "HIGH",
    "primary_threat": {"type": "road_hazard", "id": "PH_001", "reason": "PH_001 HIGH pothole 59.8 m ahead in ego_lane"},
    "recommended_action": "BRAKE", "recommended_speed_kmh": 60.1, "recommended_lane": null,
    "reason": "PH_001 HIGH pothole 59.8 m ahead in ego_lane",
    "vehicle_safety": {"timestamp": 20.4, "overall_risk": "SAFE", "most_critical_track": null,
                       "minimum_ttc_s": null, "active_conflicts": 0},
    "road_safety": {"relevant_hazards": 1, "nearest_hazard": "PH_001", "hazards": [
      {"hazard_id": "PH_001", "type": "pothole", "severity": "HIGH", "confidence": 0.9, "distance_m": 59.81,
       "longitudinal_m": 59.81, "lateral_m": -0.22, "lane_relation": "ego_lane", "urgency": "HIGH",
       "time_to_hazard_s": 2.691503831621578, "recommended_action": "BRAKE", "recommended_speed_kmh": 60.1,
       "reason": "PH_001 HIGH pothole 59.8 m ahead in ego_lane"}]}},
  "simulation": {"source": "webots", "active": true, "run_id": "b505eda11523", "sequence": 102,
                 "generated_at_unix_s": 1791524358.769}
}
```

## Limits

- One producer at a time: a second running simulation with its own `run_id` would take turns
  replacing the latest state.
- A snapshot of this highway with a dozen tracks is about 15–20 kB, 5 times a second; nothing is
  compressed or sent as differences.
- The publisher sends over plain HTTP to this computer; there is no authentication or encryption.
