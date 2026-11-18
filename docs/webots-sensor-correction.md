# Webots sensor correction and experimental multi-sensor profile (Stages W8–W11)

Status: **development evidence for review, uncommitted.** This is not acceptance evidence: every recording here is a
development recording, kept in a scratch directory and never in a campaign. Labels as in
`docs/webots-sensor-diagnostics.md`:

- **[V] verified:** measured here, or read in the pinned Webots source (R2025b, `838bb6f`).
- **[H] hypothesis:** consistent with the evidence but not isolated.

Nothing here has changed any of the following:
- the highway world, `roadsense_ego`, the scenario generator, the trackers or the safety logic;
- the installed `/Applications/Webots.app` (its executable sha256 `1a1ac594…6fdce` is unchanged, and no file in it
  is newer than before the work);
- the Pi or the running service.

Stage 15 was not started. P1, P2 and P4 stay disabled by default. Ground truth is used only to score; nothing here
feeds RoadSense inference.

Environment: Apple Silicon Mac (macOS 27.2.1), Apple clang 21.0.0, SUMO 1.27.1.

---

## 1. Publication (W1–W7)

`803d991 test: investigate Webots radar geometry and sensor limitations` was pushed to `roadsense-v2-edge-ai`
together with the six local Phase 7A.3 commits before it (you chose "push all seven"). Before committing I checked
that the diff touched none of the following:
- the highway world;
- `roadsense_ego`;
- `src/roadsense`;
- the tests of the trackers or the safety logic.

The Stage 15 plan, its frozen hash (`51ffb16d…`) and its criteria are untouched. Stage 15 is deferred.

## 2. W8: the Webots source defect

### 2.1 Source and revision [V]

- **Installed:** `/Applications/Webots.app`, R2025b. `Contents/Resources/commit.txt` reads
  `838bb6fd3f56adf46579422651179f215ff570ce`. The build script refuses any other source revision.
- **Source:** `https://github.com/cyberbotics/webots`, at exactly that commit. It is a sparse, blobless fetch of
  `src/`, `include/`, `dependencies/`, `resources/` and `scripts/install/`. The submodules `src/glm` and `src/stb`
  are fetched at the commits the tree pins.
- **The defect:** `src/webots/maths/WbAffinePlane.hpp:72`.

  ```cpp
  WbVector3 vectorProjection(const WbVector3 &v) const { return v + normal() * v.dot(normal()); }
  ```

  A projection onto a plane through the origin is `v − n (v·n)`. With `+`, the result's distance to the plane is
  `2 v·n`: the point is pushed further away instead of onto the plane. `tests/test_webots_projection.py` checks this
  for 12 vectors and planes.

**All callers** (grep of the full `src/` tree at `838bb6f`):

| Caller | Use | Effect of the defect |
|---|---|---|
| `nodes/WbRadar.cpp:494` | Azimuth: the target's origin vector is projected onto the radar's horizontal plane. | The vertical offset leaks into the bearing: \|az\| = acos(x/√(x²+y²+4z²)), always away from the axis. This is the W3 finding. |
| `nodes/utils/WbObjectDetection.cpp:288, 295` | Frustum clipping of each bounding box: a corner outside a frustum plane is projected onto it. Used by **radar** and **camera Recognition**. | Corners are pushed out instead of in. The clipped box grows vertically, and its centre (the radar's measured range point, the camera's object position and size, and the target of the ONE_RAY occlusion ray) moves. |
| `sound/WbContactSound.cpp:196` | Velocity projection for contact-sound pitch. | Audio only. No sensor effect. |

**Separately, the reference point** (not a projection defect) [V]:
- The range is measured to the centre of the box clipped to the beam (`objectRelativePosition`).
- The azimuth and radial speed are measured along the vector to the target's **origin**: a car's rear axle, a
  truck's rear axle 5 m behind its cab.
- So the three numbers of one return describe two different points. Patch B (below) is the radar-only correction
  for that. It is kept separate from the minimal fix.

### 2.2 Patches

| Patch | File | Change |
|---|---|---|
| **A** (minimal) | `WbAffinePlane.hpp` | `+` → `−` (one character; `tools/webots_w8/A-vectorProjection.patch`) |
| **B** (radar reference; applied on top of A) | `WbRadar.cpp` | Bearing `−atan2(y, x)` and radial speed both along the clipped-box centre, the point the range already measures (`B-radar-reference.patch`) |

No upstream PR was opened.

### 2.3 Build and provenance [V]

`tools/webots_w8/build.sh <work dir> none|A|AB`:
- Builds `src/glad`, `src/wren` and the arm64 half of `src/webots` from the pinned source with the patch applied.
- Then copies the installed application into the work directory and replaces only `Contents/MacOS/webots`
  (ad-hoc signed).
- Qt frameworks are symlinked at the copy's root: aqtinstall's frameworks use `@rpath` install names. The rpath is
  `@loader_path/../..`.
- Each build is a clean rebuild of `src/webots` (612 compilations, about 50 s). Per-architecture objects don't track
  header dependencies, so an incremental "none" build silently kept patch A.

**Toolchain:** Apple clang 21.0.0, `make -j` on the system. Problems on Apple Silicon were small and are solved in
the script:
- submodules missing from a sparse fetch;
- `WEBOTS_HOME` needed at link time;
- Qt install names;
- codesign refusing an unsealed bundle root.

| Dependency | Source | Check |
|---|---|---|
| Qt 6.5.3 `clang_64` + qtwebsockets | Official Qt mirrors via aqtinstall 3.3.0, as Webots' `scripts/install/qt_mac_installer.sh` does | aqtinstall's own SHA-256 checks |
| `assimp-5.2.3.tar.bz2` | cyberbotics.com/files/repository/dependencies/mac/release | MD5 `3d4a0297ef7636dc00a1ddc62ff062e3` (from `dependencies/Makefile.mac` at `838bb6f`) |
| `freetype2.tar.bz2` | same | MD5 `810286fe3f188f7ae7dca18aef0d75a1` |
| `libOIS-1.3.tar.bz2` | same | MD5 `84c5b0c18031bcf13977165ade137782` |
| `libpico-1.0.tar.bz2` | same | MD5 `7354aa3a597b1703671372a1a6b9c2fd` |
| `libode`, `libassimp`, `libOIS`, `libpico` dylibs | Copied from the installed application | (same binaries) |

