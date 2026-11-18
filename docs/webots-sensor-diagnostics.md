# Webots sensor diagnostics and simulation upgrade (Stages W1–W7)

Status: **development evidence for review, uncommitted.** This is not acceptance evidence. Nothing here changes the
highway world, the live demonstration, the scenario generator, the trackers, the safety decisions or the Pi. Stage 15
never started, so nothing ran against it. Every claim is labelled:

- **[V] verified:** measured in this work, or read in the pinned Webots source (R2025b, `838bb6f`).
- **[H] hypothesis:** consistent with the evidence but not isolated.

Environment: Apple Silicon Mac, Webots R2025b, SUMO 1.27.1. Every run used
`--batch --mode=fast --no-rendering`. Evidence is in `docs/evidence/webots-w/`.

## How to reproduce

```sh
venv/bin/python tools/webots_calibration.py plan radar  $OUT/radar.json      # 312 cases, 594 s simulated
venv/bin/python tools/webots_calibration.py run  $OUT/radar.json $OUT/run-radar
venv/bin/python tools/webots_calibration.py analyse radar $OUT/run-radar $OUT/radar-analysis.json
# likewise: plan lidar | camera | perf --profile {original,radar_experimental,radar_lidar,ibeo,camera720,all}
PYTHONPATH=src venv/bin/python -m pytest -q tests/test_webots_calibration.py
```

- **New files:**
  - `simulation/webots/worlds/roadsense_calibration.wbt`: the laboratory world.
  - `simulation/webots/controllers/roadsense_calib/roadsense_calib.py`: the supervisor recorder (Python 3.9,
    stdlib only).
  - `tools/webots_calibration.py`: plans, runner and analysis.
  - `tests/test_webots_calibration.py`.
  - A row in `simulation/webots/THIRD_PARTY_NOTICES.md`.
- **W6 runs:** these used a scratch copy of the simulation tree. The probe patch and scripts are in the evidence
  folder (`w6-probe.patch`, `w6_run.sh`, `w6_analyse.py`). The repository's highway world and controller were not
  edited.

---

## 1. Sensor inventory (W1)

Ego frame: x forward, y left. The origin is the LincolnMKZ origin, the rear-axle centre, 0.313 m above the ground.

| Device (highway world) | Node | Mount (x, y, z), yaw | FOV / range | Period in RoadSense | Noise | Used by |
| --- | --- | --- | --- | --- | --- | --- |
| `radar front` | Radar | (4.054, 0, 0.142), 0 | h 2.6 rad, v 0.0785 rad, 1–150 m | 200 ms | range 0.25 m, speed 0.12 m/s, angle 0.001 rad | RoadSense perception |
| `radar rear` | Radar | (−1.16, 0, 0.33), π | h 2.6, v 0.0785, 1–100 m | 200 ms | as front | RoadSense perception |
| nested `Robot` + `radar` | Radar inside a supervisor sub-robot running the sample `radar_target_tracker` controller | (4.054, 0, 0.142) | h 0.9, 60 m, cellDistance 2, radial speed −100..40 | 10 ms | as front | Sample visualisation only (draws boxes); **not** RoadSense |
| `ibeo 1..4` | IbeoLux (Lidar) | front corners (3.72, ±0.71, 0.142) yaw ±0.79; rear corners (−0.88, ±0.75, 0.33) yaw π∓0.75 | extended FOV, 4 layers, 2,720 points per scan | **never enabled** | PROTO default | Nothing |
| `distance sensor *` (10) | DistanceSensor, generic, 2 rays | front, sides, rear at z 0.3 | 10–20 m lookup | 10 ms | none | The sample's overtaking logic (driving), not RoadSense |
| `hazard camera` | Camera + Recognition | top slot (1.61, 0, 1.16) | 512×256, FOV 0.87, recognition 60 m | recognition 200 ms | none | Pothole sensing via Recognition metadata (privileged; pre-existing) |
| `gps`, `telemetry gps`, `inertial unit` | GPS, InertialUnit | origin / look-ahead | — | 10 ms | none | Steering and telemetry |

