# RoadSense Digital Twin — Webots

Repository-owned Webots simulation for the RoadSense Digital Twin: a highway with SUMO traffic
(Phase 1) and one RoadSense car, `EGO_ROADSENSE`, that drives itself and prints local telemetry
(Phase 2), the vehicles around it, seen by its own radars and tracked (Phase 3), and where those
vehicles are heading and how risky that is: predicted trajectories, time to collision and a risk
level (Phase 4), and the potholes on the road, seen by a simulated hazard camera and kept in a local
hazard map (Phase 5), and one unified safety recommendation (Phase 6). Recommendations never
feed the driving controls. Not here yet: active response, sharing hazards with other vehicles,
or any link from this simulation to the RoadSense backend, database or dashboard.

| Tool   | Version used                                                                |
| ------ | --------------------------------------------------------------------------- |
| Webots | R2025b Nightly Build 7/10/2026, commit `838bb6f` (macOS 27, Apple Silicon) |
| SUMO   | 1.27.1                                                                      |

## Layout

```
worlds/
  roadsense_highway.wbt              the world
  roadsense_highway_net/             SUMO network, routes and config (loaded from <world>_net/)
  forest/roadsense_highway/1.forest  tree positions, read relative to the world
protos/
  RoadSensePothole.proto             the simulation's pothole
controllers/roadsense_ego/
  roadsense_ego.py                   EGO_ROADSENSE's controller
  telemetry.py                       telemetry record and console line (no Webots imports)
  perception.py                      radar targets to detections, ego/world frames, lanes, track record
  tracking.py                        nearby-vehicle tracker
  prediction.py                      track trajectories (constant velocity)
  risk.py                            conflicts, time to collision, risk levels, ego safety state
  hazards.py                         road-hazard detection record; the simulated hazard sensor
  hazard_map.py                      persistent local hazard map, queries, snapshot
  safety.py                          unified risk, primary threat, action, target speed and reason
                                     (all but roadsense_ego.py: no Webots imports)
THIRD_PARTY_NOTICES.md               what comes from Webots, what changed, Apache-2.0 text
```

The world, the SUMO files, the tree file and `roadsense_ego.py` come from the Webots
`highway_overtake` sample. `THIRD_PARTY_NOTICES.md` lists them with their changes and license.

Everything else still comes from Webots and is not copied:

- **PROTOs** (cars, road, sensors, `SumoInterface`): `EXTERNPROTO` URLs pinned to Webots commit
  `838bb6f`. Webots downloads them once into `~/Library/Caches/Cyberbotics/Webots`, so the first
  launch on another machine needs internet.