| Build | Applied diff sha256 | `webots` executable sha256 |
|---|---|---|
| installed (baseline) | — | `1a1ac594809f854f4bb228d2f53e98357b7e4ee6f3e847d3ea0691370ff6fdce` |
| none (control) | `e3b0c442…` (empty) | `d1293edabf76b94bfdb50a480bd0f961c36d9e6d4390acd8690b626f27c07efc` |
| A | `7e0c8322ae621ae5367d2dce1de59953f04213a2e42ef2136fcccc77c6f4127d` | `7979648669873f832ed38e3c16ca0767288c66154cfb45d3a0b8f5a661e6f109` |
| AB | `6b82949e2b98beec53966f93b307ba5faf6d397167a3c5b9cd5c48aed4117b08` | `0c5b9bee64fed13cfa5b2db1dbdeb1e85206eaa95132ae93a75755b61b728ca4` |

- **Reproducibility:** the binaries are not bit-for-bit reproducible, because a compile timestamp is embedded. The
  applied diffs are (`docs/evidence/webots-w8/applied-*.diff`).
- **Location:** the built applications live in the scratch directory only. They are not committed and not installed.
- **The installed build stays the baseline:**
  - `none` reproduces it except for the ego's own-body return. That return is numerically ill-conditioned and
    appears or vanishes per build.
  - The sign of the azimuth exactly dead ahead also varies, because it comes from float noise.
  - Because those extra returns consume random numbers, the noisy values differ between builds. Variants are
    therefore compared statistically, not sample by sample.

### 2.4 Reproduction and tests [V]

`tests/test_webots_projection.py` has 142 cases. They cover:
- plane projection (fixed lies on the plane; installed is at 2 v·n);
- positive and negative vertical offsets at 3 m and 40 m (the leak is the same for +dz and −dz, and always away from
  the axis);
- two mounting heights (0.142 m and 0.004 m) on the front radar, plus the rear radar;
- four target orientations;
- short and long distances;
- the truck range residual from vertical clipping;
- the measured fixture.

The fixture is 96 measured noise-free returns, 24 per build (`docs/evidence/webots-w8/measured-fixture.json`). The
Python port reproduces each build's own measurements to ≤ 1 mm and 0.01°, and at least 5 installed returns are
inconsistent with the patched model and vice versa.

### 2.5 Original vs patched measurements [V]

These come from the same 312-case laboratory plan on each build (`docs/evidence/webots-w8/radar-variants.json`).
Original front and rear radars, RoadSense's transform. Errors are p50/p95 in metres against the target body's
centre (the corrected reference); the historical reference is the SUMO-style wrapper.

| Group | Installed | A | AB |
|---|---|---|---|
| Stationary 0–10 m, lateral | 0.48 / 1.17 | 0.01 / 1.03 | 0.01 / 0.27 |
| Stationary 0–10 m, longitudinal | 0.42 / 1.70 | 0.26 / 1.47 | 0.15 / 0.48 |
| Both stopped, truck, longitudinal | 1.04 / 1.72 | 0.17 / 0.50 | 0.19 / 0.56 |
| Alongside car, lateral (seen rate) | 0.58 / 0.92 (0.50) | — | 0.10 / 0.37 (0.62) |
| Orientation car, lateral (lane agreement) | 0.52 / 1.42 (0.643) | 0.52 / 1.39 | 0.02 / 0.09 (1.0) |
| Orientation car, historical reference, lateral | 0.93 / 2.87 | — | 0.50 / 1.49 |
| Static truck, lateral | 0.39 / 2.49 | — | 0.05 / 0.14 |
| Occlusion truck, lateral (seen 0.5 in all) | 1.16 | — | 0.05 |

**Where it gets worse:**
- With the corner radars on AB, some p95 values rise:
  - both-stopped car, lateral p95 0.85 (0.03 on A);
  - stationary 30–60 m, lateral p95 0.63;
  - stopped truck, lateral p95 0.86.
- Cause [V]: a corner radar sees only part of a car alongside. AB reports the centre of the visible part,
  correctly, but that is not the body's centre.
- On the installed build the same partial view is hidden by the origin bearing, which happens to point near the
  body.
- The own-body front return rate goes 0.68 / 0.77 / 0.72 (installed / A / AB).
- With corners, motorcycle occlusion stays at 0.5 seen in every build.

**Other effects of A** [H, not measured here]:
- camera Recognition sizes and positions of objects cut by the image border;
- the ONE_RAY occlusion target.

RoadSense's hazard camera reads Recognition for potholes. Its results near the image border would change under a
patched build.

## 3. W9: the experimental sensor profile (opt-in)

`tools/sensor_profile.py run` builds a **generated copy** of the highway world into a run directory. The repository's
world, network, controllers and the stock `sumo_supervisor` are only read; the test
`test_profile_world_is_a_generated_copy_with_the_profile_robot_in_the_ego` checks that.

### What the copy contains

- **The highway world, unchanged:**
  - RoadSense's own ego, `roadsense_ego`, its front and rear radars and its hazard camera;
  - one scenario: Phase 6C generator, family `normal`, seed 1, left-hand traffic, the Indian vehicle mix.
- **A nested `Robot` "roadsense sensor profile"** in EGO_ROADSENSE's `sensorsSlotCenter`, whose frame is the ego
  frame. It carries its own sensors and runs its own controller, `roadsense_sensor_recorder`, so it never reads or
  changes the ego's devices.
- **The scenario's SUMO plugin, extended:** `tools/sensor_profile_plugin.py` subclasses the Phase 6C plugin. It
  adjusts the running stock supervisor through Webots' per-world plugin hook. No supervisor copy shadows the stock
  one.

### Sensor extrinsics

Ego frame: origin at the rear-axle centre (where `telemetry gps` sits), x forward, y left, z up. Metres and radians.