- **Lidar output [V]:** no LiDAR produces data in the highway world, because the IbeoLux units are installed but
  never enabled. Webots lidars expose a range image (float32 per point) and an optional point cloud
  (x, y, z, layer, time; 20 B/point). Neither is used.
- **Radar output [V]:** each target is a list of (distance, azimuth, radial speed, received power). No point
  cloud, no extent and no elevation.
- **Measured rates [V]:**
  - Recorded campaign frames are spaced exactly 0.200 s apart (6,441 of 6,441 intervals in 12 runs).
  - 263 consecutive-frame repeats were all empty radar lists. No non-empty reading ever repeated.
  - A frame is about 1.1 kB of JSON.

## 2. Verified semantics of the Webots radar (W3)

Measured with the noise-free diagnostic radars in the laboratory: 312 cases, 908 s simulated, 7 targets. The
semantics were also read in `WbRadar.cpp` and `WbObjectDetection.cpp`.

| Quantity | What Webots reports [V] | Evidence |
| --- | --- | --- |
| distance | 3D norm of the centre of the target's bounding-box AABB, in the sensor frame, after clipping to the beam frustum | Error vs this model, p50/p95: box 2/23 mm; car 5/58 mm; BMW 4/154 mm; motorcycle 0/74 mm; rickshaw 1/101 mm |
| azimuth | Bearing to the **model origin** (the PROTO origin = rear axle), computed with a projection that doubles the vertical component: \|az\| = acos(x / √(x² + y² + 4z²)), sign = −sign(y) (positive = right) | Model error p95 ≤ 0.001° for every target; the sign is random when y ≈ 0 |
| speed | Radial component of the origin's displacement between refreshes (exact when noise-free) | Noise-free error 0.00 m/s; noisy p50 0.09, p95 0.25 m/s |
| received power | Pt·G²·λ²·σ / ((4π)³R⁴), threshold `minDetectableSignal` −80 dBm | Measured −78.5 dBm for the motorcycle (σ 10) at 95 m, i.e. a limit of about 104 m. Car: −76.9 dBm at 152 m, so `maxRange` 150 binds first |
| azimuth clamp | Clamped to ±hFOV/2 | Ego body reported at d = 1.0, az ±1.3 |

- **Range error by reference point [V]** (p50, m):

  | Reference | Car | Motorcycle | Truck |
  | --- | --- | --- | --- |
  | Bbox centre | ≈ 0 | ≈ 0 | 0.17 |
  | Model origin | 1.40 | 0.79 | 3.25 |
  | Nearest face | 2.14 | 1.27 | 4.04 |

  Range is not the distance to the nearest surface. That matters for gap and time-to-collision.

- **Azimuth error by reference point [V]** (p95):

  | Reference | Error |
  | --- | --- |
  | Bbox centre | 8.7–17.4° |
  | Origin (true bearing) | 0.7–6.4° (the elevation leak) |
  | Webots model | ≤ 0.001° |

- **Webots defect [V], source read and reproduced:** `WbAffinePlane::vectorProjection(v)` returns
  `v + normal·(v·normal)` instead of `v − normal·(v·normal)`. The vertical offset between the radar and the
  target origin therefore inflates |azimuth|, strongly at short range.

  **Mechanism check [V]:** I added `diag radar front low`, a noise-free radar mounted at a car origin's height
  (0.317 m).

  | Target | Gap | Mount | \|az\| | RoadSense lateral error |
  | --- | --- | --- | --- | --- |
  | Car, dead ahead | 0.3 m | Original | 16.6° | 0.83 m |
  | Car, dead ahead | 0.3 m | Low | 2.0° | 0.09 m |
  | Motorcycle | 0.3 m | Original | 33.3° | 1.07 m |
  | Motorcycle | 0.3 m | Low | 10.3° | 0.30 m |
  | Truck (origin 0.5 m up) | — | Original | 8.2° | 0.82 m |
  | Truck (origin 0.5 m up) | — | Low | 25.3° | 2.45 m |
  | Centred box (origin 0.75 m) | — | Original | 7.2° | — |
  | Centred box (origin 0.75 m) | — | Low | 10.3° | — |

  The truck and the box get **worse** with the low mount. The error follows the height difference between the
  radar and the target's origin, as predicted. No single mounting height fixes every class.