- **Controllers:** `sumo_supervisor` (moves the SUMO traffic) and `radar_target_tracker` (draws
  the sample radar's targets as boxes). Webots finds the latter only through
  `WEBOTS_EXTRA_PROJECT_PATH`. Without it, Webots prints a "controller directory has not been
  found" warning and runs `<generic>` for that robot. Everything else still works.

## EGO_ROADSENSE

The ego car is the world's Lincoln MKZ:
`DEF WEBOTS_VEHICLE0 LincolnMKZ { name "EGO_ROADSENSE" controller "roadsense_ego" … }`.

- Its robot name, `EGO_ROADSENSE`, is its identity. The controller reads it with `getName()`,
  and the viewpoint follows it.
- Keep the DEF name `WEBOTS_VEHICLE0`. The SUMO supervisor finds Webots-driven cars by the DEF
  names `WEBOTS_VEHICLE0`, `1`, `2`… and stops at the first gap. It mirrors them into SUMO so the
  traffic gives way to them. Renaming this DEF would hide the ego, the parked car and the cone
  zone (`WEBOTS_VEHICLE1` and `2`) from the traffic.
- `roadsense_ego` drives like the sample's `highway_overtake`: up to 80 km/h, slower for the car
  ahead, lane keeping on the `gps` mounted 5 m ahead of the rear axle, and overtaking through a
  free neighbouring lane. SUMO drives all the other traffic.
- Added for telemetry: `telemetry gps` at the car's origin (the centre of the rear axle) and an
  `inertial unit`.
- Added for perception: `radar front` and `radar rear`, see [Perception](#perception). What it
  makes of the tracks: [Prediction and risk](#prediction-and-risk).
- Added for road hazards: `hazard camera`, see [Road hazards](#road-hazards).

### Telemetry

Once per simulated second the controller prints one line to the Webots console and the
terminal:

```
[RoadSense:EGO] t=12.0s speed=80.0km/h pos=(-235.0,3.8,0.3) heading=180.5° accel=-0.00m/s² steer=+0.000rad mode=baseline_highway/lane_keep lane=2
```

The line shows a record built by `telemetry.make_record`:

| Field                      | Unit      | Value                                                                       |
| -------------------------- | --------- | --------------------------------------------------------------------------- |
| `timestamp`                | s         | simulation time                                                             |
| `vehicle_id`               |           | robot name, `EGO_ROADSENSE`                                                 |
| `position` (`x`, `y`, `z`) | m         | car origin in the world frame (z up), from `telemetry gps`                  |
| `speed_mps`, `speed_kmh`   | m/s, km/h | ground speed from `telemetry gps`                                           |
| `heading_deg`              | °         | inertial-unit yaw, 0–360, counter-clockwise from world +x (about 180° here) |
| `acceleration_mps2`        | m/s²      | speed change since the previous record ÷ time between them; `None` at first |
| `steering_rad`             | rad       | Driver steering angle; positive steers right                                |
| `mode`                     |           | `baseline_highway`, the only mode so far                                    |
| `maneuver`                 |           | `lane_keep`, or `lane_change` during an overtake                            |
| `lane`                     |           | target lane: 0 right, 1 middle, 2 left                                      |

Telemetry stays on this computer: no network, database or dashboard. `tests/test_ego_telemetry.py`
tests the record and the line without Webots.

### Perception

`EGO_ROADSENSE` sees the vehicles around it with two Webots `Radar` devices of its own and follows
them as tracks. The radars measure, the telemetry GPS and inertial unit place the measurements on
the ground, and `tracking.py` follows them. No position comes from the simulator's ground truth.

| Radar         | Where (ego frame)                       | Range   | Field of view    | Period |
| ------------- | --------------------------------------- | ------- | ---------------- | ------ |
| `radar front` | x = 4.05 m, front bumper, facing ahead  | 1–150 m | 2.6 rad (±74.5°) | 200 ms |
| `radar rear`  | x = −1.16 m, rear bumper, facing back   | 1–100 m | 2.6 rad (±74.5°) | 200 ms |

- They copy the sample radar's 76.5 GHz signal (31 dBm sent, −80 dBm detectable), noise (0.25 m,
  0.12 m/s, 0.001 rad) and occlusion check. They drop its radial-speed window and its target
  merging: Webots merges targets by range alone, which fused cars driving side by side.
- The period is the SUMO step. SUMO moves its cars every 200 ms, and a Webots radar computes the
  range rate from the movement between two refreshes, so a shorter period sees cars stand still,
  then jump.
- Only vehicles have a radar cross-section in this world, so every target is a vehicle. Scooters
  and motorbikes (cross-section 10 instead of 100) fade out beyond about 100 m.
- Covered: the car's own lane and the lanes beside it, ahead and behind. Not covered: a vehicle
  right alongside. Webots places a radar target by its origin, the rear axle, so a car in the
  next lane is missed while its rear axle is between about 2 m behind and 5 m ahead of
  `EGO_ROADSENSE`'s. It is followed from where it was last seen (`unseen` in the output).
- The sample's radar (`radar`, on the nested robot with `radar_target_tracker`) and its boxes are
  unchanged. `roadsense_ego` can't read that radar: it belongs to the nested robot.

**Ego frame.** Origin at the car's origin (the centre of the rear axle, where `telemetry gps`
sits), x (longitudinal) forward, y (lateral) left. Bearings are in degrees, positive to the left.

**Detection** (`perception.Detection`, one per radar target and cycle): `timestamp`, `source`
(`radar_front` or `radar_rear`), `longitudinal_m`, `lateral_m`, `distance_m` and `bearing_deg`
from the ego origin, `range_rate_mps` (the radar's, negative = closing), and `world_x`,
`world_y` for the tracker. Webots measures the distance to the centre of a vehicle's bounding box
and the azimuth to its origin, so a detection sits close to the middle of the vehicle. Webots
clamps what it can't place, and those targets are dropped: the ego's own body (at the minimum
range), a vehicle reaching into range (at the maximum range), and a vehicle alongside whose origin
is outside the field of view (at its edge). The range rate is `None` on the radar's first refresh
(Webots reports nan) and when SUMO teleports a car onto the road.

**Tracking** (`tracking.py`):

- Tracks live on the ground (the world frame), so the car's own driving and turning don't move
  them.
- Each radar cycle, a detection joins the nearest track whose predicted position, moving at the
  track's velocity, is within 3 m (5 m while the track has no velocity yet). Nearest pairs go
  first; the other detections start new tracks.
- A track seen over 0.4 s (3 cycles) is confirmed: it gets the next id, `TRACK_001`,
  `TRACK_002`…, and a velocity, its displacement over about the last 1.5 s. Only confirmed tracks
  are reported, so a single glimpse never is. Ids are never reused.
- An unseen track moves on at its velocity for at most as long as it was seen, between 0.4 s and
  3 s, then it is dropped.

**Lane relation** compares the lane of the vehicle with the lane of `EGO_ROADSENSE`, from their
positions on the ground: `ego_lane`, `left_lane`, `right_lane`, or `other` (two lanes away, the
oncoming carriageway, off the road). The lanes are this world's: road `0` runs toward −x between
y = 1.25 (the median, on the driver's left) and y = 16.25, in 3.75 m lanes (`CARRIAGEWAY_Y` and
`LANE_WIDTH_M` in `perception.py`).

**Track record** (`perception.track_report`), for each confirmed track:

| Field                                            | Unit | Value                                                                  |
| ------------------------------------------------ | ---- | ---------------------------------------------------------------------- |
| `track_id`                                       |      | `TRACK_001`, `TRACK_002`…                                              |
| `object_type`                                    |      | `vehicle`                                                              |
| `timestamp`                                      | s    | simulation time of the record                                          |
| `relative_position` (`longitudinal_m`, `lateral_m`) | m | in the ego frame                                                       |
| `distance_m`, `bearing_deg`                      | m, ° | from the ego origin                                                    |
| `relative_velocity` (`longitudinal_mps`, `lateral_mps`) | m/s | the vehicle's velocity over the ground minus the ego's, ego axes  |
| `relative_speed_mps`                             | m/s  | range rate the radar measured last, negative = closing; `None` if unknown |
| `lane_relation`                                  |      | see above                                                              |
| `source`                                         |      | radar that saw it last                                                 |
| `age_s`                                          | s    | time since first seen                                                  |
| `last_seen`                                      | s    | simulation time it was last seen                                       |

Once per simulated second, after the telemetry line, the controller prints a summary and one line
per track, nearest first:

```
[RoadSense:PERCEPTION] t=42.0s tracks=7 nearest=TRACK_029@28.4m
  TRACK_029 behind  28.4m lon=-27.5 lat=-7.2 rel_vel=(-11.2,-0.1)m/s rel_speed=+11.4m/s other radar_rear age=15.4s
  TRACK_031 ahead   44.8m lon=+44.1 lat=-7.9 rel_vel=(-7.9,-0.0)m/s rel_speed=-8.0m/s other radar_front age=13.4s
  TRACK_022 behind  79.6m lon=-79.5 lat=-4.0 rel_vel=(-8.4,-0.1)m/s rel_speed=+7.9m/s right_lane radar_rear age=28.4s
  …
```

A track not seen in the latest radar cycle ends with `unseen=…s`. Perception stays on this
computer: no network, database or dashboard. `tests/test_ego_perception.py` tests it without
Webots.

Compared with Webots' ground truth over 200 simulated seconds, through a temporary supervisor
hook that is not in the repository, the tracks seen in the latest cycle were within about 0.4 m
(one standard deviation) in position and distance, 0.13 m/s in range rate and 0.45 m/s in
relative velocity, and 99 % had the right lane relation. No track switched to another vehicle.

### Prediction and risk

Every radar cycle, after tracking, the controller predicts where each confirmed track is going
and rates the risk of a collision with `EGO_ROADSENSE`. `prediction.py` and `risk.py` read only
track records (`perception.track_report`), so any source of such tracks can use them. The driving
logic ignores the result: nothing brakes, steers or changes speed for a risk.

**Prediction** (`prediction.py`) is a constant-velocity baseline: the relative position moves on
at the relative velocity, as if both cars keep their current velocity over the ground. It gives
the position every 0.5 s up to 5 s ahead, in the ego frame of the record's time:

```python
{"track_id": "TRACK_039", "model": "constant_velocity",
 "trajectory": [{"t_s": 0.5, "longitudinal_m": 21.55, "lateral_m": 0.0}, …, {"t_s": 5.0, …}]}
```

A track without a finite position and velocity gets an empty trajectory, so no rating.

**Safety envelope.** `EGO_ROADSENSE`'s body box (the Lincoln MKZ's 4.9 m × 1.8 m, centred
1.44 m ahead of its origin), grown by the size of a typical car (5 m × 1.9 m: a radar target is a
point near the middle of a vehicle, without a size) and a 0.25 m safety buffer. A vehicle's
centre inside the envelope means the two bodies are within 0.25 m of each other. The values are
constants at the top of `risk.py`. A larger buffer flagged cars passing in the next lane: tracks
are within about 0.4 m.

**Conflict, TTC, CPA** (`risk.py`). The predicted path is the track's position now, then its
trajectory points, joined by straight lines, so another prediction model can replace constant
velocity without changing `risk.py`.

- Conflict: the path enters the envelope within the trajectory, 5 s.
- `ttc_s`: the time until it enters. For a car ahead in the lane, the bumper gap less the buffer,
  divided by the closing speed; for a car merging or crossing, when it is inside on both axes. A
  car closing from behind gets one too. `None` without a conflict within 5 s: pulling away,
  keeping pace, passing in another lane, crossing ahead or behind.
- `time_to_cpa_s`, `min_separation_m`: the closest point of approach, when and how close the
  vehicle's centre comes to `EGO_ROADSENSE`'s centre within 5 s.
- `closing`: the two centres are getting closer.

**Risk levels**, by the time to the conflict:

| Risk       | When                                                            |
| ---------- | --------------------------------------------------------------- |
| `SAFE`     | no conflict within 5 s                                          |
| `CAUTION`  | conflict within 5 s                                             |
| `HIGH`     | conflict within 3 s (forward-collision warnings sound at 2–3 s) |
| `CRITICAL` | conflict within 1.5 s, or already inside the envelope           |

A track seen for less than 1 s is rated at most `HIGH`. Its velocity comes from under a second
of detections: against Webots' ground truth it was off by 0.9 m/s (one standard deviation) at
0.4–0.6 s old and 0.4 m/s from 1 s on, and once a car leaving the blind spot came out moving
sideways at 2.6 m/s instead of 0.

**Safety record** (`risk.assess`), per track: `track_id`, `prediction_model`, `ttc_s`,
`time_to_cpa_s`, `min_separation_m`, `closing`, `conflict`, `risk` and the `trajectory`.

**Ego safety state** (`risk.ego_safety`): `timestamp`, `overall_risk` (the highest),
`most_critical_track` (highest risk, then soonest conflict; `None` if there's none),
`minimum_ttc_s` and `active_conflicts`.

Once per simulated second, after the perception summary, the controller prints the safety state
and one line per vehicle in conflict, most critical first:

```
[RoadSense:SAFETY] t=70.0s risk=HIGH conflicts=1 min_ttc=2.4s critical=TRACK_039
  TRACK_039 ahead   25.5m ego_lane rel_vel=(-7.9,+0.0)m/s TTC=2.4s CPA=0.1m@3.1s HIGH
```

`tests/test_ego_risk.py` tests prediction and risk without Webots.

Over 250 simulated seconds, replayed through the same code against Webots' ground truth (through
a temporary supervisor hook that is not in the repository), 99.7 % of the track cycles got the
risk level that the true positions and velocities give, and the TTC was within 0.4 s (one
standard deviation). One, two and three seconds ahead, the predicted position was a median 0.5,
0.7 and 1.0 m from where the track then was (0.4, 0.5 and 0.6 m while both cars held their
speed and lane). Most of the larger errors came from `EGO_ROADSENSE` braking or accelerating,
which constant velocity doesn't foresee. Naturally occurring cases: a car approached from 41 m
at 8 m/s went from `CAUTION` (TTC 4.4 s) to `HIGH` (2.4 s), then back to `SAFE` once the
controller had slowed down, and a car cutting in front at the start was `CRITICAL` (TTC 0.6 s)
while the controller braked hard.

### Road hazards

`EGO_ROADSENSE` finds the potholes on the road ahead with a simulated hazard camera and keeps them
in a local hazard map, where they stay after it has passed them. `hazards.py` and `hazard_map.py`
don't depend on Webots, so a real camera, an IMU or another vehicle can feed the same map later.
The driving logic ignores the hazards: nothing brakes, steers or slows down for a pothole.

**Potholes** (`protos/RoadSensePothole.proto`, RoadSense's own PROTO): a dark, irregular patch with
a lighter broken edge, drawn just above the road, with a `severity` (`LOW`, `MEDIUM` or `HIGH`) and
a `size` (length, width, depth). The road isn't deformed and the pothole has no bounding object,
so vehicles drive over it unaffected: it is a semantic hazard. The world has four, placed where
`EGO_ROADSENSE` meets them in its first minute:

| Simulation object | World x, y      | Lane                                          | Severity | Size (m)          | Found at |
| ----------------- | --------------- | --------------------------------------------- | -------- | ----------------- | -------- |
| `POTHOLE_A`       | −470, 3.3       | next to the median (`EGO_ROADSENSE`'s from 15 s) | `HIGH`   | 1.4 × 1.0, 8 cm deep | ~20 s    |
| `POTHOLE_B`       | −700, 6.9       | middle                                        | `MEDIUM` | 0.9 × 0.7, 4 cm   | ~31 s    |
| `POTHOLE_C`       | −950, 3.0       | next to the median                            | `LOW`    | 0.6 × 0.45, 2 cm  | ~42 s    |
| `POTHOLE_D`       | −1200, 10.7     | right                                         | `MEDIUM` | 1.0 × 0.8, 5 cm   | ~53 s    |

The severities follow the depth, as pavement-distress ratings often do: under 2.5 cm `LOW`,
2.5–5 cm `MEDIUM`, deeper `HIGH`.

**Simulated hazard sensor** (simulation only). `hazard camera` sits on the roof
(`sensorsSlotTop`, 1.61 m ahead of the car's origin, facing forward): a Webots `Camera` with object
recognition, 0.87 rad (50°) wide, up to 60 m, not through a vehicle in the way. It is never
rendered (recognition works without enabling the camera). Its 512 × 256 resolution matters only
because Webots drops recognized objects smaller than a pixel, and Webots sizes a pothole by its
bounding sphere: at that resolution every pothole counts up to 60 m. Each recognized pothole comes
with its label, the PROTO's `model` (`pothole severity=HIGH length=1.4 width=1 depth=0.08`), and
`hazards.simulated_detections` turns it into a detection on the ground using the car's pose.
Everything else the camera recognizes (vehicles, the road, the barriers) is ignored. A pothole in
the car's lane is seen from 60 m until about 7 m ahead, when it drops below the camera's view; one
in another lane leaves the side of the view sooner. At 80 km/h that is about a dozen detections.

**Detection** (`hazards.HazardDetection`): `timestamp`, `source` (`webots_simulated_road_sensor`),
`hazard_type` (`pothole`), `world_x`, `world_y`, `severity`, `confidence` (0.9 for the
simulation's detections) and `dimensions` (`length_m`, `width_m`, `depth_m`, or `None`).

**Hazard map** (`hazard_map.HazardMap`):

- A detection within 2 m of a hazard of its type is that hazard (lanes are 3.75 m apart);
  otherwise it is a new hazard. Malformed detections are skipped.
- RoadSense numbers the hazards in the order it finds them: `PH_001`, `PH_002`… for potholes
  (`HZ_001`… for other types). Ids are never reused; the simulation's names aren't used.
- Each further detection moves the position to the mean of all, updates `last_seen_s` and the
  dimensions, keeps the highest severity reported, and raises the confidence: it closes 0.1 × the
  detection's confidence of the gap to 1, up to 0.99. A pothole goes from 0.90 to about 0.97.
- Nothing is removed: every hazard's `status` is `ACTIVE`.

Road-hazard severity (`LOW`, `MEDIUM`, `HIGH`) is a property of the road, not a collision risk:
`risk.py` doesn't use it.

| Hazard field                  | Unit | Value                                                     |
| ----------------------------- | ---- | --------------------------------------------------------- |
| `hazard_id`                   |      | `PH_001`, `PH_002`…                                       |
| `type`                        |      | `pothole`                                                 |
| `world_position` (`x`, `y`)   | m    | the mean of its detections, world frame                   |
| `severity`                    |      | the highest reported                                      |
| `confidence`                  |      | 0 to 0.99                                                 |
| `dimensions`                  | m    | `length_m`, `width_m`, `depth_m`, or `None`               |
| `first_seen_s`, `last_seen_s` | s    | simulation time                                           |
| `observation_count`           |      | detections merged into it                                 |
| `source_vehicle`              |      | `EGO_ROADSENSE`                                           |
| `source`                      |      | the sensor of its latest detection                        |
| `status`                      |      | `ACTIVE`                                                  |

**Relative to `EGO_ROADSENSE`.** The map stores world positions; where a hazard is relative to
the car is worked out from the car's current pose when asked: `longitudinal_m` and `lateral_m` in
the ego frame (x ahead, y left, origin at the rear axle), `distance_m`, `bearing_deg`,
`direction` (`ahead` or `behind` the rear axle) and `lane_relation`: `ego_lane`, `left_lane`,
`right_lane`, `other_lane` (further over), `off_road` (off `EGO_ROADSENSE`'s carriageway) or
`unknown` (`EGO_ROADSENSE` off it), from this world's lanes in `perception.py`.

**Queries:** `get(hazard_id)`, `active_hazards()`, `nearest_hazard(pose)` (ahead or behind),
`hazards_ahead(pose, max_distance_m=None)` (nearest first) and `snapshot(t, pose)`: the whole map
as plain dicts, lists and numbers, each hazard with its relative state, ready for JSON:

```json
{"timestamp": 31.0, "source_vehicle": "EGO_ROADSENSE", "hazards": [
  {"hazard_id": "PH_001", "type": "pothole", "world_position": {"x": -469.96, "y": 3.42},
   "severity": "HIGH", "confidence": 0.968, "dimensions": {"length_m": 1.4, "width_m": 1.0, "depth_m": 0.08},
   "first_seen_s": 20.4, "last_seen_s": 22.8, "observation_count": 13, "source_vehicle": "EGO_ROADSENSE",
   "source": "webots_simulated_road_sensor", "status": "ACTIVE",
   "relative": {"longitudinal_m": -175.38, "lateral_m": -0.22, "distance_m": 175.38, "bearing_deg": -179.9,
                "lane_relation": "ego_lane", "direction": "behind"}},
  {"hazard_id": "PH_002", …, "relative": {"longitudinal_m": 54.67, "lateral_m": -3.79, …}}]}
```

The controller prints a line as soon as it finds a hazard:

```
[RoadSense:HAZARD_EVENT] t=20.4s discovered PH_001 type=pothole severity=HIGH distance=60.2m lane=ego_lane conf=0.90
```

and, once per simulated second after the safety summary, the map: its size, the nearest hazard
ahead and one line per hazard:

```
[RoadSense:HAZARD] t=31.0s map=2 ahead=1 nearest_ahead=PH_002@54.8m severity=MEDIUM lane=right_lane
  PH_001 pothole behind 175.4m ego_lane   HIGH   conf=0.97 obs=13
  PH_002 pothole ahead   54.8m right_lane MEDIUM conf=0.91 obs=2
```

`tests/test_ego_hazards.py` tests detections, the map and the output without Webots.

Over 250 simulated seconds, replayed through the same code against the potholes' true positions:
each pothole became exactly one hazard, `PH_001` to `PH_004`, first seen 59–62 m ahead and mapped
within 0.13 m. Each got 11–13 detections without a duplicate and turned `behind` in the cycle the
car passed it. All four were still in the map at the end, 3.7–4.4 km behind.

## Unified safety recommendations (Phase 6)

`safety.decide(timestamp, ego_speed_mps, assessments, reports, hazard_snapshot,
adjacent_lanes=(), policy=Policy())` consumes ordinary RoadSense records from `risk.assess`,
`perception.track_report` and `HazardMap.snapshot`. It imports no Webots APIs and changes no
input. The controller evaluates it at 5 Hz and prints one `[RoadSense:DECISION]` line at 1 Hz,
after the existing vehicle safety and hazard summaries. `unifiedSafetyState` is available locally
for a future consumer; no endpoint, dashboard or control connection is added.

This is an **academic recommendation baseline**, not certified braking or lane-change logic.
The existing sample controller continues to drive exactly as before, even when the recommended
speed is zero. Its nominal 80 km/h limit initializes `safety.Policy.nominal_speed_kmh`.

### Road urgency and target speed

Only ACTIVE potholes strictly ahead in the ego or adjacent lanes enter the dynamic threat list.
The lookahead is `max(30 m, ego_speed × 8 s)`. Behind, off-road, unknown-lane, further-lane and
out-of-range hazards remain in the persistent map but do not produce current warnings.
Adjacent-lane hazards and confidence below 0.6 give CAUTION / MONITOR without reducing speed.

For a confident ego-lane pothole, the baseline assumes a crossing speed `c` of 70%, 45% or 20%
of nominal speed for LOW, MEDIUM or HIGH severity. These are tuning assumptions, not measured
safe crossing speeds. With current speed `v` in m/s, reaction time `r = 1 s` and comfortable
deceleration `a = 3 m/s²`, the distance needed to reach that crossing speed is:

`d_comfortable = v*r + max(0, v² − c²)/(2*a)`

If speeding above `c` within this distance, MEDIUM/HIGH potholes become HIGH urgency; LOW remains
CAUTION. SLOW_DOWN becomes BRAKE for HIGH urgency inside the corresponding distance computed
with strong deceleration `6 m/s²`. Potholes alone do not produce CRITICAL or emergency braking.
The approach target varies continuously with distance `d`:

`u = max(c, sqrt((a*r)² + c² + 2*a*d) − a*r)`

The target is capped by nominal speed and current speed for a confident ego-lane hazard, then
converted to km/h and rounded to one decimal. At most one unified action is emitted. All
concurrent hazard speed caps are respected without replacing an imminent collision action.

### Vehicle priority and actions

Vehicle collision levels and TTC come unchanged from Phase 4. HIGH/CRITICAL collision warnings
(normally TTC ≤ 3 s, including young HIGH tracks) always take priority over potholes. A HIGH
pothole takes priority over a CAUTION vehicle conflict; a CAUTION vehicle takes priority over
CAUTION road threats. Road ties use urgency, lower target speed, ego-lane relevance, distance,
then stable hazard id. The nearest relevant hazard is reported separately from the primary.

The stable string-valued `Action` enum is `MAINTAIN`, `MONITOR`, `SLOW_DOWN`, `BRAKE`,
`EMERGENCY_BRAKE`, `CONSIDER_LANE_CHANGE`. Vehicle CAUTION recommends SLOW_DOWN, HIGH recommends
BRAKE, CRITICAL recommends EMERGENCY_BRAKE with target zero. For the first two, the target is
`max(0, v − a*max(reaction_time, 3 − TTC))`, capped by nominal speed, using comfortable/strong
`a` respectively. This is an urgency-based speed reduction, not a collision-free speed solution;
it does not optimize the braking response for a rear-end threat.

### Lane-change gating

For a confident MEDIUM/HIGH ego-lane pothole more than four seconds away, and no vehicle
conflict or BRAKE recommendation, a known adjacent driving lane can be considered. The caller
must supply lane availability; the default is unknown, so no lane change is suggested. The
Webots adapter supplies only this world's three driving lanes, excluding the pedestrian lane,
and only while the ego is centred in a lane and not already overtaking.

A candidate lane is blocked by a relevant pothole, any track last observed over 0.5 s ago, or
traffic whose current-to-four-second projected position overlaps the maneuver corridor. The
corridor spans the ego lane through the destination lane, includes assumed vehicle widths,
and requires longitudinal clearance of `max(15 m, 2*v)`. This deliberately overestimates
occupancy; it is recommendation gating, not path planning or proof that blind spots are clear.

### UnifiedSafetyState (plain dictionary)

| Field | Meaning |
| --- | --- |
| `timestamp` | Simulation/sensor time, seconds |
| `overall_risk` | SAFE, CAUTION, HIGH or CRITICAL driving urgency |
| `primary_threat` | Null, or `{type: vehicle/road_hazard, id, reason}` |
| `recommended_action` | One Action string |
| `recommended_speed_kmh` | Finite target in `[0, nominal_speed_kmh]` |
| `recommended_lane` | Null, left_lane or right_lane; only with CONSIDER_LANE_CHANGE |
| `reason` | Explanation of the current recommendation |
| `vehicle_safety` | Existing Phase 4 timestamp, overall_risk, most_critical_track, minimum_ttc_s, active_conflicts |
| `road_safety` | relevant_hazards count, nearest_hazard id/null, dynamic hazards list |

Each dynamic hazard contains `hazard_id`, `type`, `severity`, `confidence`, `distance_m`,
`longitudinal_m`, `lateral_m`, `lane_relation`, `urgency`, `time_to_hazard_s` (null at rest),
`recommended_action`, `recommended_speed_kmh`, and `reason`. Its action describes the road
response in isolation; the top-level action is the single prioritized recommendation.

Required malformed data raises `ValueError`, rather than silently generating SAFE. The caller
is responsible for current, consistent sensor snapshots and sensor-health handling. Numbers
are finite and bounded at the decision boundary; `json.dumps(state, allow_nan=False)` works.
The map's world coordinates, observation counts and persistent records remain separate.

Run `venv/bin/python -m pytest tests/test_ego_safety.py -q` without Webots. See
[Phase 6 validation](PHASE6_VALIDATION.md) for observed demo sequences and performance.

## Run on macOS

From the repository root:

```sh
export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/share/sumo"
export PATH="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/bin:$PATH"
export WEBOTS_EXTRA_PROJECT_PATH="/Applications/Webots.app/Contents/projects/vehicles"
/Applications/Webots.app/Contents/MacOS/webots simulation/webots/worlds/roadsense_highway.wbt
```

Start Webots from a terminal like this. If you open it from Finder or the Dock, it doesn't see
`SUMO_HOME`, and the SUMO supervisor stops with "SUMO not found". In a Webots window started this
way, you can also open the world with **File → Open World…** →
`simulation/webots/worlds/roadsense_highway.wbt`.

SUMO uses TCP port 8873, so only one SUMO world can run at a time. Close any other SUMO world,
such as the original `highway_overtake.wbt`, first.

## Known harmless warnings

- `Module 'rtree' not available. Using brute-force fallback.`: from SUMO's `sumolib`, when the
  Python that runs the controllers lacks `rtree`.
- `Use of deprecated parameter lane in function moveToXY, use laneIndex instead.`: from the
  Webots SUMO supervisor running against SUMO 1.27.
- `UNSUPPORTED (log once): POSSIBLE ISSUE: unit 0 GLD_TEXTURE_INDEX_2D is unloadable …`: printed
  once at startup by the macOS OpenGL driver. Rendering works.

Don't edit Webots' own files under `/Applications/Webots.app` to remove these warnings.

## Limits

- The highway is 5 km long. `EGO_ROADSENSE` reaches its end after about 4¼ simulated minutes and
  keeps driving straight on the flat ground beyond it, as in the Webots sample.
- A vehicle hidden for longer than it can coast, behind another vehicle or in the strip alongside
  the car, gets a new track id when it reappears. The median guardrail hides most oncoming cars;
  one seen under it for less than 0.4 s is never confirmed, so never reported.
- Lane relation is only for this world's straight carriageway (see `CARRIAGEWAY_Y`).
- Constant-velocity prediction doesn't foresee braking, accelerating or the end of a lane change.
  A car merging into the lane is predicted to keep moving sideways, so for a moment it can seem
  to cross the lane rather than settle in it. The tracker's 1.5 s velocity window also makes a
  lane change's conflict start and end about half a second to a second late.
- A radar target has no size: a bus or truck is taken for a 5 m car, so its conflict comes a few
  tenths of a second late at highway closing speeds.
- The hazard camera reads the simulation's labels, so it never misses, invents or misjudges a
  pothole in view; positions are off by about 0.1 m (Webots places a recognized object at its
  bounding sphere's centre). Nothing is ever removed from the hazard map.
- Hazard lane relations use this world's straight carriageway, like the vehicles'.