| Sensor | x | y | z | yaw | Range | Horizontal FOV | Vertical FOV | Notes |
|---|---|---|---|---|---|---|---|---|
| profile radar front | 4.054 | 0 | 0.142 | 0 | 150 | 2.6 | 0.0785 | Exactly RoadSense's `radar front`: same parameters and noise (range 0.25 m, speed 0.12 m/s, angle 0.001 rad), occlusion on |
| profile radar rear | −1.16 | 0 | 0.33 | π | 100 | 2.6 | 0.0785 | Exactly `radar rear` |
| profile radar corner fl / fr | 3.85 | ±0.85 | 0.142 | ±0.785 | 80 | 1.6 | 0.25 | Experimental |
| profile radar corner rl / rr | −1.1 | ±0.85 | 0.33 | ±2.356 | 80 | 1.6 | 0.25 | Experimental |
| profile lidar roof | 1.11 | 0 | 1.51 | 0 | 0.5–100 | 2π (1800 columns) | 0.5236 (16 layers) | Noise 0.0003 of range. Beam elevations calibrated: ±0.2653 rad in 0.03537 steps (`lidar_geometry.ROOF16_ELEVATIONS`) |
| profile camera 720p | 1.61 | 0 | 1.16 | 0 | 0.5–300 | 1.05 | — | 1280×720, recording only; no Recognition |
| profile gps, profile imu | 0 | 0 | 0 | — | — | — | — | — |

**Timestamps and units:**
- Every record carries Webots simulation time `t` (s). Radars refresh every `--radar-ms` and the lidar every 100 ms.
- A Webots lidar captures all beams at the same instant (`scan_s = 0`).
- The radar gives range in m, azimuth in rad (positive to the right, as Webots reports it) and range rate in m/s
  (negative = closing).

**Recording:**
- `records.jsonl` holds raw radar targets, GPS/IMU and the ego pose. For scoring only, it also holds the pose,
  velocity and model type of every SUMO vehicle node within 150 m.
- `lidar.bin` holds raw float32 range images.
- `camera_*.png` holds camera frames, when `--camera-s` > 0.
- `sumo_state.jsonl` holds SUMO's own state at each step, with both clocks.

**Not added:**
- No `Recognition` anywhere in the profile.
- No proximity sensors: the ego already has eight `DistanceSensor`s, and the blind-zone test below needs nothing
  shorter-range than the lidar.
- The four IbeoLux units are not duplicated. W5 already compared them with the roof lidar in the calibration lab
  (`docs/evidence/webots-w/lidar-analysis.json`):
  - The union of the four saw an alongside car 1.0, a motorcycle 0.88 and a truck 1.0, but a static motorcycle only
    0.60.
  - The front units never see behind, and the rear units never see ahead.
  - The four together cost 37 ms of wall time per simulated second (the roof unit: 20 ms) and produce 0.54 MB/s.

  The roof unit covers 360° from one mount and is the one evaluated here.

### 3.1 Autorickshaw and the geometry of simulator truth [V]

**Autorickshaw model.** `simulation/webots/protos/RoadSenseAutorickshaw.proto` is RoadSense's own model, built from
primitives with no external assets.
- Size: 2.8 × 1.4 m, the same as the scenario generator's SUMO vType, and 1.75 m tall.
- One bounding box for the whole body.
- `radarCrossSection` 30 m². This is an assumption between Webots' motorcycle (10) and car (100) values, not a
  measurement.
- Origin at the rear axle, as in Webots' models. Three wheels.

The plugin draws every SUMO vehicle of type `autorickshaw` with this model, in its own vehicle pool, instead of a
random 4.2–6.6 m car.

**Every stock model was misplaced, not only the autorickshaw.** `sumo_supervisor` puts the wrapper at SUMO's
vehicle centre and the model a fixed distance behind it per class: car 2.85 m, truck 5.2, bus 4.6, motorcycle 1.7.
Measured on the drawn bounding bodies in the highway (`original-baseline` run):

| Model | SUMO class, length | Drawn length | Drawn body centre vs SUMO centre (stock) | Profile |
|---|---|---|---|---|
| ToyotaPriusSimple | car, 4.5 | 4.2 | −1.45 m | 0.00 |
| BmwX5Simple | car, 4.5 | 4.5 | −1.50 | 0.00 |
| CitroenCZeroSimple | car, 4.5 | 3.3 | −1.60 | 0.00 |
| LincolnMKZSimple | car, 4.5 | 4.93 | −1.41 | 0.00 |
| RangeRoverSportSVRSimple | car, 4.5 | 4.52 | −1.60 | 0.00 |
| TeslaModel3Simple | car, 4.5 | 4.5 | −1.48 | 0.00 |
| MercedesBenzSprinterSimple | car, 4.5 | **6.62** | −1.15 | 0.00 |
| TruckSimple | truck, 8.0 | 7.7 | −2.05 | 0.00 |
| BusSimple | bus, 11.0 | **9.73** | −3.14 | 0.00 |
| MotorbikeSimple | motorcycle, 2.0 | 2.48 | −0.91 | 0.00 |
| ScooterSimple | motorcycle, 2.0 | 1.95 | −1.08 | 0.00 |
| autorickshaw (stock: a random car) | autorickshaw, 2.8 | 3.3–6.6 | −1.15 to −1.60 | RoadSenseAutorickshaw 2.8, 0.00 |

**What the Phase 6C ground truth gets wrong.** It reconstructs each vehicle as the wrapper plus half the *SUMO*
length. For a car ahead of the ego, the drawn rear bumper the sensors see is about 1.3 m closer than that truth says.
- So SUMO-based labels **under**-state collision risk for vehicles ahead.
- They over-state it for vehicles behind.

**The profile's fix** (`ROADSENSE_PROFILE_ALIGN_BODIES`, on by default in the profile only): each model is shifted so
its body centre sits on the wrapper. All measured offsets are then 0.00.
- **Not fixed:** the length mismatch (Sprinter 6.6 m drawn for a 4.5 m SUMO car; bus 9.7 for 11).
- **Hence the scoring reference.** Everything below is scored against the **drawn bounding body**, the object the
  sensors actually observe. The SUMO centre is reported separately.

### 3.2 Camera feasibility (recording only)