- **Not a filter [V]:** the installed `Radar.wrl` defaults `minRadialSpeed`/`maxRadialSpeed` to 0, meaning no
  speed filter. The table in Webots' `radar.md` says otherwise. Stationary targets are therefore *not* removed by
  speed. In the laboratory, with both vehicles stopped at 0.3–20 m gaps, the radar saw cars and motorcycles 100%
  of the time and trucks 97% (front radar with noise).
- **Garbage first speed [V]:** the first refresh after a target is teleported reports nonsense radial speed
  (+149 to +599 m/s, once −59,362).
- **Own body [V]:** the front radar reports the ego's own body (d = 1.0, az ±1.3 clamped) in 68% of samples and the
  rear in 18%. The corner radars never do. RoadSense drops these through `min < d` and the edge margin.
- **Unexplained residual [H]:** after adding `trailer NULL` (as SUMO does), the TruckSimple range still reads long
  versus the AABB model: 1.09 m at 6.7 m, falling to 0 by 134 m. It is probably a bounding-object detail of the
  PROTO that the model does not capture.

## 3. Simulator and configuration problems

1. **[V] Elevation-doubling bug** in Webots' radar azimuth (section 2). Upstream fix: one sign in
   `WbAffinePlane::vectorProjection`. **This is not patched: it needs your approval.**
2. **[V] SUMO traffic is sensed one SUMO step late, interpolated.** `SumoSupervisor.py` (lines 311–343) places the
   wrapper at the *previous* SUMO pose and gives it velocity (target − current)/step. Webots moves physics-less
   wrappers kinematically with that velocity. Sensors faster than the SUMO step therefore see linear
   interpolation, not new traffic information. The lag is 200 ms at the default step.
3. **[V] Radar range and azimuth refer to different points** (bbox centre vs rear-axle origin). Neither is the
   body centre or the near face.
4. **[V] The autorickshaw is drawn as a random 4.2 m car model** with a SUMO length of 2.8 m. Its ground-truth body
   and its sensed body differ by about 1.4 m in length.
5. **[V] The sample `radar_target_tracker` sub-robot** runs a supervisor controller every 10 ms in the highway world.
   RoadSense never uses it. [H] It costs CPU; this was not measured separately.
6. **[V] The IbeoLux lidars are installed but never enabled**, so there is no lidar data in the highway world.
7. **[V] The motorcycle's radar range limit is about 104 m** (RCS 10) against 150 m `maxRange`. This is realistic,
   but it should be documented as a range limit, not a miss.
8. **[V] Pre-existing:** the hazard camera uses Recognition (privileged object metadata) for potholes. I did not
   change it. No new work uses Recognition.

## 4. RoadSense interpretation problems

`perception.radar_detections` computes `a = mount_yaw − az; lon = mx + d·cos a; lat = my + d·sin a`.

1. **[V] Azimuth is treated as the bearing of the vehicle's body.** It is the bearing of the rear-axle origin,
   inflated by the elevation leak. Close, stopped vehicles land 0.8–1.1 m to the side (both_stopped, 0.3 m gap).
   In the highway campaigns a stopped car whose origin is 0.43 m ahead read az 0.82–1.29 rad, which places it
   1.4–2.2 m to the side. **This, plus FOV clamping and the `EDGE_MARGIN_RAD` drop, is why stopped vehicles
   "disappear" or sit to the side. The returns are not lost.**
2. **[V] Range is treated as a point range.** It is the beam-clipped bbox-centre range. For cars the longitudinal
   p50 error vs the body centre is small (0.2 m). For trucks it is 1.0 m (both_stopped p50) to 1.8 m (alongside).
3. **[V] Clamped azimuths are dropped** (`|az| > fov/2 − 0.005`). That correctly rejects own-body returns. It also
   rejects real targets whose azimuth was clamped because of the leak: the highway Stage 1 motorcycle overlapping
   the ego inside min range.
