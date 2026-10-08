# RoadSense Digital Twin — Webots

Repository-owned Webots simulation for the RoadSense Digital Twin.

**Phase 1 is only the baseline simulation foundation:** a highway with SUMO traffic, taken from
the Webots `highway_overtake` sample. No RoadSense code runs in it yet: no RoadSense ego
controller, perception, hazard detection, telemetry or dashboard link.

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
```

These files are copied from the Webots `highway_overtake` sample (Cyberbotics Ltd., Apache
License 2.0). Only the world was edited: its name, title and info, and the tree-file path.
`controllers/` and `protos/` will be added with the first RoadSense controller or PROTO.

Everything else still comes from Webots and is not copied:

- **PROTOs** (cars, road, sensors, `SumoInterface`): `EXTERNPROTO` URLs pinned to Webots commit
  `838bb6f`. Webots downloads them once into `~/Library/Caches/Cyberbotics/Webots`, so the first
  launch on another machine needs internet.
- **Controllers:** `sumo_supervisor` (moves the SUMO traffic), `highway_overtake` (drives the
  main car and overtakes) and `radar_target_tracker` (draws the radar's targets as boxes).
  Webots finds the last one only through `WEBOTS_EXTRA_PROJECT_PATH`. Without it, Webots prints
  a "controller directory has not been found" warning and runs `<generic>` for that robot.
  The traffic and the overtaking still work.

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

Don't edit Webots' own files under `/Applications/Webots.app` to remove these warnings.