- **Cost:** the 720p camera at 10 Hz works, but costs about 35% of real-time factor (1.36 → 0.88 with the lidar on).
- **Data:** about 1.0 MB per PNG frame, so 10 MB/s, or 217 MB for 20 s simulated.
- **Content:** frames show the road, traffic and the new autorickshaw, a yellow box with a green canopy. It is
  plainly a primitive model.
- **What it is not:** no detector was run, trained or sized from pixels, and nothing here claims a camera detection
  range. The camera stays recording-only.

## 4. W10: SUMO/Webots synchronisation

### 4.1 What the stock coupling does [V]

**The common clock.** `sumo_supervisor` mirrors the ego into SUMO before each SUMO step. So SUMO's state for SUMO time
*s* is the traffic around the ego as it was at Webots time *s*: SUMO time = Webots time.
- Measured: `webots_t − sumo_t` = one step when the state is read.
- Separately, the mirrored ego sits +3.0 m along in SUMO at 22 m/s. It is moved with `moveToXY` from its rear axle,
  with a speed computed over a hard-coded 0.2 s [H on the cause].

**But SUMO's state for *s* only exists from Webots time *s* + step.** The stock supervisor then:
- puts each car at the pose of the step **before**;
- moves it toward the new pose with `setVelocity`.

So **Webots shows SUMO time t − 2 steps** (measured lag p50 0.400 s at 200 ms, 0.200 s at 100 ms, 0.100 s at 50 ms).
At highway speed that is **6.5 m along at the default 200 ms step**, which corrects the "one-step lag" stated in W6.
The angular velocity it sets also has the wrong sign: `current − target`, read in source.

**Lane changes.** SUMO changes lanes instantly, while Webots smooths them over `laneChangeDelay` (4 s here). The body
the sensors see therefore lags SUMO laterally (cross p95 0.14–0.67 m, with single excursions to 3.6 m). Truth
tied to SUMO's lateral position is wrong during lane changes; truth from the drawn body is right by construction.

### 4.2 Sync modes (`ROADSENSE_PROFILE_SYNC`, profile only)

| Mode | What it does | Result |
|---|---|---|
| `stock` | Webots' behaviour | Shows t − 2 steps |
| `current` | Starts each step at the newest pose | t − 1 step |
| `aligned` | Newest pose extrapolated one step | No lag, but the car **jumps** by the extrapolation error every step. Webots' radar differences translations, so the jumps double the range-rate error |
| **`smooth`** | Aligned without jumps: the car is left where it is and given the velocity that reaches the pose SUMO implies one step later; teleports beyond 5 m only for newly assigned cars | No lag, continuous motion |

**Measured vs physical reference.** Each mode was measured over 60 s simulated: one scenario, the same seed, the
installed Webots, the profile on. There are about 4,000–17,000 vehicle samples per run. Pose = wrapper vs SUMO's
state at the same time; range rate = the front radar vs the true radial velocity of the matched body.

| Config | Radar / SUMO | Sync | RTF | CPU s | Lag s | Along error m p50 / p95 | Front range-rate error m/s p50 / p95 |
|---|---|---|---|---|---|---|---|
| **A** (original) | 200 / 200 | stock | 2.15 | 40 | 0.40 | 6.50 / 7.20 | 0.08 / 0.28 |
| A | 200 / 200 | aligned | 2.19 | 43 | 0.00 | 0.02 / 0.08 | 0.19 / 0.61 |
| A | 200 / 200 | smooth | 2.17 | 44 | 0.00 | 0.02 / 0.08 | 0.08 / 0.31 |
| **B** | 100 / 200 | stock | 2.22 | 43 | 0.40 | 6.50 / 7.20 | 0.08 / 0.27 |
| B | 100 / 200 | aligned | 2.24 | 43 | 0.00 | 0.02 / 0.08 | 0.14 / **0.92** |
| B | 100 / 200 | smooth | 2.07 | 47 | 0.00 | 0.02 / 0.08 | 0.08 / 0.26 |
| **C** | 100 / 100 | stock | 1.41 | 70 | 0.20 | 3.26 / 3.62 | 0.09 / 0.26 |
| C | 100 / 100 | current | 1.42 | 70 | 0.10 | 1.63 / 1.81 | 0.09 / 0.28 |
| C | 100 / 100 | aligned | 1.36 | 73 | 0.00 | 0.01 / 0.02 | 0.11 / 0.38 |
| **C** | **100 / 100** | **smooth** | **1.31** | **75** | **0.00** | **0.01 / 0.02** | **0.09 / 0.27** |
| **D** | 50 / 50 | stock | 0.81 | 122 | 0.10 | 1.64 / 1.81 | 0.08 / 0.24 |
| D | 50 / 50 | aligned | 0.79 | 125 | 0.00 | 0.00 / 0.01 | 0.10 / 0.28 |
| D | 50 / 50 | smooth | 0.81 | 123 | 0.00 | 0.00 / 0.01 | 0.09 / 0.25 |

**Fresh measurements and interpolated poses:**
- With `stock`, every radar sample sees an interpolated, two-step-old pose: there is no fresh instant.
- With `smooth` at 200 ms, samples on a SUMO step and halfway between have the same error: total p50 0.03 vs
  0.02 m, p95 0.18 vs 0.20 m. So the distinction disappears.
- At 100 ms and 50 ms every radar sample falls on a step. That is also why `aligned` and `smooth` have identical
  pose errors there and differ only in range rate.
- Range-rate error on a target's **first** refresh is ordinary (p95 0.2–0.6 m/s) unless the SUMO node was just
  reassigned from its parking position. Then it reaches 10⁵ m/s (§6, `velocity_unreliable`).

**High speed:** the along-track error above 15 m/s relative speed matches the overall figure in every mode (it scales
with speed only in `stock`).

**Resources** (whole Webots process with SUMO, profile on, 60 s simulated):
- Max RSS is about 1.4 GB in every config.
- CPU is 40 s (A) to 123 s (D).
- Going from 100 ms to 50 ms costs 38% of real-time factor for 0.01 m of position error.
- The scheduling constraint is met everywhere: the radar period is a multiple of `basicTimeStep` (10 ms), and SUMO's
  step equals or is a multiple of the radar period.

