# Road-hazard records (`roadsense.hazard.v1`)

RoadSense maps potholes while it drives: the hazard map (`roadsense.autonomy.hazard_map`) merges every
observation of a pothole into one hazard with an id, a severity and a confidence. This page defines what
such a hazard looks like once it leaves the live state, so that it can later be stored, synchronised and
drawn on a map. It is a structure and a plan. There is no map, no storage and no synchronisation yet.

Code: `src/roadsense/web/hazard_records.py`, served read-only at `GET /api/hazards`.
Tests: `tests/test_web_live.py`.

## What exists today

- **One hazard per pothole.** A detection within 2 m of a mapped hazard of its type is that hazard: its
  position becomes the mean of all observations, its severity the highest reported, and its confidence
  rises with each consistent observation (to at most 0.99). `observation_count` counts them. Repeated observations inside that association radius merge; position noise beyond it can create
  another record, and distinct nearby potholes can merge. This is spatial association, not verified identity.
- **The map lives for one run**, in the edge server's memory. A new simulation run starts an empty map.
- **Positions are simulation coordinates**: metres in the Webots world frame. The dashboard says so, and
  shows no geographic position.

## The record

```json
{
  "schema": "roadsense.hazard.v1",
  "record_id": "efa76bdb83bc:PH_001",
  "local_id": "PH_001",
  "type": "pothole",
  "severity": "HIGH",
  "confidence": 0.972,
  "observations": 13,
  "status": "ACTIVE",
  "dimensions_m": {"length": 1.4, "width": 1.0, "depth": 0.09},
  "position": {"frame": "webots_world", "x_m": -469.98, "y_m": 3.42, "geo": null},
  "observed": {"clock": "simulation", "first_seen_s": 20.4, "last_seen_s": 22.8},
  "provenance": {"simulated": true, "sensor": "webots_simulated_road_sensor",
                 "vehicle_id": "EGO_ROADSENSE", "run_id": "efa76bdb83bc", "processor": "edge"}
}
```

| Field | Meaning |
| ----- | ------- |
| `record_id` | run id and local id: unique, and the same every time the record is produced, so sending it twice changes nothing |
| `position.frame` | the coordinate frame of `x_m`, `y_m`. Today always `webots_world`. |
| `position.geo` | a real position: `{"lat", "lon", "accuracy_m", "fix_time_utc"}` from a GNSS fix. **`null` until a producer supplies one.** RoadSense never derives one from simulation coordinates. |
| `observed.clock` | which clock the times are on: `simulation` seconds today, `utc` once a real clock is the source |
| `provenance.simulated` | true for everything that comes from Webots |
| `confidence`, `observations`, `severity` | as the hazard map holds them |

`GET /api/hazards` returns the latest state's hazards in this form:

```json
{"schema": "roadsense.hazards.v1", "run_id": "efa76bdb83bc", "coordinate_frame": "webots_world",
 "geo_configured": false, "records": [ … ]}
```

## Simulated and real positions stay apart

A record says where its position comes from in two places (`position.frame`, `provenance.simulated`),
and a real position has its own field (`geo`). A map of real roads must take only records with a `geo`
and `simulated: false`. A simulated hazard can be drawn in a simulation view, in its own frame, and
nowhere else.

## The way to a shared map

Not built. This is the intended order, each step usable on its own:

1. **Real positions.** A GNSS receiver on the vehicle gives each sensor frame a fix. The hazard map then
   keeps a `geo` beside the local position, and merges by distance on the ground (the same 2 m rule,
   widened by the fix's accuracy).
2. **Keep records on the edge.** *Partly built:* the opt-in [history store](history.md) keeps each
   hazard's material changes in SQLite on the edge, survives restarts and needs no network, with an
   identity (`<segment_id>/<local_id>`) that never collides across processor lifetimes. A sync
   outbox (acknowledged sequence numbers) is still to come.
3. **Synchronise when online.** The edge sends the outbox from its last acknowledged sequence number.
   Records are idempotent by `record_id`, so sending again after a lost acknowledgement is harmless
   (at-least-once delivery). The edge never needs the central map to keep working.
4. **A central map of hazards, not of observations.** The server decides which records from different
   runs and vehicles are the same hazard (same type, within a few metres), and keeps for it: the number
   of distinct vehicles and observations, the strongest severity, a confidence that rises with
   independent confirmation and falls with time and with passes that saw nothing, and whether it is
   still there.
5. **A map view inside RoadSense.** The hazards are drawn as RoadSense's own layer over a base map
   (OpenStreetMap tiles with Leaflet, or the Google Maps JavaScript API as the base). Google Maps would
   be a way to draw RoadSense's data; RoadSense cannot add potholes to Google's own public map. No map
   API key or billing account is needed until this step has a working use.
