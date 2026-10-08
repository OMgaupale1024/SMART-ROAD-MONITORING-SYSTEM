# RoadSense Digital Twin — Webots

Repository-owned Webots simulation for the RoadSense Digital Twin: a highway with SUMO traffic
(Phase 1) and one RoadSense car, `EGO_ROADSENSE`, that drives itself and prints local telemetry
(Phase 2). Not here yet: perception, hazard detection, trajectory prediction, time-to-collision,
collision avoidance, or any link to the RoadSense backend, database or dashboard.

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
THIRD_PARTY_NOTICES.md               what comes from Webots, what changed, Apache-2.0 text
```

The world, the SUMO files, the tree file and `roadsense_ego.py` come from the Webots
`highway_overtake` sample. `THIRD_PARTY_NOTICES.md` lists them with their changes and license.

Everything else still comes from Webots and is not copied:

- **PROTOs** (cars, road, sensors, `SumoInterface`): `EXTERNPROTO` URLs pinned to Webots commit
  `838bb6f`. Webots downloads them once into `~/Library/Caches/Cyberbotics/Webots`, so the first
  launch on another machine needs internet.
- **Controllers:** `sumo_supervisor` (moves the SUMO traffic) and `radar_target_tracker` (draws
  the radar's targets as boxes). Webots finds the latter only through
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