### 4.3 Choice

**C, 100/100 with `smooth`.** It has:
- no observation lag;
- 0.02 m p95 position error, 4× better than 200 ms smooth;
- range-rate error as good as stock;
- 10 Hz radar.

That costs about 40% of real-time factor against today's 200/200. **D** gains 0.01 m for another 38%, which is not
worth it. B (radar faster than SUMO) only works with `smooth`; with `aligned` it is the worst config.

## 5. W11: LiDAR geometry processing (no fusion, no learning)

`tools/lidar_geometry.py` (numpy only, no simulator imports) processes one range image as follows:
1. **Transform:** calibrated beam elevations and Webots' column convention give ego-frame points.
2. **Ground removal:** estimate the ground height as the median of the lowest four beams' returns 3–25 m out (one
   flat plane, marked `ponytail:`), then keep 0.2–4.0 m above it. That drops the road, gantries and sign faces.
3. **Ego removal:** drop points inside the ego footprint plus a 0.3 m margin.
4. **Clustering:** 8-neighbour connected components on a 0.4 m grid.
5. **Per cluster:**
   - the near point (closest to the ego's footprint, which is what clearance depends on);
   - its x/y/z extent (visible surfaces only: a lower bound on the body);
   - the point count and a confidence;
   - the 1-σ lateral uncertainty of the near point from the beam spacing there;
   - `unseen_vertical_gap` (the vertical gap between layers at that range);
   - `near_range_interval`, which is new and explained below.

`tools/sensor_profile.py lab-lidar` re-runs the W4/W5 laboratory cases through it.

### 5.1 The truck's 4.6 m near-face error [V]

| Truck centre ahead | W4 scoring (first cluster on the body) | All clusters on the body | Clusters on the body | Cause |
|---|---|---|---|---|
| 10.7 m | 2.59 m | 0.01 m | 3 | Truck fragments; W4 took the cab |
| 15.7 m | 4.60 m | 0.01 m | 4 | Same |
| 27.7 m | 4.59 m | 0.01 m | 2 | Same |
| 57.7 m | 4.56 m | 0.01 m | 2 | Same |
| 37.7 m (`lidar_occlusion` case) | 4.57 m | 4.57 m | 1 | A nearer vehicle hides the chassis; that occluder is the obstacle and is detected |
| **97.7 m** | 4.80 m | **4.80 m** | 1 | **Real.** The chassis (0.45–1.25 m above the road) lies between two beams, 0.13 m and 3.5 m high there |

**Two causes, separated:**
- **Scoring:** most of the 4.6 m was the scoring. TruckSimple's flat chassis runs 4.5 m behind the cab, and the
  roof lidar sees its rear end and the cab's back wall as separate clusters.
- **Sensing:** the remainder is real, and it is also in highway traffic. Truck near-face error p95 is **4.6 m at
  20–80 m** (C-aligned run, all clusters on the body), detected 77% at 40–80 m.
- **Direction:** the error is always too far, so it is optimistic. Uncorrected, it would under-state collision risk.

**What is reported now.** Clusters are never merged into one "vehicle" box, so the nearest obstacle point survives
fragmentation. Each cluster also reports `near_range_interval`:
- In every column through the visible face whose lowest return is above the road, the beam below reached the road
  somewhere in front.
- Nothing lower than the next beam was observed between there and the face, so the face may be that close.
- On the laboratory cases dead ahead (no occluder), the interval contains the true near face on 83 of 96 frames.
- Close up it is tight: 5.66–5.66 m at 9 m; 20.86–23.05 m at 25 m. At long range it is wide: 34.8–59.7 m for a car
  at 63 m.

**Misses** are the 97.7 m truck, the occlusion case and three motorcycle frames:
- A raised structure with ground clearance (the truck chassis) can sit entirely between two beams. No
  geometry-only rule can bound it.
- `unseen_vertical_gap` reports that blindness. It is about 0.035 × range: 1.3 m at 38 m, 3.5 m at 98 m.
- So beyond about 25 m, a structure under 0.8 m thick at an arbitrary height can be missed.

### 5.2 Highway results (C-aligned run, 600 frames; drawn bodies within 80 m)

Near face, near side and clearance: positive = the lidar reports the object farther than its body (optimistic).
Each cell is p50 / p95.

| Class (model) | Range | Detected | Near face, m | Near side, m | Clearance error, m | Fragmented |
|---|---|---|---|---|---|---|
| Car (BMW X5) | 0–20 | 1.00 | −0.16 / −0.03 | −0.18 / −0.05 | −0.19 / −0.04 | 0.05 |
| Car (BMW X5) | 20–40 | 1.00 | −0.04 / 1.27 | — | −0.04 / 1.27 | 0.32 |
| Car (BMW X5) | 40–80 | 0.99 | −0.03 / 0.98 | — | −0.03 / 0.97 | 0.03 |
| Autorickshaw | 0–20 | 1.00 | −0.01 / 0.00 | −0.00 / 0.01 | −0.01 / 0.01 | 0.24 |
| Autorickshaw | 40–80 | 0.96 | −0.00 / 0.01 | — | −0.00 / 0.05 | 0.30 |
| Motorbike | 0–20 | 1.00 | 0.11 / 0.79 | 0.01 / 0.24 | 0.10 / 0.60 | 0.05 |
| Motorbike | 20–40 | 0.80 | 0.40 / 0.89 | — | 0.43 / 0.90 | 0.08 |
| **Motorbike** | **40–80** | **0.47** | 0.86 / 1.51 | — | 0.90 / 1.53 | 0.01 |
| Scooter | 40–80 | 0.41 | 0.51 / 0.77 | — | 0.52 / 0.78 | 0.02 |
| Truck | 0–20 | 1.00 | 0.01 / 0.03 | −0.02 / 0.04 | −0.02 / 0.04 | 0.60 |
| **Truck** | **20–40** | 1.00 | 0.02 / **4.60** | — | 0.04 / 4.07 | 0.48 |
| **Truck** | **40–80** | 0.77 | 0.21 / **4.60** | — | 0.20 / 4.35 | 0.58 |
| Bus | 40–80 | 1.00 | −0.06 / −0.01 | — | −0.07 / −0.03 | 0.48 |

- **Stopped vehicles:** the laboratory cases are all static (§5.1, and W5).
- **Adjacent lanes:** the near-side error alongside is ≤ 0.24 m at p95 for every class.
- **Motorcycles:** the lidar's weak class. Beyond 40 m, fewer than half the frames see one, and the near face is
  read about 0.9 m too far, because the low front wheel falls between layers. The radar keeps seeing motorcycles to
  about 100 m.
- **Blind-zone occupancy:** the left zone runs x −6…4.5 m and y 1…4.5 m beside the ego; it was occupied on 92 of 600
  frames.
  - The roof lidar's recall was 1.00, with false alarms 0.002.
  - The original radars' recall was 0.37.
  - All six radars reached 0.44 (self-clutter flagged, §6).
  - No vehicle used the right zone in this scenario.

### 5.3 Compact binary format (`tools/lidar_geometry.py`, version 1)

There are three message types:
- `RSLC`: the sensor configuration, sent once.
- `RSLF`: a range-image frame.
- `RSLO`: a compact object list.

The configuration holds:
- the sensor name and its mount extrinsics (6 × float32);
- width, layers, FOV, column azimuth model, minimum and maximum range;
- the range encoding (float32 metres, or uint16 at a scale with 0 = no return);
- **the calibrated elevation of every beam**.

Its `config_id` is the CRC-32 of that body, and every frame names it, so a frame cannot be decoded against the
wrong beam table.

A frame carries:
- a sequence number;
- the capture time (float64 s) and the scan duration;
- flags for zlib and a validity bitmask;
- the payload: the range image, plus a packed bitmask of valid returns when float32.

The round trip is tested for every encoding (`tests/test_lidar_geometry.py`, 8 tests). No verbose JSON is involved.

**Bandwidth** for one roof-lidar frame, measured on the highway recording (10 Hz):

| Representation | Bytes per frame | MB/s | Mbit/s |
|---|---|---|---|
| Raw points, xyz float32 (20.4 k valid) | 244,812 | 2.45 | 19.6 |
| Range image, float32 | 115,200 | 1.15 | 9.2 |
| `RSLF` float32 + validity mask | 118,832 | 1.19 | 9.5 |
| `RSLF` float32 + mask, zlib | 64,992 | 0.65 | 5.2 |
| **`RSLF` uint16 cm, zlib** | **19,727** | **0.20** | **1.6** |
| `RSLO` clusters (p50 1,564; max 2,070) | ~1,600 | 0.016 | 0.13 |
| `RSLC` config (once) | 145 | — | — |

**This corrects W5's conclusion** that raw lidar to the Pi "is not viable; it would have to be clustered on the Mac
side":
- A 1 cm quantised, compressed range image is 1.6 Mbit/s, about 12× the object list.
- Its error is ≤ 5 mm, below the sensor's own noise (0.0003 × range: 3 cm at 100 m).
- It carries everything needed for edge-side processing.

**Recommendation:**
- The simulator generates range images and ships `RSLF` uint16 + zlib.
- Perception (this module or its successor) runs on the edge device, so the same code would run on a real sensor's
  range image.
- `RSLO` is a fallback when the link can't carry 1.6 Mbit/s.

**Processing time:** 13.6 ms p50 and 18.0 ms p95 per frame on the Mac (Python and numpy, single thread). No Pi was
benchmarked, as the restrictions require. On the Pi 5 it is expected to stay well inside the 100 ms frame, but that
is not measured [H].

## 6. Radar uncertainty and ambiguous measurements (proposal; `tools/radar_measurement_model.py`)

Nothing here changes `perception.radar_detections`, a tracker or the safety logic. The helper is evaluated offline
only.

### 6.1 What one Webots return means

| Quantity | Physical reference | Error model |
|---|---|---|
| Range | Centre of the part of the bounding body inside the beam (all builds) | Gaussian, 0.25 m |
| Bearing | Installed: the body's **origin** (rear axle), plus the vertical leak. AB: the same point as the range | 0.001 rad, plus the reference-point offset |
| Range rate | Installed: along the origin direction. AB: along the measured point | 0.12 m/s noise. Garbage on the first refresh of a reassigned SUMO node (up to 1.9 × 10⁵ m/s) |

**None of these is the near face.** On the highway, the clearance a return implies exceeds the true clearance to
the body by 1.8 m at p50 and 3.9 m at p95 (front radar, every build; rear radar 2.2 / 3.6). Reading a radar point as
the obstacle's surface therefore under-states collision risk by about half a vehicle length.

### 6.2 Proposed representation: keep every return, with flags and intervals

| Flag / field | Rule (no simulator dimensions, no identities) | Highway evidence (C-aligned, vehicle / no vehicle) |
|---|---|---|
| `self_suspect` | Matches the radar's self-clutter signature, measured target-free (§6.3): range interval, the clamp side, \|speed\| < 1 m/s | Front radar 8 / 329; rear 6 / 101; corner fl 58 / 542; corner fr 0 / 600 |
| `range_clamped` | d ≤ minimum range: the range is the interval [0, min_range] | Always with `self_suspect` here |
| `bearing_clamped` | \|az\| ≥ FOV/2 − 5σ: the bearing is one-sided, [edge, beyond] | Front radar 18 / 0; rear 9 / 1. **RoadSense drops all of these today** (`EDGE_MARGIN_RAD`), and they are real vehicles |
| `velocity_unreliable` | First refresh of a target, NaN, or \|v\| > 100 m/s: the position is kept, the range rate set to None | Front radar 37 / 0 (all real) |
| `near_face_slack_m` | 6 m, class-agnostic (half a 12 m vehicle): the surface may be up to this much closer than the point | — |

Self-suspect returns that had a real vehicle nearby (front 8, rear 6, corner fl 58):
- In each case a vehicle's body was within the 2.5 m matching distance of the self-clutter point: close behind or
  close ahead in traffic.
- Whether the return came from that vehicle or from the ego's own body can't be told from the return alone.

Because the return is flagged, not dropped, another sensor (the lidar near point) can confirm or rule it out. A
suspect that is the only evidence of an obstacle would still reach a tracker.

### 6.3 Self-clutter calibration [V]

There were 900 radar samples with no target anywhere (laboratory, ego at 0, 10 and 25 m/s), on the installed and AB
builds.

| Radar | Present | Range | Bearing | Speed |
|---|---|---|---|---|
| radar front | 0.59–0.63 | 1.00–1.52 (minimum-range clamp) | ±1.30 (edge) | ≈ 0 |
| radar rear | 0.19 | 1.00–1.06 | ±1.30 | ≈ 0 |
| corner fl / fr | **1.00** | 1.7–3.6 | inner edge (∓0.80) | ≈ 0 |
| corner rl / rr | 0 | — | — | — |

The front corner radars see the ego's own front on **every** sample at a range a real vehicle can occupy (1.7–3.6 m),
not at the 1 m clamp. With them installed, the corners' false alarms in the blind zones were 1.0 before flagging,
and 0.002 after.

**Recommendation:** mount the front corners further out, or narrow their inner FOV, before trusting them.

## 7. Validation: simulator × sensors (bounded)

**Setup:**
- Three 60 s runs of the same scenario (normal, seed 1, left-hand traffic):
  - `original-baseline`: installed Webots, 200/200, stock sync, stock models;
  - `C-smooth`: installed Webots, 100/100, smooth, profile models;
  - `C-smooth-AB`: patched AB build, same settings.
- The "original sensors" are the profile's exact copies of RoadSense's front and rear radars. The "upgraded" sensors
  add the four corner radars and the roof lidar.
- Each run records both sensor sets at once, so the four combinations come from three runs.

**Error definitions.** Errors are p50 / p95 in metres. Each return goes through RoadSense's own transform (mount
plus bearing) and is compared with:
- **corrected:** the drawn body's centre, the object the sensors observe;
- **SUMO:** SUMO's vehicle centre at the same time, the historical definition (it includes the lag and the body
  offset);
