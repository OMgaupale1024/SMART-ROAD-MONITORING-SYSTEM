# RoadSense Digital Twin — Webots

Repository-owned Webots simulation for the RoadSense Digital Twin: a highway with SUMO traffic
(Phase 1) and one RoadSense car, `EGO_ROADSENSE`, that drives itself and prints local telemetry
(Phase 2), the vehicles around it, seen by its own radars and tracked (Phase 3), and where those
vehicles are heading and how risky that is: predicted trajectories, time to collision and a risk
level (Phase 4). Not here yet: hazard detection, collision avoidance (nothing brakes or steers for
a risk), or any link to the RoadSense backend, database or dashboard.

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
controllers/roadsense_ego/
  roadsense_ego.py                   EGO_ROADSENSE's controller
  telemetry.py                       telemetry record and console line (no Webots imports)
  perception.py                      radar targets to detections, ego/world frames, lanes, track record
  tracking.py                        nearby-vehicle tracker
  prediction.py                      track trajectories (constant velocity)
  risk.py                            conflicts, time to collision, risk levels, ego safety state
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