4. **[V] The Stage 12 statistics used wrapper or footprint oracles.** These sit about 1.45 m (car), 0.91 m
   (motorcycle) and 3.4 m (truck) from the physical body centre. Some "lateral/longitudinal errors" attributed to
   the trackers are oracle offsets. This does not change the acceptance criteria, which stay as they are. It is a
   caveat on how the earlier numbers are read.
5. **[H] The first-refresh garbage speed** (section 2) may reach the tracker when a vehicle enters radar range by
   teleport (SUMO insertion or lane-change smoothing). In the highway W6 runs, 0.2–0.7% of linked
   consecutive-cycle pairs changed range rate by more than 3 m/s. I did not attribute those individually.

**A deterministic correction does not settle it [V].** `corrected_azimuth` assumes nominal origin heights. It halves
p50 lateral error:

| Case | Before (m) | After (m) |
| --- | --- | --- |
| both_stopped car | 0.33 | 0.00 |
| static_rear car | 0.54 | 0.26 |

However, it leaves p95 about unchanged and is **worse** in some cases:

| Case | Lane agreement before | After |
| --- | --- | --- |
| Alongside motorcycle (original radars) | 0.971 | 0.857 |
| Orientation car | 0.643 | 0.600 |

An uncertainty-aware interpretation fits better: lateral variance that grows with the possible height offset at
short range (section 11).

## 5. Calibration errors (laboratory, original front + rear radars, RoadSense transform vs body centre)

p50/p95 in metres. Lane = lane agreement.

| Condition | Seen | Lateral | Longitudinal | Lane |
| --- | --- | --- | --- | --- |
| Stationary targets, 0–10 m | 0.827 | 0.48 / 1.18 | 0.42 / 1.67 | 0.955 |
| Stationary, 10–30 m | 0.970 | 0.27 / 1.19 | 0.20 / 0.71 | 0.971 |
| Stationary, 30–60 m | 0.966 | 0.12 / 0.45 | 0.18 / 0.46 | 1.000 |
| Stationary, 100–150 m | 0.885 (motorcycle beyond ~104 m) | 0.34 / 1.00 | 0.23 / 0.56 | 1.000 |
| Moving, 10–30 m | 1.000 | 0.20 / 0.67 | 0.17 / 0.51 | 0.952 |
| Both stopped, car / motorcycle / truck | 1.0 / 1.0 / 0.974 | 0.33 / 0.44 / 0.30 (p50) | 0.16 / 0.17 / 1.03 (p50) | 1.0 |
| Truck, static | 1.000 | 0.39 / 2.49 | 0.24 / 0.95 | 0.889 |
| Car at yaw 20–180° | 1.000 | 0.52 / 1.42 | 0.17 / 0.51 | **0.643** |
| Car / motorcycle behind a car (occlusion) | 0.0 / 0.0 | — | — | — |

Occlusion is realistic and was kept.

## 6. Original vs experimental configuration

Same plans and seeds, at identical instants:
- **Original:** front + rear radar.
- **Experimental:** the original radars + four corner radars, each 80 m, hFOV 1.6, vFOV 0.25, with the original
  noise and occlusion.

| Condition | Original seen | + corners seen | Note |
| --- | --- | --- | --- |
| Stationary 0–10 m | 0.827 | 0.887 | Lateral p95 1.18 → **1.32 (worse)** |
| Alongside car | 0.50 | 0.75 | Lateral p95 0.92 → **1.18 (worse)** |
| Alongside truck | 0.625 | 0.75 | Lane 0.80 → 0.83 |
| Alongside motorcycle | 0.625 | 0.625 | Lane 0.971 → **0.886 (worse)** |
| Motorcycle occluded by a car | 0.0 | 0.5 | Corner sees around |
| Moving 0–10 m | 0.844 | 0.906 | |