- **clearance optimism:** the clearance the return implies minus the true clearance to the body. Positive = the
  obstacle looks farther than it is.

| Simulator | Sensors | Radar | Lon, corrected | Lat, corrected | Lon vs SUMO (historical) | Clearance optimism |
|---|---|---|---|---|---|---|
| Original (installed, 200/200 stock, stock models) | Original | front | 0.19 / 0.64 | 0.18 / 0.71 | **7.84 / 9.01** | 2.24 / 3.98 |
| | | rear | 0.20 / 0.74 | 0.10 / 0.51 | 7.87 / 8.72 | 2.21 / 3.28 |
| Installed, 100/100 smooth, profile | Original | front | 0.19 / 0.68 | 0.19 / 0.72 | 0.19 / 0.68 | 1.68 / 3.87 |
| | | rear | 0.19 / 0.90 | 0.10 / 0.45 | 0.19 / 0.90 | 2.04 / 3.65 |
| | Upgraded | corner rl | 0.18 / 0.91 | 0.14 / 0.66 | 0.18 / 0.90 | 1.95 / 3.66 |
| | | corner fl (61 matched, rest self-clutter) | 1.98 / 4.67 | 0.53 / 0.96 | 1.96 / 4.67 | 0.29 / 1.92 |
| **Patched AB**, 100/100 smooth, profile | Original | front | 0.16 / **0.50** | **0.06 / 0.22** | 0.16 / 0.50 | 1.66 / 3.83 |
| | | rear | 0.17 / 0.54 | 0.05 / 0.20 | 0.17 / 0.55 | 1.65 / 2.56 |
| | Upgraded | corner rl | 0.19 / 0.60 | 0.04 / 0.17 | 0.19 / 0.60 | 1.60 / 2.56 |
| | | corner fl | 1.98 / 4.12 | 0.65 / 1.06 | 1.97 / 4.12 | −0.06 / 0.42 |

| Configuration | Left blind zone recall (false alarm) | Lidar near face, truck 20–80 m (p95) | Lidar motorcycle detected 40–80 m |
|---|---|---|---|
| Original simulator, original radars | 0.38 (0.000) | — | — |
| Original simulator, + corners + lidar | radars 0.38, lidar 1.00 (0.000) | 4.61 m | 0.41 |
| Installed, smooth, original radars | 0.38 (0.000) | — | — |
| Installed, smooth, upgraded | radars 0.42, lidar 1.00 (0.002) | 4.60 m | 0.45 |
| Patched AB, smooth, original radars | 0.40 (0.000) | — | — |
| Patched AB, smooth, upgraded | radars 0.50, lidar 1.00 (0.004) | 4.60 m | 0.46 |

**What improves:**
- Sync and the body fix remove 6.5–7.8 m of error against SUMO: the historical metric was dominated by the
  simulator, not the sensor.
- Against the body, AB cuts the original radars' lateral p95 three-fold (0.72 → 0.22 front, 0.45 → 0.20 rear), and
  longitudinal p95 from 0.68 to 0.50.
- The lidar's blind-zone recall is 1.0 in every simulator.

**What gets worse, or stays bad:**
- **Clearance optimism.** Radar clearance stays 1.6–2.2 m optimistic at p50, in every combination. That comes from
  the reference point, not the simulator (§6).
- **Front corner radars.** They are mostly self-clutter: corner fl matched 51–61 real returns in 600 samples, with
  2.0 m longitudinal error at p50, and corner fr none.
- **Same-carriageway truck.** The lidar near face is unchanged at 4.6 m p95 (sensor geometry, §5.1).
- **Measurement noise.** Comparisons are statistical over one scenario and seed. Vehicle model choice is random per
  run, so class mixes differ between runs.

## 8. Resources and payloads

- **Whole run (60 s simulated, Webots + SUMO + controllers + profile):**
  - max RSS about 1.4 GB;
  - CPU 75 s at 100/100 smooth;
  - real-time factor 1.31.
- **Cost of each experimental device at 100/100:**
  - the roof lidar costs 10% of the real-time factor (1.52 without, 1.36 with);
  - the corner radars cost about nothing (W1–W7);
  - the 720p camera at 10 Hz costs 35% (§3.2).