- **Side blind zone [V]** (alongside sweep, adjacent lane, body centre relative to ego origin):

  | Configuration | Car blind at lon (m) | Motorcycle blind at | Truck blind at |
  | --- | --- | --- | --- |
  | Original radars | 0, +1, +3, +6 | 0, +1, +3 | +1, +3, +6 |
  | + corners | +1, +3 | 0, +1, +3 | +3, +6 |
  | IbeoLux ×4 | — | +1 | — |
  | Roof lidar | — | — | — |

  Positions tested: −9, −6, −3, 0, +1, +3, +6, +9.

- **Raised vertical FOV [V]:** `exp radar front vfov` (vFOV 0.25 instead of 0.0785) brought **no detection gain**
  in the laboratory.
- **Low mount:** it helps cars and motorcycles and hurts trucks and tall boxes (section 2), so I do not recommend it
  as a configuration. It is a diagnostic.

## 7. LiDAR feasibility (W5)

- **Range-image conversion [V]:** the conversion in `tools/webots_calibration.py` reproduces Webots' own point cloud
  exactly (maximum difference 0.0000 m over 12,846 points).
  - Columns run left to right at fov/2 − (i + ½)·fov/w.
  - Layers run top down.
  - The layer elevations must be taken from a point cloud: Webots spreads 16 layers over ±15.2° for a 30° vFOV.
  - Infinite returns must be masked; `atan2(inf, inf)` gives 45°.
- **Detection method:** grid clustering (0.4 m cells) after ground and ego-footprint removal. No learning and no
  supervisor positions are used as detections.

Roof unit (16 layers, 1,800 columns, 100 m, 10 Hz), p50/p95:

| Group | Seen | Lateral near-side error (m) | Lane |
| --- | --- | --- | --- |
| Alongside car / truck / motorcycle | 1.0 / 1.0 / 1.0 | 0.13 / 0.15; 0.04 / 0.06; 0.19 / 0.29 | 1.0 |
| Lateral motion (car) | 1.0 | 0.01 / 0.03 | 1.0 |
| Static car / motorcycle / truck | 0.80 / 0.70 / 1.0 | 0.05 / 0.08; 0.15 / 0.41; 0.04 / 0.55 | 1.0 |
| Moving motorcycle | **0.53** | 0.41 | 1.0 |
| Motorcycle behind a car | 0.0 | — | — |

- **Range limit [V]:** with ≥ 3 points per cluster, the roof unit's effective range is:

  | Target | Seen to | Lost at |
  | --- | --- | --- |
  | Car | ≈ 55 m ahead (body centre) | ≈ 95 m |
  | Truck | 98 m | — |
  | **Motorcycle (dead ahead)** | **≈ 45 m** | 55 m and beyond (the closing case loses it from about 47 m) |

  The adjacent-lane motorcycle at 55 m was still seen. Cause [H]: 16 layers with 2° spacing put few beams on a
  narrow target. Long-range motorcycles must stay with radar (about 104 m) or camera.
- **Truck far face [V]:** the near-face longitudinal error for trucks is about 4.6 m, because the cluster sees the
  cab rather than the box end. Shape extraction would need class-aware fitting.
- **IbeoLux ×4 union [V]:** alongside car 1.0, motorcycle 0.88, truck 1.0; static motorcycle 0.60. The front units
  never see behind, and the rear units never see ahead.
- **Payload [V]:**

  | Lidar | Per scan | Rate | Bandwidth |
  | --- | --- | --- | --- |
  | Roof unit, range image (float32) | 115 kB | 10 Hz | 1.15 MB/s |
  | Roof unit, point cloud | 576 kB | 10 Hz | 5.8 MB/s |
  | IbeoLux | 10.9 kB | 12.5 Hz each | ≈ 0.54 MB/s for four |

  The roof unit's bandwidth is ~1,000× today's 1.1 kB frame at 5 Hz. Shipping raw lidar to the Pi is not viable; it
  would have to be clustered on the Mac side first.

## 8. Frequency and synchronisation (W6)

- **Laboratory [V]:** noise-free radar, target closing at 10 m/s.

  | Target movement | Radar 50 ms | Radar 100 ms | Radar 200 ms |
  | --- | --- | --- | --- |
  | Moved every 10 ms | Exact | Exact | Exact |
  | Moved in 200 ms jumps, no velocity | Sawtooth −39.9 / 0 m/s; 43 of 59 samples repeat the position | Sawtooth −20 / 0; 14 of 29 repeat | Exact |
  | Moved in 200 ms jumps **with** `setVelocity` (what SumoSupervisor does) | Exact after the first update | Exact after the first update | — |

- **Highway world [V]:** scratch copy; 60 s each; fast mode, no rendering.

  | SUMO step / radar period | Real-time factor | Range-rate jumps > 3 m/s per linked pair |
  | --- | --- | --- |
  | 200 / 200 (current) | 5.7× | 0.6% |
  | 200 / 50 | 6.1× | 0.2% |
  | 100 / 100 | 5.3× | 0.7% |
  | 50 / 50 | **3.6×** | 0.2% |

  The 50 ms radar sequence is smooth (e.g. −9.17, −9.14, −9.08, −9.02 m/s), with no repeated positions. Radar rate
  costs almost nothing; SUMO step rate is what costs real-time factor.
- **[V]** `basicTimeStep` is 10 ms, and the radar samples exactly on its period. Timestamps in the recorded frames
  are exact. Between SUMO steps the "new" positions are interpolation (section 3.2): 20 Hz radar gives fresh
  *sensor* samples but not fresh *traffic* information. 20 Hz traffic needs SUMO at 50 ms, costing about 37% of
  the real-time factor.

## 9. Camera feasibility (W7)

Pixel footprint of the target in each frame: the frame minus a per-pixel median background. The ego was stationary
and no Recognition was used. Width × height in px:

| Gap (≈ distance from camera − 2.3 m) | Car, 512×256 FOV 0.87 | Car, 720p FOV 1.05 | Motorcycle, 512×256 | Motorcycle, 720p | Truck, 720p |
| --- | --- | --- | --- | --- | --- |
| 5 m | 130×109 | 262×219 | 76×108 | 151×220 | 380×495 |
| 15 m | 58×48 | 116×97 | 36×48 | 70×96 | 164×248 |
| 40 m | 26×20 | 50×40 | 16×20 | 28×40 | 68×111 |
| 80 m | 13×10 | 28×21 | **4×10** | 14×20 | 36×59 |
| 120 m | 8×7 | 17×14 | **4×6** | 10×14 | 24×40 |

- **Current camera [V]:** the 512×256 camera shows a motorcycle at 4 px wide beyond 80 m. That is below what a
  YOLO-class detector resolves.
- **720p candidate [V]:** doubles the focal length. A motorcycle is 14×20 px at 80 m and 10×14 px at 120 m.
  - [H] That is marginal at 120 m and usable to about 80 m.
- **Scene realism [V]:** the laboratory scene is a flat grey plane against a sky box. This says nothing about
  detector accuracy on realistic backgrounds.
- **Rendering cost [V]:** see section 10. A 720p frame is 3.7 MB raw RGBA, i.e. 37 MB/s at 10 Hz. Inference would
  have to run where the frames are rendered, or on compressed frames.

## 10. Mac resources

The 60 s laboratory drive at 15 m/s with 4 targets, one profile at a time. Each figure is for the whole Webots
process. GPU utilisation is the system-wide `ioreg` median.

| Profile | Real-time factor | Step p95 (ms) | CPU (s) | Max RSS (MB) | GPU median | Raw recorded |
| --- | --- | --- | --- | --- | --- | --- |
| Original radars | 43.0 | 0.13 | 3.9 | 669 | 41% | — |
| + corner radars | 41.6 | 0.20 | 3.9 | 648 | 44% | — |
| + roof lidar | 23.2 | 1.22 | 5.0 | 653 | 45% | 69 MB / 60 s |
| IbeoLux ×4 | 16.5 | 2.39 | 6.3 | 673 | 57% | 6.5 MB (subsampled) |
| 720p camera, 10 Hz | 27.8 | 1.31 | 4.3 | 669 | 41% | — |
| All | 12.7 | 2.61 | 7.2 | 701 | 61% | 76 MB |