- **Lidar processing:** 13.6 ms p50 per frame on the Mac.
- **Payloads:** see §5.3. A lidar range image is 1.6 Mbit/s as uint16 + zlib, objects 0.13 Mbit/s, raw points
  19.6 Mbit/s. Radar is unchanged: under 1 kB per frame.
- **Recordings** (scratch only):
  - lidar 69 MB per minute raw float32;
  - records.jsonl about 10 MB per minute;
  - camera 1 MB per frame.

## 9. Recommended experimental configuration (opt-in; the default stays as it is)

```sh
PYTHONPATH=src venv/bin/python tools/sensor_profile.py run --out DIR --sumo-ms 100 --radar-ms 100 --sync smooth
#  [--webots <Webots-W8-AB.app>/Contents/MacOS/webots]   only for simulator studies: a patched build is not released
```

**Settings:**
- Profile models on: autorickshaw model, bodies aligned.
- Sensors:
  - the original front and rear radars;
  - the rear corner radars;
  - the roof lidar shipped as `RSLF` uint16 + zlib to edge-side processing;
  - the front corner radars only after re-mounting;
  - the camera recording-only, at a low rate.
- The authoritative RoadSense pipeline keeps its current inputs, rate and rules: nothing here is wired into it.

**Prefer the patched AB build** for anything that evaluates radar geometry. Keep the installed build as the baseline
and for anything compared with earlier evidence. Adopting a patched simulator for campaigns is a separate decision.
It changes camera Recognition near the image border, which RoadSense's hazard camera uses.

## 10. Remaining limitations

- **One scenario, one seed, 60 s per configuration.** Development evidence only; nothing here is acceptance data.
- **Physical reference = Webots bounding objects**, as axis-aligned boxes in the model frame (tilted boxes are
  enclosed). This is the right reference for radar, which uses those boxes. The lidar and camera see the visual
  shapes, which differ (TruckSimple's chassis is lower and thinner than its box).
- **Radar coverage is not occlusion-aware.** Same-direction vehicles only; the opposite carriageway is behind the
  median's guardrail.
- **Lidar ground model:** one flat plane. Raised structure with ground clearance can be invisible between layers
  beyond about 25 m.
- **Length mismatches remain** between SUMO and drawn models: Sprinter, bus, motorbike.
- **Ego mirror into SUMO** is +3.0 m along. SUMO's car-following around the ego is offset by that; not fixed.
- **Lane-change smoothing** (`laneChangeDelay`) still makes the body lag SUMO laterally. Truth from the drawn body
  is unaffected; SUMO-based labels are not.
- **Patched Webots:** arm64 only, ad-hoc signed, not reproducible bit-for-bit, not upstreamed.
- **Pi-side lidar processing is not benchmarked**, as the restrictions require.
- **The 2-step lag affects every existing recording made with the stock supervisor:** Phase 6C, 7A.3 and Stage 15's
  frozen plan. Its effect on those results was not re-evaluated here.

## 11. Proposed radar + lidar fusion architecture (proposal; nothing implemented, stopped for review)

1. **Edge input.**
   - Radar frames: today's JSON, plus per return the §6 flags and intervals.
   - Lidar `RSLF` frames (uint16 + zlib) with a once-per-session `RSLC`.
   - One clock: the simulator, and later the vehicle's.
2. **Per-sensor measurement models on the edge.**
   - Radar: a point inside the body (range ± 0.25 m, bearing ± 0.001 rad, plus the reference-point slack up to the
     near face); a one-sided bearing at clamps; no range rate when unreliable.
   - Lidar: clusters with near point, visible extent, `near_range_interval` and `unseen_vertical_gap`.
3. **Association in the ego frame, gated by geometry, never by simulator identity.** A radar point associates with
   a lidar cluster if it lies inside or behind the cluster's visible extent, within the radar's slack.
   Self-suspect radar returns are cleared only if the lidar sees nothing at that cell.
4. **Track state.**
   - Kinematics: centre position and velocity.
   - An extent estimate: a lower bound from the lidar, growing with observation.
   - A separate **near-surface estimate**, the conservative minimum over the sensors' intervals, from which clearance
     and time to collision are computed.
   - Range rate from radar only, when reliable.
5. **Conservatism rules.** Clearance uses the nearest bound any sensor allows: a lidar interval, or a radar point
   minus slack where there is no lidar. A disagreement keeps the nearer value. Motorcycles beyond 40 m and trucks
   with an unseen-gap flag stay radar-led.
6. **Authoritative logic unchanged until reviewed.** The fusion output would run in shadow beside today's tracker,
   P1, P2 and P4 (disabled by default), and the safety rules, logged for comparison, until accepted. Acceptance
   criteria are not changed retroactively.

## 12. Deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | Publication confirmation | §1 (`803d991`, pushed) |
| 2 | Bug reproduction | §2.1, §2.4, `tests/test_webots_projection.py`, `measured-fixture.json` |
| 3 | Original vs patched measurements | §2.5, §7, `radar-variants.json`, `profile-runs.json` |
| 4 | Build status and provenance | §2.3, `tools/webots_w8/` |
| 5 | Experimental sensor configuration | §3, `tools/sensor_profile.py`, `roadsense_sensor_recorder`, `RoadSenseAutorickshaw.proto` |
| 6 | Synchronisation comparison | §4 |
| 7 | LiDAR results | §5, `tools/lidar_geometry.py`, `lab-lidar.json` |
| 8 | Camera feasibility | §3.2 |
| 9 | Resources and payloads | §5.3, §8 |
| 10 | Recommended configuration | §9 |
| 11 | Remaining limitations | §10 |
| 12 | Proposed fusion architecture | §11 |

**Reproduce:** `tools/webots_w8/matrix.sh <out dir> [<AB executable>]` (run matrix), then
`tools/sensor_profile.py analyse <runs> --out x.json` and `python tools/webots_w8/tables.py x.json`.
`tools/sensor_profile.py lab-lidar <W4 run-lidar dir>` re-runs the laboratory lidar cases.