- **Marginal cost per simulated second [V]:**

  | Device | Cost |
  | --- | --- |
  | Corner radars | ≈ 0 |
  | Roof lidar | 20 ms |
  | IbeoLux ×4 | 37 ms |
  | 720p camera | 13 ms |

- **Highway estimate [H]:** the highway world runs at about 5.7× (≈ 175 ms of wall time per simulated second), so
  adding everything should cost about 55 ms. That would bring it to about 4.3×. This was not measured in the
  highway world.
- **Full radar laboratory run [V]:** 908 s simulated in 19 s; peak footprint 840 MB.

## 11. Recommended experimental configuration (opt-in only)

The original world and live demo stay as they are. Proposal for a separate, opt-in profile (world copy or
environment flag):

1. **Radars:** keep front and rear unchanged. Add the four corner radars (80 m, hFOV 1.6, vFOV 0.25, original noise,
   occlusion on). They give the best gain per cost: nearly free, with side coverage +25 points for cars and +12.5 for trucks.
   The lane and lateral regressions in section 6 must be handled by the uncertainty model in item 4, not hidden.
2. **Rates:** radar at 100 ms. Keep SUMO at 200 ms (or 100 ms if a 7% real-time-factor loss is acceptable). Treat
   between-step positions as interpolated, and add the one-step lag to the latency budget.
3. **Lidar:** a roof 16-layer unit at 10 Hz as the geometric reference for the side blind zone and lateral position,
   clustered on the Mac. Raw points never leave the simulator host.
4. **Interpretation:** model the radar azimuth as origin bearing with a bounded elevation leak. The lateral
   uncertainty is derived from |az| ≤ acos(x / √(x² + y² + 4Δz²)) for Δz across plausible target origin heights:
   inflated variance at short range, not a deterministic correction. Treat range as bbox-centre range, with a
   class-free ±half-length uncertainty toward the near face. Discard the first range rate after a track appears.
5. **Camera:** a 720p, 10 Hz camera kept only for future dataset collection. No inference and no Recognition.
6. **Webots fix (needs your approval):** patching the one-sign `vectorProjection` bug would remove the leak at source.
   It would also change every radar reading, so it would need a fresh baseline.

## 12. Next steps for fusion and tracking (proposal; nothing implemented)

1. A measurement model per sensor:
   - Radar: (bbox-centre range, origin bearing with a leak bound, radial speed of the origin).
   - Lidar cluster: near face and side, with an extent.
2. Associate radar to lidar clusters in the ego frame, with these uncertainties. Fuse in the existing world-frame
   tracker interface, behind the existing opt-in flags, with experimental trackers still off by default.
3. Re-score Stage 12 failure cases against physical-body oracles before attributing errors to trackers.
4. Repeat this laboratory with the Webots fix (if approved) to separate simulator error from interpretation error.
5. Only then design the fusion architecture. **Stopping here for review, as asked.**

## 13. Limitations

- **The laboratory is idealised:** a flat plane with no road furniture, barriers or clutter. Ground removal is
  trivial and there are no multipath or false returns.
- **Targets are kinematic:** no suspension pitch, except the ego's own.
- **Corrections use nominal heights:** the corrected azimuth uses nominal origin heights (car 0.32 m), and they do
  not hold for trucks and boxes.
- **Bounded matrix:** 312 radar cases, 78 lidar cases, 31 camera cases and 6 perf profiles. Single runs, with noise
  from Webots' RNG and no repeat seeds. The figures are development evidence, not acceptance evidence.
- **Moving-ego cases:** the first 7 never met the strict settle criterion (±0.1 m/s). They ran anyway and are
  flagged `settled: false` in the logs; the ego reached 15.00 m/s.
- **Camera measurement:** pixel sizes come from background subtraction on a stationary ego. No detector was run.
- **Mac only:** no Pi measurement was made, since none is authorised.
- **Unexplained residual:** the truck range residual (section 2).
