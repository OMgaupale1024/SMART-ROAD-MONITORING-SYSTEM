# Experimental multi-sensor perception (shadow mode)

Status (2026-11-18): implemented, evaluated offline on development recordings and **frozen**. **Not committed.**
Nothing here changes RoadSense's authoritative pipeline, its warnings, the ego's control, the Pi or the installed
Webots. Every recording below is development evidence, not acceptance data.

**Summary.**
- Of the experimental configurations, **fused (E)** is the strongest.
- Against the unchanged authoritative pipeline on the same recordings, E misses fewer warnings (2.0 s vs 9.0 s) and
  warns earlier, but raises twice as many false warnings (19.1 s vs 9.2 s).
- Its clearance is optimistic for trucks and buses.
- It fails five of the seven criteria this report set for it (§15).
- It is stable as software and is shown as a **recorded-data shadow demonstration**.
- On the Raspberry Pi 5 it gives identical decisions at 54–59 ms p50, but 118 ms at p99 in dense traffic (§11.1).
- The live prototype stays the existing deployed system (§18).

Legend: [V] verified by a measurement or test here; [H] hypothesis, not measured.

| # | Deliverable | Where |
|---|---|---|
| 1 | W8–W11 publication | §1 |
| 2 | Versioned corrected baseline | §2 |
| 3 | Transport specification | §3, `src/roadsense/experimental/transport.py` |
| 4 | Radar measurement model | §4, `src/roadsense/experimental/radar.py` |
| 5 | LiDAR processing | §5, `src/roadsense/experimental/lidar.py` |
| 6 | Fusion | §6, `src/roadsense/experimental/fusion.py` |
| 7 | Tracking | §7, `src/roadsense/experimental/tracker.py` |
| 8 | Original vs experimental comparisons | §9 |
| 9 | Failure cases | §10 |
| 10 | Performance | §11 |
| 11 | Limitations | §12 |
| 12 | Proposed acceptance campaign | §13 |
| 13 | Longer-term Pi integration and ML retraining plan | §14 |
| 14 | Strongest configuration and demonstration readiness | §15 |
| 15 | Pi compatibility and minimal shadow integration plan (fallback preserved) | §16 |
| 16 | Trained-model compatibility and the recorded-data ML demonstration | §17 |
| 17 | Exact startup sequence for the final demonstration | §18 |

Historical dependencies (§3 of the request) are in §8.

## 1. W8–W11 publication [V]

- Commit `62d2e4c` ("feat: correct Webots sensor geometry and simulation synchronization") is pushed to
  `origin/roadsense-v2-edge-ai`. It holds the patches, build and matrix scripts, evidence JSON and
  `docs/webots-sensor-correction.md`.
- Patch provenance is recorded there (§2.3): Webots R2025b `838bb6f`, the base blob of each patched file, the
  applied-diff and executable SHA-256s.
- The installed `/Applications/Webots.app` was never written to. Its executable hash is unchanged (`1a1ac594…`).
- Not committed: the patched build, bulk recordings and local paths. The reproduction scripts take paths as
  arguments.

## 2. The corrected experimental baseline (opt-in) [V]

Version **`roadsense.sim-baseline.corrected.v1`**. The original world, application, demo and default
`tools/sensor_profile.py run` are unchanged. The corrected baseline is this exact invocation:

```
PYTHONPATH=src venv/bin/python tools/sensor_profile.py run OUT --case CASE --seed N \
  --webots <Webots-W8-AB.app>/Contents/MacOS/webots \
  --sumo-ms 100 --radar-ms 100 --sync smooth --lidar-ms 100 --capture-frames
```

| Component | Setting |
|---|---|
| Simulator | the patched AB build (vectorProjection fix + radar point reference), built by `tools/webots_w8/build.sh`, never installed |
| Bodies | profile models aligned on their wrappers; `RoadSenseAutorickshaw` 2.8 m |
| SUMO step / radar period | 100 ms / 100 ms |
| SUMO→Webots | `smooth` sync (below) |
| Radars | the original front and rear radars, plus the rear corners. The front corners are recorded but **disabled in fusion** (self-body detections, W11 §6.3) |
| LiDAR | roof, 16 layers, 10 Hz |
| GPS / IMU | as recorded by the profile robot |
| Camera | off, except RoadSense's own hazard camera |

**Cases** (`--case`, all from existing generators):
- `normal`
- `stationary`: Phase 7A.3's high-speed stationary lead
- `truck_brake`: sudden brake, truck
- `moto_cutin`: gradual, motorcycle
- `rickshaw_cutin`: assertive, autorickshaw
- `mixed_potholes`: mixed traffic plus the potholes family's potholes
- `expressway`

### 2.1 Smooth sync: causality and physical consistency [V]

**The rule** (`tools/sensor_profile_plugin.py`). At each SUMO step boundary:
- `move` = SUMO's newest position − its previous one;
- each body gets a velocity that reaches `newest + 2·move` one step later.

So it uses only states SUMO has already published: a constant-velocity extrapolation of the past two steps. Webots
integrates that velocity, and the sensors sample the integrated pose.

**Evidence.**
- `tools/sensor_profile.py sync-check`: measured.
- `tools/webots_w8/sync_emulate.py`: an offline emulation of the rule from the SUMO log alone.

Evidence JSON: `docs/evidence/experimental-perception/sync-check.json`.

| Check | stock 100 ms | smooth 100 ms | stock 200 ms | smooth 200 ms | smooth, truck braking case |
|---|---|---|---|---|---|
| Sensors vs displayed pose: front radar range RMS (m) at truth shift −1 / **0** / +1 record | 1.57 / **0.36** / 1.55 | 1.56 / **0.35** / 1.54 | 1.56 / **0.35** / 1.55 | 1.55 / **0.39** / 1.55 | 1.78 / **0.57** / 1.76 |
| Displayed − SUMO along track, p50 / p99 (m): steady | 3.26 / 3.83 | 0.006 / 0.033 | 6.50 / 7.63 | 0.019 / 0.12 | 0.007 / 0.067 |
| … at braking onset | 3.46 / 3.58 | 0.037 / 0.041 | 7.06 / 7.55 | 0.105 / 0.150 | 0.072 / 0.227 |
| … while braking | 3.40 / 3.45 | 0.032 / 0.041 | 6.80 / 6.86 | 0.079 / 0.164 | 0.135 / 0.270 |
| Node velocity − displacement rate p95 / p99 (m/s) | 0.07 / 0.10 | 0.27 / 0.43 | 0.11 / 0.17 | 0.46 / 0.73 | 0.32 / 0.83 |
| Heading − direction of motion p99 (rad) | 0.004 | 0.017 | 0.007 | 0.027 | 0.020 |
| Displayed longitudinal acceleration p01 / p99 (m/s²); SUMO's own ±1.7 | −1.65 / 1.75 | −7.2 / 7.2 | −2.8 / 2.9 | −12.6 / 12.7 | −14.6 / 9.2 |
| Lateral speed p99 (m/s) | 0.06 | 0.28 | 0.11 | 0.44 | 0.29 |

**What this establishes:**
1. **Sensor timestamps match the displayed poses.** The residual is smallest at shift 0 in every mode, so each
   measurement belongs to the pose drawn at its own timestamp.
2. **No future SUMO state leaks.**
   - Errors grow at braking onsets (smooth 100 ms: 0.006 m steady, 0.037 m at onset). An extrapolation that saw the
     future would not err there.
   - The offline emulation, given only past states, reproduces the in-simulator error: p50 / p95 / p99
     0.0062 / 0.0217 / 0.0334 m emulated vs 0.0063 / 0.0217 / 0.0326 m measured.
3. **Scheduling is causal and documented.** SUMO state k is applied from step k+1. Sensors sample the integrated
   pose. Scoring against later SUMO states happens offline only.
4. **Pose, orientation and velocity are consistent to within 0.3 m/s (p95) and 0.02 rad.**
5. **Braking and lane changes are physically meaningful, with one caveat.**
   - The extrapolation amplifies SUMO's step-to-step speed changes. Displayed acceleration has p01/p99 of ±7 m/s²
     (100 ms), against SUMO's own ±1.7.
   - It is a 10 Hz jitter, not a manoeuvre: SUMO's speeds remain the low-pass truth.
   - Lateral speeds stay below 0.3 m/s at p99. The one 2.3 m/s maximum is a lane change in the truck case.
   - Isolated values of −350 to −600 m/s² are spawn and teleport frames, excluded from scoring.

**What it costs and the alternative.** An exponentially smoothed velocity (α 0.5) halves the jitter (±3.7 m/s²). It
roughly doubles the error during braking: stationary case p99 0.29 vs 0.17 m; truck 0.21 vs 0.135 m.

Smooth is kept, because position matters more than acceleration jitter here:
- the radar range rate comes from the node velocity, so the 0.27 m/s (p95) inconsistency enters it;
- the tracker's range-rate noise (0.30 m/s) covers it.

Changing the rule would need a new simulation attempt, and the twelve are spent.

### 2.2 Camera Recognition and the pothole pipeline [V]

Patch A changes how Recognition clips objects at the image border. RoadSense's hazard camera
(`hazards.simulated_detections`) turns each recognised pothole's clipped-box centre into its world position.

**Offline** (`tools/webots_w8/recognition_effect.py`, the frustum port; evidence
`docs/evidence/experimental-perception/recognition-effect.json`).
- Potholes are flat, so the clipping defect barely touches them:
  - visibility intervals are identical in both builds (from 6.6–7.1 m ego lane, 14.5–15.2 m two lanes over, to
    60 m);
  - the reported centres differ by at most 0.05 m, only in the last metre before the pothole leaves the image.
- The merge radius of `hazard_map` is 2 m.

**In the simulator** (corrected run `mixed_potholes`). All 4 potholes on the route were recognised: 16, 8, 7 and 7
detections, from about 60 m. Position error against the world's pothole positions was median 0.07–0.12 m, max
0.69 m.

The matching original-simulator run could not test this. Its ego crashed into traffic at 28 s (the stock driving
controller, §12) before the first pothole.

**Conclusion.** The correction does not change the pothole pipeline. Tall vehicles cut by the image border are
another matter, not measured here. Only the recording-only camera reads them.

## 3. Sensor transport: RSSF v1 [V]

`src/roadsense/experimental/transport.py`. One binary frame per perception cycle. The existing JSON interface
(`roadsense.sensors.v1`) is unchanged; `from_v1` / `to_v1` convert both ways. A test feeds the converted frames to
the unchanged `pipeline.Pipeline` and checks identical reports.

```
header   "RSSF" u16 version=1 u16 flags u32 seq f64 t_frame u16 n_sections u16 reserved u32 total_bytes
         u32 crc32 (whole frame, this field zero)
section  u8 kind u8 health u16 reserved f64 t_capture u32 payload_bytes, payload
  1 GNSS          f64 x y z, f32 vx vy speed                                 (world, m, m/s)
  2 IMU           f32 roll pitch yaw                                          (rad)
  3 RADAR         32s name, f32[6] mount (x y z roll pitch yaw), f32 min_range max_range hfov vfov, u16 n,
                  n x (f32 distance azimuth range_rate power, u8 flags)       flags 1 = range rate valid, 2 = power valid
  4 LIDAR_CONFIG  RSLC: id, mount, layers, width, FOV, elevations, encoding, scale (first frame + every 100)
  5 LIDAR_FRAME   RSLF: config id, seq, t_capture, scan time, uint16 cm ranges, zlib
health   0 OK, 1 NO_DATA (alive, nothing this cycle), 2 FAILED (outage); an absent section = sensor missing
```

**What the Receiver does.** `Receiver.accept` is the only path into perception.

| Category | Cases |
|---|---|
| Rejects and counts frames | truncated; larger than 256 KB; corrupt (CRC); not RSSF; other version; out of order or duplicated |
| Counts sequence gaps | |
| Drops sections, recorded in `Frame.dropped` | stale (older than 0.25 s); from the future; unknown sensor; changed mount (> 1 cm or 0.01 rad); lidar without a known config; lidar time ≠ section time; malformed |

**Invalid values.**
- A NaN or infinite range rate is carried as invalid (flag clear) and never becomes a number.
- A lidar "no return" (∞) and any range beyond max range decode as NaN.

**Accuracy and payload.**
- Lidar ranges are quantised to 1 cm (error ≤ 0.005 m, tested).
- Payload: §11.

Tests cover: round trip, corruption, truncation, version, oversize, out of order, gaps, stale, future, mount
change, unknown config, outage health, invalid returns and v1 compatibility (`tests/test_experimental_perception.py`).

## 4. Radar measurement model [V]

`src/roadsense/experimental/radar.py`. Each return becomes a `RadarMeasurement`. Inputs are only the return, the
mount and calibration constants: no simulator identity, dimension or position.

**What it computes:**
- **Position.** Range to the clipped-box centre. The bearing reference is the same point on the corrected build
  (`point`), or the target's origin on the installed one (`origin`).
- **Uncertainty.**
  - σ_range = √(0.25² + 0.30²) m.
  - σ_bearing = √(0.001² + (0.12 m / range)²) rad (`point`), or (0.40 m / range) (`origin`).
  - Both are measured spreads of the reference point around the body centre (W9).
- **Flags** (the return is always kept):
  - `range_clamped`: at minimum range, so position anywhere within it.
  - `bearing_clamped`: at the FOV edge. The bearing is moved 0.15 rad beyond the edge with σ ≥ 0.15 rad, because
    the target is partly outside.
  - `self_suspect`: matches the W11 self-clutter signature of that radar.
  - `velocity_unreliable`: |range rate| > 60 m/s or not finite, including Webots' first refresh at ~10⁵ m/s. The
    position is kept and the speed dropped.
- **Clearance interval**, distance from the ego's body to the target's surface.
  - Formula: point-to-footprint distance minus the measured slack, [d − q99, d − q01].
  - The slack is by radar group, by inline (|lat| < 1.5 m) or side, and by range band. It is not a fixed vehicle
    length.
  - Calibration: `calibration.json`, measured on the W9 development runs (C-smooth-AB, C-aligned-AB), never on the
    evaluation runs. Front, all: q01 / q50 / q99 = 0.67 / 1.78 / 4.26 m.

**The front corner radars** stay disabled in fusion (`fusion.profile_radars`). The self-body problem is not
resolved, and their matched returns had 2 m longitudinal error.

## 5. LiDAR processing [V]

`src/roadsense/experimental/lidar.py`. Pure numpy, no simulator access.

**Pipeline:**
1. Decode RSLF.
2. Transform to the ego frame.
3. **Level with the IMU's roll and pitch** (new). A hard-braking car pitches by 0.077 rad. Unlevelled, the road 20 m
   ahead sits 1.5 m "high" and became 8–21 m wide false obstacles.
4. Remove the ego box.
5. Remove the ground (a plane from the lowest layers).
6. Height band 0.2–4 m.
7. Grid clustering (0.4 m cells).
8. Per cluster:
   - near point;
   - visible extent;
   - lateral σ;
   - `near_range_interval` (range at which a lower beam passed under);
   - `face_low`: height of the lowest visible point of the near face.

**Near-surface clearance.** The visible face is not always the near surface. A Lincoln's bumper at 25 m falls
between beams that are 1.07 m and 0.18 m above the road there, and a truck's chassis sits between beams too. The
interval is therefore:

[visible clearance − max(hidden gap, q99 slack) − 2σ, visible clearance − min(0, q01) + 2σ]

The q01/q99 slack is calibrated per `face_low` bin (< 0.5, 0.5–1, > 1 m) × range band, on the W9 development runs.
Samples are the nearest cluster point on each body, so a split truck counts by its nearest fragment.

| face_low, range | 0–20 m | 20–40 m | > 40 m |
|---|---|---|---|
| < 0.5 m | −0.28 / 0.04 / 0.46 | −0.28 / 0.11 / 0.83 | −0.24 / 0.01 / 0.90 |
| 0.5–1 m | −0.23 / 0.04 / 0.74 | −0.18 / 0.13 / 0.91 | −0.24 / 0.09 / 1.28 |
| > 1 m (cab only: chassis unseen) | (3 samples) | −0.03 / 0.54 / 1.97 | −0.19 / 0.49 / 4.82 |

**No return is not evidence of an empty road.** `free_space()` classifies a spot as one of:
- OCCUPIED;
- SEEN_THROUGH: beams passed beyond it, through its height band;
- OCCLUDED: beams stopped before it;
- UNKNOWN: no valid beam.

The tracker counts a lidar miss only for SEEN_THROUGH (§7).

**Split trucks.** Cab and trailer, or a chassis seen through the wheel gap, form separate clusters. Clusters are not
merged by distance. A fragment joins a confirmed track only if it lies inside that track's estimated extent, which
the tracker learns from earlier views (tested: a second vehicle 3 m behind a truck stays separate).

**Long-range motorcycles stay radar's.** Lidar recall beyond 40 m is ~0.45 (W11), and fusion does not need lidar to
confirm a radar track.

The code runs as plain functions on arrays, structured for the Pi (§14). Nothing in it is Mac-specific.

## 6. Fusion [V]

`src/roadsense/experimental/fusion.py`, `ExperimentalPerception(Config(mode, radars)).process(frame)`. It has three
modes, `radar`, `lidar` and `fused`, all on one tracker.

**Radar:**
- every return goes through §4;
- self-suspect returns never start a track.

**LiDAR:**
- clusters are kept on the ego's carriageway from the existing lane map (`perception.CARRIAGEWAY_Y`). The median
  edge is moved in by 0.3 m, so the guardrail is outside.
- Clusters longer than 20 m are structure.
- The rest becomes `LidarCluster`s with their clearance intervals.

**Association first, then clearance.** Measurements are associated with tracks (§7) before any clearance is computed.
A track's clearance:
- intersects the intervals from its own radar and lidar measurements of the last 0.25 s, each widened by
  relative speed × age;
- if they conflict (empty intersection), keeps the union, which is conservative;
- with no recent measurement, uses the predicted centre minus max(radar q99, half the extent) − 2σ.

It is never the minimum over unrelated detections.

**Lane.**
- Lane probabilities come from the track's lateral position and σ against the lane map.
- A track is `lane_uncertain` if no lane exceeds 0.8.

**Shadow safety.** `shadow_decision` calls the unchanged `prediction`, `risk` and `safety` functions on the
experimental reports, with no hazards:
- *centre*: the report as is;
- *clearance*: `clearance_view` shifts the report so risk.py's assumed 5 × 1.9 m body puts its near face at the
  clearance lower bound.

Neither touches the authoritative pipeline; a test checks that no module outside `roadsense.experimental` imports it.

## 7. Tracking [V]

`src/roadsense/experimental/tracker.py`. A covariance-aware EKF per track. The old tracker stays the baseline.

**State and motion model:**
- World-frame constant velocity [x, y, vx, vy].
- Process noise 3 m/s² along the ego's heading, 1 m/s² across.
- Ego motion is compensated by working in the world frame from GNSS + IMU yaw.

**Updates and gating:**
- **Radar update:** polar range/bearing from the mount, using the measurement's σ.
- **Range rate:** a separate scalar update, gated at χ² 1 dof. A rejected rate keeps the position (tested).
- **LiDAR update:** the centre, from the visible extent plus the track's extent estimate.
- **Assignment:** optimal Hungarian (`assign`, brute-force checked on 30 random cases).
- **Gates:** Mahalanobis χ² 2-dof 11.83, after a 12 m Euclidean pre-gate.

**States:**
- tentative → confirmed on 3 hits in 0.55 s, or radar + lidar within 0.35 s, **and** only once the velocity σ is
  below 2 m/s. A lidar-born track has no velocity at first. Reporting one guessed from 2–3 centre positions put a
  stationary neighbour on a false collision course.
- a lidar cluster of fewer than 8 points (a sliver) may update a track but never starts one. Vehicles within 40 m
  return 20+ points, and sparse far objects are radar's to start;
- confirmed → coasting when not updated;
- deleted when existence falls below threshold, when coasting exceeds 3 s, or when σ exceeds 4 m;
- tentatives expire after 0.35 s without a hit.

Reports carry age, last update, σ, existence and sources.

**Visibility-aware existence** (log-odds):
- A hit adds 1.5, capped at 6.
- A miss costs ln(1 − PD), and only when the sensor was expected to see the track:
  - radar: inside its FOV and range, not behind a nearer track (PD 0.5);
  - lidar: the spot `SEEN_THROUGH` (PD 0.97).
- A miss is ignored if another sensor hit the track in the same cycle. One sensor's miss never invalidates another's
  hit (tested).
- An outage (health FAILED) or an occluded spot costs nothing, so the track coasts.

**Extent.**
- Length and width are learned as the largest visible extents. Widths from clusters wider than 2.6 m (two objects in
  one cluster) are not learned.
- Until a vehicle's length has been seen (alongside), the lidar centre uses a 4.5 m prior with σ 3.0 m. That spans
  the traffic mix, so a bus's near face still associates with its radar track.

**Duplicates.** Two tracks whose centres are within 1 m with the same velocity (±2 m/s) are one vehicle: two
vehicles' centres cannot be that close. Typically one is radar-born and one lidar-born in the same instant. The older
track is kept.

**Groups.**
- Tracks moving together (velocity within 0.7 m/s, gap < 6 m, lateral overlap, for 1 s) form a group. A truck seen
  in parts reports one group.
- Groups are reported, never merged: two cars in convoy stay two tracks.

**Not used.** IMM and learned motion models (GRU): no evidence they are needed yet.

## 8. Historical experiments: what depends on the simulator defects [V/H]

No historical recording was relabelled, re-recorded or deleted. Phase 6C/6D/7A/7A.3 data and models stay as they
are.

**Stage 15** stays frozen and deferred:
- the plan's SHA-256 is still `51ffb16d…f9e2`, unchanged against `HEAD`;
- it was not executed against any configuration.

What the historical truth was (from `docs/dataset.md` and the ground-truth headers, schema
`roadsense.groundtruth.v2`):
- the physical Webots pose of each wrapper at the supervisor's time, `pose_source: webots_physical`;
- a front bumper reconstructed as that wrapper plus half the *intended SUMO* length;
- SUMO's logical poses recorded alongside, for audit only.

| Dependency | Size (measured) | Affected historical conclusions | Not affected |
|---|---|---|---|
| **SUMO logical positions** | display lags SUMO by 2 steps: 6.5–7.8 m at 200 ms (W9) | scenario staging and event timing. Gaps, "act" times and slot checks were computed in SUMO, so physical gaps at the event were ~2 steps of travel shorter or longer than drawn. Cut-in severity and event timing in 6C/7A/7A.3 recordings are therefore approximate. | labels and truth: they use physical poses, consistent with what the sensors saw |
| **Physical poses of the wrapper, not the drawn body** | drawn rear bumper 1.3 m nearer than the truth's bumper for cars ahead; body offsets −0.9 to −1.6 m by model; intended vs drawn lengths (Sprinter 6.6 vs 4.5, autorickshaw drawn as a random 3.3–6.6 m car) | 6D `forward-risk.v1` near-miss labels (intended boxes) **understate** risk ahead and overstate it behind by about a metre. 7A/7A.2 models learned from those labels. 7A.3 position-error metrics (TruthOracle) include the offset. | event/no-event distinctions far from the label threshold |
| **Incorrect reference points** (installed radar: range to the clipped box centre, azimuth to the origin, plus the vectorProjection defect) | lateral p95 0.72 m vs 0.22 m corrected (front) | 7A.3's main measured cause of T7–T9 (neighbouring-lane errors, close stopped-lead misses) is a **simulator** defect, not a property of a real radar. P1/P2/P4 tuning and T2–T9 failures are confounded by it. 7A/7A.2 radar-derived features carry it. | T1, T10, T13 (latency, Pi CPU); the Stage 1 confirmation speed cliff (tracker logic) |
| **Delayed timing** | none at the sensors (residual minimum at shift 0, §2.1); SUMO behaviour 2 steps ahead of display | car-following and event reactions in SUMO respond to states 2 steps ahead of what is drawn. Small for steady traffic, material at braking onsets. | measured sensor latency, warning latency relative to the drawn scene |

**Small re-evaluation on previously failing cases.** The six corrected/original pairs in §9 are the high-speed
stationary lead, truck braking, motorcycle cut-in, autorickshaw cut-in, mixed traffic and expressway. They compare the
**unchanged** original tracker on both simulators (A vs B). The rest of the historical corpus is not re-run.

## 9. Original vs experimental comparisons [V]

### 9.1 Configurations and how they were run

| Label | Simulator | Pipeline | Sensors | Rate |
|---|---|---|---|---|
| **A** | original: installed Webots, 200 ms SUMO/radar, stock sync, stock models | authoritative `pipeline.Pipeline`, unchanged | front + rear radar, hazard camera | 5 Hz |
| **B** | corrected baseline v1 (§2) | authoritative, unchanged | the same | 5 Hz |
| **C** | corrected | experimental, radar only | front, rear, rear-corner radars | 10 Hz |
| **D** | corrected | experimental, lidar only | roof lidar | 10 Hz |
| **E** | corrected | experimental, fused | C + D | 10 Hz |

**How they were run.**
- A and B replay the sensor frames RoadSense's own controller captured.
- C–E replay the profile recording through the RSSF transport: encode → `Receiver` → perception.
- Every tracker sees only sensor data. Truth (drawn bodies) scores only.
- Tools:
  - `tools/experimental_replay.py replay`, results in `docs/evidence/experimental-perception/results.json`;
  - `tools/experimental_replay.py warnings`, results in `warnings.json`.

**Caveats.**
- Original and corrected runs of a case share the seed, not the traffic (§12). Compare A with B as distributions.
- B vs C/D/E is the like-for-like comparison: the same recordings.
- The oracle is RoadSense's unchanged risk/safety rules applied to the true bodies.
  - A/B are rated on their reports. C–E are rated on the *clearance view* (the conservative one, §6).
  - B runs at 5 Hz and C–E at 10 Hz, so frame counts differ by 2×. Warning metrics below are also given in seconds.

### 9.2 Definitions

| Metric | Definition |
|---|---|
| Recall (zone) | share of true same-direction bodies in the zone with a report within 2 m of their footprint (optimal one-to-one matching). Zones: ego lane ahead ≤ 60 m; adjacent lanes within ±30 m; blind zones (−6…4.5 m, 1–4.5 m to the side); behind |
| False /fr | reports per frame within −60…100 m, \|lat\| ≤ 12 m, with no body within 3 m |
| Dup /fr | unmatched reports within 3 m of a body that is matched to a report of a different group |
| Position / velocity error | matched reports vs body centre (velocity only for tracks older than 1 s) |
| **Clearance optimistic** | lower bound of the reported clearance interval > true clearance + 0.1 m. The obstacle looks farther than it is: **the safety-relevant error** |
| **Clearance coverage** | true clearance within [lo − 0.1, hi + 0.1] |
| Clearance width | hi − lo |
| Missed HIGH | oracle HIGH or CRITICAL, tracker below HIGH |
| False HIGH | tracker HIGH or CRITICAL while the oracle is SAFE or CAUTION |

### 9.3 Detection, tracking and clearance (filtered: scored before 1 s ahead of any crash)

| Case | Trk | Ego lane ≤60 m | Adjacent ≤30 m | Blind L/R | Behind | False /fr | Dup /fr | ID sw | Never trk | Confirm p50 s | Lon p95 m | Lat p95 m | Vel p95 m/s | Lane acc | Clear. optim. / cover | Missed / false HIGH | First HIGH vs act (oracle, trk) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| expressway | A | — | 1.00 | 1.00/— | 1.00 | 0.00 | 0.00 | 0 | 0/1 | 0.00 | 0.91 | 1.36 | 0.89 | 0.97 | — | 0 / 0 (of 0) | — |
| expressway | B | — | 1.00 | 1.00/— | 1.00 | 0.00 | 0.00 | 0 | 0/1 | 0.00 | 0.55 | 0.13 | 0.53 | 1.00 | — | 0 / 0 (of 0) | — |
| expressway | C | — | 1.00 | 1.00/— | 1.00 | 0.00 | 0.01 | 0 | 0/1 | 0.00 | 0.18 | 0.07 | 0.29 | 1.00 | 0.00 / 1.00 | 0 / 11 (of 0) | — |
| expressway | D | — | 1.00 | 1.00/— | 1.00 | 0.02 | 0.02 | 1 | 0/1 | 6.00 | 0.86 | 0.51 | 0.59 | 1.00 | 0.00 / 1.00 | 0 / 5 (of 0) | — |
| expressway | E | — | 1.00 | 1.00/— | 1.00 | 0.02 | 0.01 | 0 | 0/1 | 0.00 | 0.27 | 0.09 | 0.21 | 1.00 | 0.00 / 0.99 | 0 / 5 (of 0) | — |
| mixed_potholes | A | 1.00 | 0.98 | 1.00/— | 0.63 | 0.00 | 0.00 | 3 | 0/6 | 0.20 | 0.67 | 0.91 | 0.80 | 0.99 | — | 1 / 0 (of 18) | — |
| mixed_potholes | B | 0.86 | 0.99 | 0.98/0.53 | 0.99 | 0.04 | 0.03 | 22 | 2/16 | 0.00 | 0.71 | 0.30 | 0.85 | 1.00 | — | 34 / 43 (of 154) | — |
| mixed_potholes | C | 0.87 | 1.00 | 1.00/0.89 | 1.00 | 0.05 | 0.12 | 24 | 1/16 | 0.00 | 0.61 | 0.16 | 0.56 | 1.00 | 0.08 / 0.91 | 13 / 362 (of 228) | — |
| mixed_potholes | D | 0.93 | 0.99 | 1.00/0.99 | 0.71 | 0.21 | 0.85 | 38 | 3/16 | 6.50 | 2.99 | 0.56 | 1.20 | 1.00 | 0.09 / 0.84 | 33 / 173 (of 228) | — |
| mixed_potholes | E | 0.99 | 1.00 | 1.00/1.00 | 1.00 | 0.18 | 0.85 | 23 | 1/16 | 0.00 | 2.90 | 0.31 | 0.60 | 1.00 | 0.09 / 0.88 | 15 / 163 (of 228) | — |
| moto_cutin | A | 1.00 | 1.00 | —/— | — | 0.00 | 0.00 | 0 | 0/2 | 0.40 | 0.76 | 0.88 | 1.14 | 0.99 | — | 0 / 0 (of 0) | —, — |
| moto_cutin | B | 1.00 | 1.00 | —/— | — | 0.00 | 0.00 | 0 | 0/2 | 0.60 | 0.51 | 0.10 | 0.85 | 1.00 | — | 0 / 0 (of 0) | —, — |
| moto_cutin | C | 1.00 | 1.00 | —/— | — | 0.01 | 0.01 | 1 | 0/2 | 0.35 | 0.14 | 0.09 | 0.48 | 1.00 | 0.60 / 0.38 | 0 / 6 (of 0) | —, 4.10 |
| moto_cutin | D | 0.89 | 0.95 | —/— | — | 0.00 | 0.15 | 0 | 0/2 | 3.90 | 2.78 | 0.32 | 1.34 | 0.99 | 0.00 / 0.96 | 0 / 0 (of 0) | —, — |
| moto_cutin | E | 1.00 | 1.00 | —/— | — | 0.05 | 0.20 | 1 | 0/2 | 0.35 | 0.32 | 0.27 | 0.46 | 1.00 | 0.40 / 0.56 | 0 / 2 (of 0) | —, — |
| rickshaw_cutin | A | — | 0.87 | —/— | — | 0.00 | 0.00 | 0 | 0/2 | 0.20 | 0.57 | 0.50 | 0.49 | 1.00 | — | 0 / 0 (of 0) | —, — |
| rickshaw_cutin | B | 1.00 | 0.95 | 1.00/— | 1.00 | 0.00 | 0.00 | 0 | 0/3 | 0.00 | 0.44 | 0.19 | 1.59 | 1.00 | — | 3 / 0 (of 10) | 0.20, 0.60 |
| rickshaw_cutin | C | 1.00 | 0.96 | 1.00/— | 1.00 | 0.02 | 0.02 | 3 | 0/3 | 0.00 | 0.14 | 0.15 | 0.62 | 1.00 | 0.00 / 1.00 | 3 / 20 (of 15) | 0.20, 0.50 |
| rickshaw_cutin | D | 0.97 | 0.90 | 1.00/— | 0.98 | 0.04 | 0.03 | 4 | 2/3 | 0.80 | 1.31 | 1.36 | 2.30 | 0.96 | 0.04 / 0.95 | 2 / 14 (of 15) | 0.20, 0.40 |
| rickshaw_cutin | E | 1.00 | 0.99 | 1.00/— | 1.00 | 0.02 | 0.01 | 4 | 0/3 | 0.00 | 0.46 | 0.18 | 0.63 | 0.99 | 0.01 / 0.99 | 2 / 16 (of 15) | 0.20, 0.40 |
| stationary | A | 0.95 | 1.00 | 0.13/— | — | 0.00 | 0.00 | 0 | 1/5 | 0.10 | 0.56 | 0.34 | 2.85 | 1.00 | — | 4 / 0 (of 4) | 1.20, — |
| stationary | B | 0.88 | — | —/— | — | 0.00 | 0.00 | 0 | 1/2 | 0.60 | 0.29 | 0.09 | 4.49 | 1.00 | — | 3 / 0 (of 4) | 1.00, 1.60 |
| stationary | C | 0.92 | — | —/— | — | 0.01 | 0.01 | 1 | 1/2 | 0.40 | 1.10 | 0.03 | 3.94 | 1.00 | 0.00 / 1.00 | 0 / 0 (of 8) | 1.00, 1.00 |
| stationary | D | 0.81 | — | —/— | — | 0.00 | 0.00 | 0 | 1/2 | 0.90 | 2.94 | 0.06 | 6.15 | 1.00 | 0.00 / 0.97 | 7 / 0 (of 8) | 1.00, 1.70 |
| stationary | E | 0.98 | — | —/— | — | 0.03 | 0.02 | 2 | 1/2 | 0.10 | 1.40 | 0.04 | 4.33 | 1.00 | 0.00 / 0.96 | 0 / 0 (of 8) | 1.00, 1.00 |
| truck_brake | A | 0.78 | 1.00 | 0.51/— | 0.99 | 0.00 | 0.00 | 4 | 2/18 | 0.30 | 1.03 | 0.81 | 1.29 | 0.99 | — | 6 / 7 (of 6) | — |
| truck_brake | B | 0.89 | 0.99 | 1.00/— | 0.94 | 0.00 | 0.00 | 4 | 3/18 | 0.20 | 0.55 | 0.21 | 1.52 | 1.00 | — | 5 / 3 (of 8) | 0.80, -2.40 |
| truck_brake | C | 0.89 | 1.00 | 1.00/— | 0.96 | 0.04 | 0.02 | 6 | 2/18 | 0.00 | 0.45 | 0.22 | 0.66 | 1.00 | 0.01 / 0.98 | 3 / 27 (of 13) | 0.80, -3.40 |
| truck_brake | D | 0.79 | 0.97 | 1.00/— | 0.89 | 0.01 | 0.12 | 7 | 7/18 | 1.00 | 2.03 | 0.40 | 2.70 | 0.99 | 0.03 / 0.94 | 0 / 22 (of 13) | 0.80, -2.90 |
| truck_brake | E | 0.92 | 1.00 | 1.00/— | 0.97 | 0.08 | 0.11 | 8 | 1/18 | 0.00 | 0.56 | 0.20 | 0.63 | 1.00 | 0.01 / 0.97 | 3 / 5 (of 13) | 0.80, -2.60 |

**Reading the table:**
- **E has the highest (or tied) ego-lane recall of the corrected trackers in every case**, and never more
  never-tracked episodes than B. Its position and velocity errors are close to C's (radar dominates them).
- **D (lidar only)** is the weakest tracker:
  - confirmation takes 0.8–6.5 s (it must first learn a velocity);
  - longitudinal p95 is up to 3 m;
  - it misses 7 of 8 oracle-HIGH frames on the stationary lead.
- **Mixed traffic is E's weakest case.**
  - It has 0.18 false and 0.85 duplicate reports per frame (B: 0.04 / 0.03).
  - Its longitudinal p95 is 2.9 m (B: 0.7 m).
  - These come from lidar fragments of long and dense vehicles. D alone shows the same: 0.21 / 0.85, and 3.0 m.
  - Its recall is still the highest: ego lane 0.99 vs B's 0.86.
- **C (radar only)** is accurate in position but raises the most false warnings (§9.5), and is optimistic for buses
  (§9.6).
- **B vs A** (same tracker, simulator corrected): lateral p95 drops from 0.34–1.36 m to 0.09–0.21 m. A missed all 6
  oracle-HIGH frames of its truck run (that run never staged its event).

### 9.4 Robustness configurations of E

All five variants were run on every corrected case:
- `radar_front_outage` and `lidar_outage`: 10 s around the event, or mid-run;
- `lidar_late`: every lidar frame one cycle late;
- `corrupt_1pct`: one random byte flipped in 1 % of frames.

| Case | Variant | Ego lane ≤60 m | Adjacent ≤30 m | Blind L/R | False /fr | Clear. optim. | Missed / false HIGH | Receiver drops |
|---|---|---|---|---|---|---|---|---|
| expressway | E | — | 1.00 | 1.00/— | 0.02 | 0.00 | 0 / 5 | 0 rejected; {} |
| expressway | E-corrupt_1pct | — | 1.00 | 1.00/— | 0.02 | 0.00 | 0 / 5 | 7 rejected; {} |
| expressway | E-lidar_late | — | 1.00 | 1.00/— | 0.00 | 0.00 | 0 / 15 | 0 rejected; {} |
| expressway | E-lidar_outage | — | 1.00 | 1.00/— | 0.02 | 0.00 | 0 / 11 | 0 rejected; {} |
| expressway | E-radar_front_outage | — | 1.00 | 1.00/— | 0.02 | 0.00 | 0 / 9 | 0 rejected; {} |
| mixed_potholes | E | 0.99 | 1.00 | 1.00/1.00 | 0.18 | 0.09 | 15 / 163 | 0 rejected; {} |
| mixed_potholes | E-corrupt_1pct | 0.99 | 1.00 | 1.00/1.00 | 0.18 | 0.09 | 15 / 163 | 13 rejected; {} |
| mixed_potholes | E-lidar_late | 0.88 | 1.00 | 1.00/0.90 | 0.10 | 0.09 | 8 / 380 | 0 rejected; {} |
| mixed_potholes | E-lidar_outage | 0.99 | 1.00 | 1.00/1.00 | 0.19 | 0.10 | 15 / 168 | 0 rejected; {} |
| mixed_potholes | E-radar_front_outage | 0.99 | 1.00 | 1.00/1.00 | 0.20 | 0.09 | 15 / 161 | 0 rejected; {} |
| moto_cutin | E | 1.00 | 1.00 | —/— | 0.05 | 0.40 | 0 / 2 | 0 rejected; {} |
| moto_cutin | E-corrupt_1pct | 1.00 | 1.00 | —/— | 0.05 | 0.40 | 0 / 2 | 4 rejected; {} |
| moto_cutin | E-lidar_late | 1.00 | 1.00 | —/— | 0.01 | 0.60 | 0 / 6 | 0 rejected; {} |
| moto_cutin | E-lidar_outage | 1.00 | 1.00 | —/— | 0.04 | 0.25 | 0 / 6 | 0 rejected; {} |
| moto_cutin | E-radar_front_outage | 1.00 | 0.95 | —/— | 0.04 | 0.13 | 0 / 2 | 0 rejected; {} |
| rickshaw_cutin | E | 1.00 | 0.99 | 1.00/— | 0.02 | 0.01 | 2 / 16 | 0 rejected; {} |
| rickshaw_cutin | E-corrupt_1pct | 1.00 | 0.99 | 1.00/— | 0.02 | 0.01 | 2 / 16 | 4 rejected; {} |
| rickshaw_cutin | E-lidar_late | 1.00 | 0.98 | 1.00/— | 0.01 | 0.00 | 3 / 22 | 0 rejected; {} |
| rickshaw_cutin | E-lidar_outage | 1.00 | 0.96 | 1.00/— | 0.02 | 0.01 | 3 / 20 | 0 rejected; {} |
| rickshaw_cutin | E-radar_front_outage | 0.97 | 0.90 | 1.00/— | 0.02 | 0.03 | 2 / 16 | 0 rejected; {} |
| stationary | E | 0.98 | — | —/— | 0.03 | 0.00 | 0 / 0 | 0 rejected; {} |
| stationary | E-corrupt_1pct | 0.98 | — | —/— | 0.03 | 0.00 | 0 / 0 | 5 rejected; {} |
| stationary | E-lidar_late | 0.96 | — | —/— | 0.01 | 0.00 | 0 / 0 | 0 rejected; {} |
| stationary | E-lidar_outage | 0.92 | — | —/— | 0.01 | 0.00 | 0 / 0 | 0 rejected; {} |
| stationary | E-radar_front_outage | 0.81 | — | —/— | 0.00 | 0.00 | 7 / 0 | 0 rejected; {} |
| truck_brake | E | 0.92 | 1.00 | 1.00/— | 0.08 | 0.01 | 3 / 5 | 0 rejected; {} |
| truck_brake | E-corrupt_1pct | 0.92 | 1.00 | 1.00/— | 0.08 | 0.01 | 2 / 4 | 5 rejected; {} |
| truck_brake | E-lidar_late | 0.91 | 1.00 | 1.00/— | 0.07 | 0.01 | 3 / 26 | 0 rejected; {} |
| truck_brake | E-lidar_outage | 0.91 | 1.00 | 1.00/— | 0.04 | 0.01 | 4 / 9 | 0 rejected; {} |
| truck_brake | E-radar_front_outage | 0.82 | 0.99 | 1.00/— | 0.04 | 0.02 | 3 / 10 | 0 rejected; {} |

**What the robustness runs show:**
- **Corruption.** Every corrupted frame was rejected by the CRC (4–13 per run). None reached fusion, and metrics are
  unchanged.
- **No stale, future or misconfigured section was dropped** in any variant: the recordings are clean. These checks
  are exercised by the tests (§3), not by these runs.
- **LiDAR outage.** E falls back on radar. False HIGH frames rise toward C's level (truck 5 → 9, rickshaw 16 → 20).
- **Front-radar outage.** E relies on the lidar ahead and inherits D's weakness: stationary-lead ego-lane recall
  0.98 → 0.81, and 7 of 8 HIGH frames missed.
- **LiDAR one cycle late: a design limitation.**
  - The tracker has no out-of-sequence update. A batch older than the track's last update is counted and
    discarded (`late_batches`).
  - With the lidar 100 ms behind the radar, E effectively becomes radar-only (truck false HIGH 5 → 26, close to C's
    27).
  - A real asynchronous sensor set needs either buffering to the slowest sensor or out-of-sequence updates. Not
    added: feature freeze.

### 9.5 Warning metrics, raw and filtered, with every exclusion counted

**Tiers** (`warning_tiers` in the replay tool):

| Tier | What it covers |
|---|---|
| raw, all frames | everything recorded, including after the ego crashed, and bodies in their insertion frames |
| raw before crash | as raw, up to the crash (a deceleration above 10 m/s² over 0.2 s) |
| **filtered** | the headline metric: up to 1 s before the crash. The oracle ignores bodies inserted in the last 1 s, and tracker HIGH frames caused *only* by such bodies (or by a ghost within 10 m of one) are set aside |
| final second | the last second before each crash, filtered and raw |

Frame counts at 5 Hz (A/B) and 10 Hz (C–E); seconds in brackets.

| Case | Trk | Filtered: missed / false HIGH frames (seconds) of oracle HIGH | Raw before crash | Raw, all frames | Final second before crash (filtered; raw) | Excluded: post-crash / final second / insertion artefacts / oracle frames changed |
|---|---|---|---|---|---|---|
| expressway | A | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| expressway | B | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| expressway | C | 0 / 11 (0.0 / 1.1 s) of 0 | 0 / 11 (0.0 / 1.1 s) of 0 | 0 / 11 (0.0 / 1.1 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| expressway | D | 0 / 5 (0.0 / 0.5 s) of 0 | 0 / 5 (0.0 / 0.5 s) of 0 | 0 / 5 (0.0 / 0.5 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| expressway | E | 0 / 5 (0.0 / 0.5 s) of 0 | 0 / 5 (0.0 / 0.5 s) of 0 | 0 / 5 (0.0 / 0.5 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| mixed_potholes | A | 1 / 0 (0.2 / 0.0 s) of 18 | 1 / 0 (0.2 / 0.0 s) of 23 | 21 / 1 (4.2 / 0.2 s) of 47 | 0 / 0 (0.0 / 0.0 s) of 5; 0 / 0 (0.0 / 0.0 s) of 5 | 479 / 5 / 0 / 0 |
| mixed_potholes | B | 34 / 43 (6.8 / 8.6 s) of 154 | 34 / 44 (6.8 / 8.8 s) of 154 | 34 / 44 (6.8 / 8.8 s) of 154 | no crash | 0 / 0 / 1 / 0 |
| mixed_potholes | C | 13 / 362 (1.3 / 36.2 s) of 228 | 13 / 362 (1.3 / 36.2 s) of 228 | 13 / 362 (1.3 / 36.2 s) of 228 | no crash | 0 / 0 / 0 / 0 |
| mixed_potholes | D | 33 / 173 (3.3 / 17.3 s) of 228 | 33 / 173 (3.3 / 17.3 s) of 228 | 33 / 173 (3.3 / 17.3 s) of 228 | no crash | 0 / 0 / 0 / 0 |
| mixed_potholes | E | 15 / 163 (1.5 / 16.3 s) of 228 | 15 / 163 (1.5 / 16.3 s) of 228 | 15 / 163 (1.5 / 16.3 s) of 228 | no crash | 0 / 0 / 0 / 0 |
| moto_cutin | A | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| moto_cutin | B | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| moto_cutin | C | 0 / 6 (0.0 / 0.6 s) of 0 | 0 / 6 (0.0 / 0.6 s) of 0 | 0 / 6 (0.0 / 0.6 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| moto_cutin | D | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | 0 / 0 (0.0 / 0.0 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| moto_cutin | E | 0 / 2 (0.0 / 0.2 s) of 0 | 0 / 2 (0.0 / 0.2 s) of 0 | 0 / 2 (0.0 / 0.2 s) of 0 | no crash | 0 / 0 / 0 / 0 |
| rickshaw_cutin | A | 0 / 0 (0.0 / 0.0 s) of 0 | 3 / 0 (0.6 / 0.0 s) of 3 | 7 / 0 (1.4 / 0.0 s) of 7 | 3 / 0 (0.6 / 0.0 s) of 3; 3 / 0 (0.6 / 0.0 s) of 3 | 111 / 5 / 0 / 0 |
| rickshaw_cutin | B | 3 / 0 (0.6 / 0.0 s) of 10 | 3 / 0 (0.6 / 0.0 s) of 10 | 3 / 0 (0.6 / 0.0 s) of 10 | no crash | 0 / 0 / 0 / 0 |
| rickshaw_cutin | C | 3 / 20 (0.3 / 2.0 s) of 15 | 3 / 20 (0.3 / 2.0 s) of 15 | 3 / 20 (0.3 / 2.0 s) of 15 | no crash | 0 / 0 / 0 / 0 |
| rickshaw_cutin | D | 2 / 14 (0.2 / 1.4 s) of 15 | 2 / 14 (0.2 / 1.4 s) of 15 | 2 / 14 (0.2 / 1.4 s) of 15 | no crash | 0 / 0 / 0 / 0 |
| rickshaw_cutin | E | 2 / 16 (0.2 / 1.6 s) of 15 | 2 / 16 (0.2 / 1.6 s) of 15 | 2 / 16 (0.2 / 1.6 s) of 15 | no crash | 0 / 0 / 0 / 0 |
| stationary | A | 4 / 0 (0.8 / 0.0 s) of 4 | 5 / 0 (1.0 / 0.0 s) of 9 | 7 / 1 (1.4 / 0.2 s) of 29 | 1 / 0 (0.2 / 0.0 s) of 5; 1 / 0 (0.2 / 0.0 s) of 5 | 100 / 5 / 0 / 0 |
| stationary | B | 3 / 0 (0.6 / 0.0 s) of 4 | 4 / 0 (0.8 / 0.0 s) of 10 | 8 / 6 (1.6 / 1.2 s) of 56 | 0 / 0 (0.0 / 0.0 s) of 5; 0 / 0 (0.0 / 0.0 s) of 5 | 101 / 5 / 0 / 1 |
| stationary | C | 0 / 0 (0.0 / 0.0 s) of 8 | 1 / 0 (0.1 / 0.0 s) of 19 | 1 / 29 (0.1 / 2.9 s) of 109 | 0 / 0 (0.0 / 0.0 s) of 10; 0 / 0 (0.0 / 0.0 s) of 10 | 202 / 10 / 0 / 1 |
| stationary | D | 7 / 0 (0.7 / 0.0 s) of 8 | 8 / 0 (0.8 / 0.0 s) of 19 | 11 / 21 (1.1 / 2.1 s) of 109 | 0 / 0 (0.0 / 0.0 s) of 10; 0 / 0 (0.0 / 0.0 s) of 10 | 202 / 10 / 0 / 1 |
| stationary | E | 0 / 0 (0.0 / 0.0 s) of 8 | 1 / 0 (0.1 / 0.0 s) of 19 | 2 / 15 (0.2 / 1.5 s) of 109 | 0 / 0 (0.0 / 0.0 s) of 10; 0 / 0 (0.0 / 0.0 s) of 10 | 202 / 10 / 0 / 1 |
| truck_brake | A | 6 / 7 (1.2 / 1.4 s) of 6 | 8 / 8 (1.6 / 1.6 s) of 8 | 8 / 8 (1.6 / 1.6 s) of 8 | no crash | 0 / 0 / 1 / 3 |
| truck_brake | B | 5 / 3 (1.0 / 0.6 s) of 8 | 5 / 3 (1.0 / 0.6 s) of 13 | 28 / 6 (5.6 / 1.2 s) of 55 | 0 / 0 (0.0 / 0.0 s) of 5; 0 / 0 (0.0 / 0.0 s) of 5 | 73 / 5 / 0 / 2 |
| truck_brake | C | 3 / 27 (0.3 / 2.7 s) of 13 | 4 / 34 (0.4 / 3.4 s) of 24 | 22 / 55 (2.2 / 5.5 s) of 133 | 0 / 0 (0.0 / 0.0 s) of 10; 0 / 0 (0.0 / 0.0 s) of 10 | 147 / 10 / 7 / 5 |
| truck_brake | D | 0 / 22 (0.0 / 2.2 s) of 13 | 1 / 22 (0.1 / 2.2 s) of 24 | 2 / 34 (0.2 / 3.4 s) of 133 | 0 / 0 (0.0 / 0.0 s) of 10; 0 / 0 (0.0 / 0.0 s) of 10 | 147 / 10 / 0 / 5 |
| truck_brake | E | 3 / 5 (0.3 / 0.5 s) of 13 | 4 / 9 (0.4 / 0.9 s) of 24 | 4 / 45 (0.4 / 4.5 s) of 133 | 0 / 0 (0.0 / 0.0 s) of 10; 0 / 0 (0.0 / 0.0 s) of 10 | 147 / 10 / 4 / 5 |

**Totals over the six corrected cases, filtered, in seconds:**

| | B (authoritative) | C radar | D lidar | **E fused** |
|---|---|---|---|---|
| Missed HIGH | 9.0 s | 1.9 s | 4.2 s | **2.0 s** |
| False HIGH | **9.2 s** | 42.6 s | 21.4 s | 19.1 s |

**Final second before each crash.** The recordings remain valid there: every frame has its sensors, and the crash
is after it.
- In both stationary runs and the corrected truck run, B and C–E rate every one of the last 5/10 frames as the
  oracle does (0 missed, 0 false).
- A missed 1 of 5 (stationary) and all 3 oracle-HIGH frames of its autorickshaw run's last second, and none of 5 in its mixed run.
- Raw and filtered agree in every final second: no insertion fell there.

**Exclusions.**
- Post-crash frames: 73–479 per crashed run, listed per tracker.
- Insertion artefacts set aside, all in the truck run: C 7, E 4, A 1; B 1 (mixed).
- Oracle frames changed by ignoring just-inserted bodies: 0–5 per run.

**Raw, all frames, is not meaningful after a crash.** The wrecked ego sits against a vehicle (e.g. 46 oracle-HIGH
frames after stationary B's crash). It is reported only so nothing is hidden.

### 9.6 Clearance by vehicle class, and the remaining bus and truck errors

| Case | Trk | Class | Matched frames | Optimistic | Coverage | Optimism p50 / p95 / max m | Optimistic within 20 m | Worst (m at lon, lat; basis) |
|---|---|---|---|---|---|---|---|---|
| expressway | C | autorickshaw | 478 | 0.00 | 1.00 | — | 0 | — |
| expressway | C | car | 1022 | 0.00 | 1.00 | — | 0 | — |
| expressway | C | motorcycle | 602 | 0.00 | 0.99 | — | 0 | — |
| expressway | D | autorickshaw | 226 | 0.00 | 1.00 | — | 0 | — |
| expressway | E | autorickshaw | 478 | 0.00 | 1.00 | — | 0 | — |
| expressway | E | car | 1018 | 0.00 | 1.00 | — | 0 | — |
| expressway | E | motorcycle | 596 | 0.00 | 0.99 | — | 0 | — |
| mixed_potholes | C | autorickshaw | 3458 | 0.00 | 0.99 | — | 0 | — |
| mixed_potholes | C | bus | 22 | 0.41 | 0.59 | 1.03 / 1.22 / 1.23 | 3 | 1.23 at -20.9, -7.5; predicted |
| mixed_potholes | C | car | 3422 | 0.00 | 1.00 | — | 0 | — |
| mixed_potholes | C | motorcycle | 4631 | 0.00 | 0.99 | — | 0 | — |
| mixed_potholes | C | truck | 2599 | 0.46 | 0.54 | 0.43 / 0.86 / 1.22 | 339 | 1.22 at 130.9, 8.9; radar |
| mixed_potholes | D | autorickshaw | 1277 | 0.12 | 0.87 | 1.05 / 1.45 / 2.06 | 159 | 2.06 at 20.2, 5.9; lidar |
| mixed_potholes | D | car | 1487 | 0.03 | 0.95 | 0.68 / 2.71 / 4.05 | 27 | 4.05 at 15.6, 1.5; lidar |
| mixed_potholes | D | motorcycle | 1756 | 0.01 | 0.96 | 0.21 / 1.35 / 1.41 | 17 | 1.41 at 17.2, 0.3; lidar |
| mixed_potholes | D | truck | 2877 | 0.31 | 0.50 | 2.49 / 5.05 / 9.15 | 345 | 9.15 at 25.8, -2.0; lidar |
| mixed_potholes | E | autorickshaw | 3926 | 0.08 | 0.90 | 1.04 / 1.64 / 2.62 | 317 | 2.62 at 19.3, -1.1; lidar |
| mixed_potholes | E | bus | 23 | 0.39 | 0.61 | 1.03 / 1.22 / 1.23 | 3 | 1.23 at -20.9, -7.5; predicted |
| mixed_potholes | E | car | 3484 | 0.00 | 0.98 | 0.26 / 2.77 / 3.90 | 14 | 3.90 at 12.6, 2.4; lidar |
| mixed_potholes | E | motorcycle | 4630 | 0.00 | 0.97 | 0.14 / 1.23 / 1.45 | 13 | 1.45 at -6.9, -2.1; radar+lidar |
| mixed_potholes | E | truck | 4033 | 0.40 | 0.55 | 0.73 / 3.76 / 9.15 | 350 | 9.15 at 25.8, -2.0; lidar |
| moto_cutin | C | autorickshaw | 245 | 0.00 | 1.00 | — | 0 | — |
| moto_cutin | C | bus | 429 | 0.99 | 0.01 | 0.91 / 1.59 / 5.93 | 10 | 5.93 at 77.9, -3.8; predicted |
| moto_cutin | C | car | 337 | 0.00 | 1.00 | — | 0 | — |
| moto_cutin | C | motorcycle | 400 | 0.00 | 0.94 | — | 0 | — |
| moto_cutin | D | bus | 458 | 0.08 | 0.89 | 2.59 / 4.79 / 4.81 | 16 | 4.81 at 21.7, 4.0; lidar |
| moto_cutin | D | motorcycle | 208 | 0.07 | 0.89 | 0.79 / 1.10 / 1.16 | 15 | 1.16 at 17.5, 0.2; lidar |
| moto_cutin | E | autorickshaw | 245 | 0.00 | 1.00 | — | 0 | — |
| moto_cutin | E | bus | 701 | 0.45 | 0.55 | 0.88 / 4.61 / 8.03 | 19 | 8.03 at 25.8, 4.0; lidar |
| moto_cutin | E | car | 337 | 0.00 | 1.00 | — | 0 | — |
| moto_cutin | E | motorcycle | 400 | 0.00 | 0.92 | — | 0 | — |
| rickshaw_cutin | C | autorickshaw | 939 | 0.00 | 0.99 | — | 0 | — |
| rickshaw_cutin | C | car | 187 | 0.00 | 1.00 | — | 0 | — |
| rickshaw_cutin | C | motorcycle | 252 | 0.00 | 1.00 | — | 0 | — |
| rickshaw_cutin | D | autorickshaw | 267 | 0.07 | 0.92 | 0.69 / 1.43 / 1.44 | 20 | 1.44 at -19.3, 5.1; lidar |
| rickshaw_cutin | E | autorickshaw | 936 | 0.01 | 0.98 | 0.68 / 1.14 / 1.19 | 3 | 1.19 at -19.5, 5.4; lidar |
| rickshaw_cutin | E | car | 183 | 0.00 | 1.00 | — | 0 | — |
| rickshaw_cutin | E | motorcycle | 250 | 0.00 | 1.00 | — | 0 | — |
| stationary | C | car | 72 | 0.03 | 0.97 | 0.61 / 0.83 / 0.86 | 0 | 0.86 at 26.0, 0.2; predicted |
| stationary | D | car | 39 | 0.00 | 0.97 | — | 0 | — |
| stationary | E | car | 84 | 0.00 | 0.98 | — | 0 | — |
| truck_brake | C | autorickshaw | 1261 | 0.00 | 0.98 | — | 0 | — |
| truck_brake | C | car | 1297 | 0.00 | 1.00 | 0.34 / 0.56 / 0.60 | 0 | 0.60 at 141.1, 7.2; radar |
| truck_brake | C | motorcycle | 94 | 0.00 | 0.95 | — | 0 | — |
| truck_brake | C | truck | 83 | 0.22 | 0.78 | 0.42 / 0.81 / 0.90 | 9 | 0.90 at 93.7, 5.0; radar |
| truck_brake | D | autorickshaw | 617 | 0.00 | 0.99 | 1.36 / 1.37 / 1.38 | 2 | 1.38 at 11.2, 4.5; lidar |
| truck_brake | D | car | 403 | 0.01 | 0.97 | 0.73 / 0.75 / 0.75 | 4 | 0.75 at 14.1, -2.9; lidar |
| truck_brake | D | motorcycle | 23 | 0.00 | 0.83 | — | 0 | — |
| truck_brake | D | truck | 110 | 0.29 | 0.50 | 2.52 / 2.53 / 4.14 | 24 | 4.14 at 16.5, 2.1; lidar |
| truck_brake | E | autorickshaw | 1295 | 0.00 | 0.98 | — | 0 | — |
| truck_brake | E | car | 1325 | 0.01 | 0.99 | 0.40 / 0.58 / 0.60 | 0 | 0.60 at 141.1, 7.2; radar |
| truck_brake | E | motorcycle | 92 | 0.00 | 0.86 | — | 0 | — |
| truck_brake | E | truck | 126 | 0.25 | 0.67 | 2.52 / 2.53 / 4.14 | 22 | 4.14 at 16.5, 2.1; lidar |

| Class | Optimistic | Coverage |
|---|---|---|
| Cars | ≤ 1 % (E) | 0.97–1.00 |
| Motorcycles | 0 % (E) | 0.86–0.99 |
| Autorickshaws | ≤ 1 %, except mixed traffic 8 % (lidar faces at 19 m, up to 2.6 m) | |

**Trucks and buses are not covered: the main unresolved safety error.**
- **Trucks.**
  - E is optimistic in 25 % (truck braking) and 40 % (mixed) of matched frames. In mixed traffic that is 350 frames
    with the body within ~20 m.
  - Worst case 9.15 m (lidar basis, 25.8 m ahead).
  - Partly a truth artefact [H]. The truth box for `TruckSimple` includes a 4.5 m × 0.75 m chassis block behind the
    cab, but the drawn model there is thinner and lower. The lidar sees the cab face, and the radar reports the
    centre of the box it is given.
  - Neither sensor can see what is not drawn, but a real truck's bed would be seen. The share caused by the artefact
    is not separated.
- **Buses.**
  - Radar only (C) is optimistic in 99 % of bus frames in the motorcycle run (up to 5.9 m at 78 m).
  - E 45 % (p95 4.6 m, max 8.0 m at 25.8 m, lidar basis).
  - Cause: the two calibration runs held too few buses for the radar slack, and the lidar's visible face was not
    always the bus's near surface. Unexplained in detail.
- These are failures by this report's own criterion X4 (§13). They were not tuned away.

### 9.7 Motorcycle false warnings (unresolved)

Mixed traffic, E: 163 false-HIGH frames involve 181 offending reports. Each was classified against the nearest
true body.

| Cause | Motorcycle, other lane | Motorcycle, ego lane | Truck | Car | Ghost | Autorickshaw |
|---|---|---|---|---|---|---|
| estimate close to truth: the rules' threshold edge | 51 | 6 | 3 | 10 | — | — |
| clearance lower bound > 1 m conservative | 36 | 9 | 18 | 5 | — | 3 |
| velocity error > 2 m/s | 12 | 5 | 2 | — | — | — |
| lateral velocity error > 0.5 m/s | 6 | — | 8 | — | — | — |
| no body within 2 m | — | — | — | — | 7 | — |

**What the counts show:**
- Motorcycles account for 125 of the 181 reports, 105 of them in another lane.
- Most are tracked close to truth. The unchanged `risk.py` gives every vehicle a 1.9 m wide body, so a motorcycle
  riding the lane line with a few tenths of a m/s lateral velocity is predicted to conflict.
- The oracle sees exactly true values and stays just below HIGH. The tracker's noise pushes it over.

This is a risk-rule sensitivity amplified by tracking noise, not a detection failure. A class-aware body width or
lateral-velocity uncertainty in the prediction would address it. Both are changes to authoritative logic, so they
are out of scope and not done.

## 10. Failure cases

| # | Failure | Where | Cause | Status |
|---|---|---|---|---|
| F1 | Truck / bus clearance optimistic up to 9 m | §9.6 | invisible low chassis (partly truth artefact [H]); radar slack under-calibrated for long vehicles; lidar face not the near surface | **unresolved** |
| F2 | False HIGH, motorcycles near lane lines | §9.7 | rules' fixed 1.9 m body + lateral noise | **unresolved** (authoritative logic) |
| F3 | Late lidar discarded | §9.4 | no out-of-sequence updates | **unresolved** (design limit) |
| F4 | Front-radar outage → late warning on a stationary lead | §9.4 | lidar-only confirmation needs a settled velocity (0.5–1 s) | **unresolved** |
| F5 | Insertion artefacts (bogus velocities 2 frames after spawn) | §12 | simulator | scored separately |
| F6 | Ego crashes (5 of 12 runs) | §12 | stock driving controller | scoring cut-off; final second reported |
| F7 | Lidar ground rings when braking | §5 | sensor pitch 0.077 rad | **fixed** (IMU levelling): false tracks 0.63 → 0.00 /frame |
| F8 | Lidar clearance optimistic for cars (bumper between beams) | §5 | beam gaps | **fixed** (face-height slack): 51 % → 0 % |
| F9 | Radar- and lidar-born duplicates of one car | §7 | no merge | **fixed** |
| F10 | Sliver cluster stole a neighbour's points → −10 m/s velocity, false CRITICAL | §7 | births from 3–4 points; confirmation before velocity settled | **fixed** |
| F11 | Bus near face not associated with its radar track | §7 | 1.5 m length σ too narrow | **partly fixed** (σ 3.0 m): E's overall optimism in the motorcycle run 48 → 40 %; bus frames still 45 % |
| F12 | Lead re-acquired under a new id while braking (stationary, 48.5–49 s) | demo | rapid deceleration outside the gate | open (1–2 ID switches per run) |

F7–F11 were found on the stationary, truck and motorcycle runs and fixed before the final replay. Those three
runs are therefore development data for E, not independent evidence.

## 11. Performance (Mac, Python 3.12, numpy 2.5) [V]

Measured in one uncontended process without memory tracing. The replay's own timing columns were taken under
tracemalloc and seven parallel processes, inflating lidar time ~13×; they are not used.
Evidence: `performance-uncontended.json`.

| Run | Config | Cycle p50 / p95 / p99 ms | Lidar stage p95 | Tracking p95 | Max tracks | RSSF frame | Queue (10 Hz, cap 2): max / drops | Processing delay p99 |
|---|---|---|---|---|---|---|---|---|
| mixed traffic | B (authoritative) | 0.20 / 0.28 / 0.38 | — | — | — | v1 JSON | — | — |
| | C | 2.1 / 4.0 / 4.7 | — | 3.7 | 22 | 23.9 kB p50 | 1 / 0 | 4.7 ms |
| | D | 12.5 / 13.6 / 42.3 | 12.7 | 0.9 | 14 | 23.9 kB | 1 / 0 | 42 ms |
| | **E** | **15.3 / 19.0 / 42.7** | 12.8 | 5.8 | 26 | 23.9 kB (max 26.2) | 1 / 0 | 43 ms |
| truck braking | B | 0.09 / 0.23 / 0.25 | — | — | — | — | — | — |
| | C | 0.6 / 3.0 / 3.9 | — | 2.7 | 19 | 22.6 kB | 1 / 0 | 3.9 ms |
| | D | 11.1 / 15.6 / 20.9 | 12.5 | 2.2 | 21 | 22.6 kB | 1 / 0 | 21 ms |
| | **E** | **12.0 / 20.5 / 25.6** | 12.5 | 6.8 | 29 | 22.6 kB (max 29.9) | 1 / 0 | 26 ms |

**Throughput and payload.**
- RSSF decode: 0.12–0.15 ms p95.
- Transport: ~24 kB per frame, ≈ 0.24 MB/s at 10 Hz. Lidar uint16 cm + zlib is ~20 kB of it; raw float32 would be
  115 kB.
- Effective frequency: 10.0 Hz in every replay (one perception cycle per sensor instant). Observation age at the
  receiver: 0 s in recorded data; a live link adds its own latency, not measured.

**Memory.**
- Python heap peak per configuration (tracemalloc): C 3–15 MB, D 5–12 MB, E 5–19 MB.
- Whole replay process peak RSS: 322 MB. That holds two complete recordings in memory, not a live requirement.

**Against the 100 ms budget.**
- E uses 15–19 % of the cycle at p50/p95 on the Mac. Its p99 (43 ms) comes from lidar clustering spikes.
- Before the Pi measurement, a 3.4–3.9× Mac → Pi ratio (Phase 7A.3 §8) predicted ~50–60 ms p50 and ~145–170 ms
  p99 there. The measured Pi figures are in §11.1.

### 11.1 Raspberry Pi 5 measurement [V]

Setup:
- Raspberry Pi 5 (4 GB), system Python 3.13.5 and the system `python3-numpy` 2.2.4 (already installed; no virtual
  environment or wheel was needed).
- `nice -n 10`, one process per recording, no memory tracing.
- The deployed service was running alongside, idle.
- Evidence: `pi-benchmark.json`, produced by `pi_bench.py` (both in the evidence folder), run from inside the bundle.

| Recording | Fused cycles | p50 / p95 / p99 / max ms | Cycles > 100 ms | Lidar stage p95 | Tracking p95 | Peak RSS | Queue (10 Hz, cap 2): max / drops | Processing delay p99 |
|---|---|---|---|---|---|---|---|---|
| stationary lead | 709 | 53.9 / 58.0 / 72.6 / 77.1 | **0 (0 %)** | 56.5 ms | 3.1 ms | 119 MB | 1 / 0 | 73 ms |
| truck braking | 681 | 59.5 / 95.7 / 117.8 / 131.5 | **23 (3.4 %)** | 59.6 ms | 34.9 ms | 145 MB | 1 / 0 | 172 ms |

**What the Pi measurement shows:**
- 1,390 fused cycles in all, 23 (1.7 %) over the 100 ms budget, all in the truck run.
- The authoritative pipeline on the same recordings: p95 0.09–0.73 ms.
- **Lidar processing** costs a near-constant ~55–60 ms: 4.4–4.8× the Mac.
- **Tracking** in dense traffic (truck run: 29 tracks) reaches 35 ms p95 and causes the overruns. The queue never
  dropped a frame (an overrun delays the next cycle and is absorbed), but processing delay reached 172 ms at p99.
- The mixed-traffic recording, the heaviest on the Mac (p99 43 ms vs 26 ms for the truck run), is not in the Pi
  bundle and was not measured. It would probably exceed the budget more often [H].
- Peak RSS is 119–145 MB, of which most is the loaded recording; the Pi had 2.7 GB available.
- **Decisions on the Pi are identical to the Mac**: the 30-line demo output of both recordings is the same except
  for the timing in its header.
- No thermal throttling (0x0 before and after; 46–50 °C).
- **Verdict:** typical cycles fit at 10 Hz with ~40 % headroom, but p99 does not fit in dense traffic. This confirms
  that fused perception is a recorded-data demonstration on the Pi, not a live 10 Hz function. No optimisation was
  attempted (freeze).


## 12. Limitations

**Evidence base.**
- Six scenario pairs, one seed each, 25 s after the event (mixed traffic 125 s, expressway 90 s). This is
  development evidence: too small for any acceptance claim, and the configurations were tuned while looking at
  three of these runs (stationary, truck braking, motorcycle cut-in). The remaining nine are less contaminated, not
  clean.
- **The ego is not driven by RoadSense.** The stock driving controller crashed in five of twelve runs (both stationary, the
  original autorickshaw and mixed-traffic runs, the corrected truck run). Scoring stops 1 s before each crash. The
  original mixed-traffic run never reached a pothole.
- `orig-truck_brake` never staged its event (SUMO found no clear slot). Its A row is ordinary traffic, not a braking
  truck.
- Original and corrected runs of a case share the seed but **not the traffic**. Different simulators, step sizes and
  vehicle models diverge within seconds. A vs B is a comparison of distributions, not of identical events.

**Scoring exclusions** (all counted in §9.5).
- Frames after a crash, and the last second before it, are outside the headline metrics. The last second is reported
  separately.
- HIGH frames caused only by just-inserted bodies are set aside.
- The oracle ignores bodies for 1 s after insertion.
- The replay's timing columns (under memory tracing and parallel load) are replaced by an uncontended measurement
  (§11).

**Simulator.**
- **Insertion artefact.** For ~2 frames after SUMO inserts a vehicle, Webots reports nonsense velocities (radar range
  rates of 10⁵, then −26 to −53 m/s). The scoring sets aside HIGH frames caused only by such bodies and counts
  them. A real road has no teleporting vehicles, but the trackers here are not hardened against it.
- **Smooth-sync acceleration jitter.**
  - Displayed bodies jitter at ±7 m/s² (p01/p99) at 10 Hz (§2.1), against SUMO's own ±1.7 m/s².
  - It enters the radar range rate (node velocity vs displacement differs by 0.27 m/s p95, 0.43 p99) and the
    truth's own velocities.
  - The tracker's 0.30 m/s range-rate σ and 3 m/s² process noise absorb most of it. It contributes to the lateral
    velocity noise behind F2 (§9.7).
  - Its effect was not separated from the trackers' own noise. Fixing it, by smoothing the extrapolated velocity
    (§2.1), needs a new simulation, which was not done (freeze).

**Calibration.**
- **The radar slack comes from two 60 s W9 runs.** Classes rare there (buses, trucks) are under-covered. A bus seen
  by radar alone is optimistic by up to 5.9 m (motorcycle cut-in run, 78 m ahead), and fused by up to 8.0 m
  (25.8 m ahead). Trucks: up to 9.15 m (§9.6).
- **Lidar slack** for faces higher than 1 m (truck cab, chassis unseen) rests on ~600 samples per range band.

**Not done.**
- No per-class models.
- No road curvature beyond the map's lane lines.
- No sensor timing jitter beyond what the simulator produced.
- The front corner radars stay excluded.

**Performance.** Measured in Python on the Mac (§11). Nothing here demonstrates Pi feasibility.

**Truth.**
- The drawn bounding bodies (W9 §3.1) are the best simulator reference. They are still boxes: a truck's chassis
  shape and a motorcycle rider are approximations.
- The oracle is RoadSense's own unchanged risk/safety rules on true bodies, not an independent judgement of danger.

## 13. Proposed fresh acceptance campaign (not run; needs approval)

**Purpose.** Accept or reject the experimental pipeline as a shadow candidate, on data none of this work has seen.
Stage 15's frozen plan stays frozen: this is a new plan, hashed before recording.

**Configuration.**
- Corrected baseline v1 (§2), patched AB build, with code and calibration frozen at the reviewed commit.
- The plan records the commit, `calibration.json` SHA-256, the Webots executable SHA-256 and the seeds.

**Scenarios.**

| Family | Variation | Runs |
|---|---|---|
| stationary lead | ego 60–100 km/h, lead in each lane | 8 |
| sudden braking | car / truck / bus lead, −4 to −8 m/s² | 9 |
| cut-in | motorcycle / autorickshaw / car, gentle and assertive, both sides | 12 |
| high-speed rear approach | closing 10–25 m/s, ego lane and adjacent | 6 |
| blind zone | vehicles holding alongside, both sides | 6 |
| truck / bus clearance | following at 10–40 m | 6 |
| occlusion | lead hidden behind a truck, revealed by a lane change | 6 |
| normal / mixed with potholes | dense Indian mix | 6 |

59 runs, about 60 s each. Seeds are drawn once and hashed.

**Ego.** The stock controller crashed in five of the twelve development runs. Either keep it and score only before any
crash (as here), or cap its speed. This is a decision for you.

**Metrics fixed before recording.** Thresholds are to be agreed at review; the values below are this work's
proposal.

| ID | Metric | Pass |
|---|---|---|
| X1 | ego-lane recall within 60 m (E) | ≥ B, and ≥ 0.95 |
| X2 | blind-zone recall, left and right (E) | ≥ 0.95 |
| X3 | false tracks per frame within 60 m (E) | ≤ 0.05 |
| X4 | clearance optimism (lower bound > truth + 0.1 m) | ≤ 2 % of matched frames, none within 20 m |
| X5 | missed HIGH frames vs oracle (E, clearance view) | ≤ B |
| X6 | false HIGH frames vs oracle (E) | ≤ 2 × B |
| X7 | first HIGH after a scored event | no later than B in any episode |
| X8 | robustness: any single-sensor outage (10 s) | X1 within 0.05 of the healthy run, no new false HIGH |
| X9 | transport: corrupt / stale / out-of-order frames reaching fusion | 0 |
| X10 | cycle time p99 on the Mac, Python | ≤ 30 ms (the Pi budget is set separately, §14) |

## 14. Longer-term Pi integration and ML retraining plan (not started; the minimal plan is §16)

**Pi (no connection, deployment or restart in this phase).**
1. **Benchmark offline first.**
   - Copy `roadsense.experimental` and a few recorded RSSF streams to the Pi's scratch directory.
   - Time `lidar.process`, the tracker and fusion per frame.
   - Read-only, like the Phase 7A.3 Stage 4/16 benchmarks; the service is untouched.
2. **Budget.** At 10 Hz, perception has 100 ms.
   - The Mac's fused p95 is in §11.
   - The Pi 5 typically runs NumPy code 3–5× slower than an M-series Mac [H].
   - The lidar clustering (grid hash in Python) is the hot spot. Options:
     - vectorise it;
     - drop to 8 layers;
     - process every other scan.
3. **Shadow on the Pi.**
   - A separate process subscribes to RSSF frames and writes shadow decisions to its own history table.
   - The authoritative service, its warnings and the dashboard are unchanged.
   - Bounded queue (2 frames, drop-oldest), as simulated in §11.
4. **Real sensors.** A real lidar and radar each need their own measurement models:
   - real slack;
   - real self-clutter;
   - real timing;
   - recalibrated from recordings.

   None of the simulator constants transfer.

**ML retraining (stop for review before generating any dataset).**
- The 7A/7A.2 models learned from labels and radar features affected by the defects in §8. They should not be
  retrained on the old corpus.
- **Proposal.**
  - Generate a new corpus on the corrected baseline.
  - Labels from drawn bodies, at the physical time.
  - Features from the experimental tracker's reports, including uncertainties.
  - Split by seed and scenario family, never by frame.
- Only after X1–X10 pass and you approve the dataset plan.

## 15. Recommendation and readiness

### 15.1 Strongest configuration, by measurement

**Among the experimental configurations: E (fused).** It has:
- missed warnings of 2.0 s, tied with C (1.9 s) and half of D's (4.2 s);
- half of C's false warnings (19.1 s vs 42.6 s);
- the best or tied recall;
- the only configuration whose clearance is reliable for cars, motorcycles and autorickshaws together.

C is cheaper and as accurate in position, but its false-warning rate and its bus clearance rule it out. D alone is
too slow to confirm and misses the stationary lead.

**Against the original authoritative pipeline (B), on the same recordings, E is not better overall:**

| | B (authoritative) | E (fused) |
|---|---|---|
| Missed HIGH (6 cases) | 9.0 s | **2.0 s** |
| False HIGH (6 cases) | **9.2 s** | 19.1 s |
| Earliest HIGH, stationary lead | +1.6 s after event | **+1.0 s** (= oracle) |
| Clearance | not modelled | intervals; optimistic for trucks/buses |
| Cost on the Mac, p50 | **0.1–0.2 ms** | 12–15 ms |

E warns earlier and misses less, but twice as often wrongly. It also reports clearance that is unsafe for long
vehicles.

**Against this report's own criteria (§13, fixed before the final replay), on development data:**

| Criterion | Result | Verdict |
|---|---|---|
| X3 false tracks ≤ 0.05/frame | truck 0.08; mixed 0.18 | **fail** |
| X4 clearance optimism | trucks 25–40 % | **fail** |
| X5 missed ≤ B | in every case | **pass** |
| X6 false ≤ 2 × B | rickshaw 1.6 s vs 0; expressway 0.5 s vs 0; motorcycle 0.2 s vs 0; mixed 1.9× | **fail** |
| X8 single-sensor outage | front-radar outage: recall −0.17 on the stationary lead | **fail** |
| X9 transport | 0 corrupt frames reached fusion | **pass** |
| X10 p99 ≤ 30 ms (Mac) | mixed 42.7 ms; Pi truck run 117.8 ms | **fail** |

The criteria are not relaxed here.

**Recommendation for the final prototype.**
1. **Live system:** the existing deployed RoadSense on the Pi with its authoritative pipeline (the A path: installed
   Webots, original world), unchanged. It is the stable, verified configuration. The corrected simulator needs the
   patched, ad-hoc-signed Webots build and is for recorded evaluation only.
2. **Experimental fusion (E):** shown as a **separate recorded-data shadow demonstration** (§17). It is not
   integrated into the live path.

### 15.2 Is the fusion pipeline stable enough for an experimental demonstration?

**As software: yes.**
- Across 12 runs and 5 robustness variants (≈ 40,000 perception cycles), it raised no exception.
- No corrupt, stale or out-of-order frame reached fusion.
- The bounded queue never exceeded one frame uncontended.
- It produced identical decisions under Python 3.12/numpy 2.5 and Python 3.13/numpy 2.2.
- The shadow path never touches authoritative decisions (tested).

**As a safety function: no.**
- F1–F4 are unresolved.
- It fails five of the seven criteria checked above.

So: fit for a clearly labelled experimental demonstration on recorded data, not for live integration.

## 16. Raspberry Pi compatibility and the minimal shadow integration plan

### 16.1 Compatibility (inspected locally; the Pi was not contacted)

| Item | Finding |
|---|---|
| Pi Python | 3.13.5 (recorded in Phase 7A.3 §8 and §12) |
| Runtime dependencies of `roadsense.experimental` | **numpy only** (plus the standard library: `struct`, `zlib`, `math`) |
| SciPy | **not used** anywhere (the Hungarian assignment is our own) |
| scikit-learn, joblib | not used |
| Compressed lidar transport | `zlib` and `struct`: standard library, available on every Python |
| Python 3.13 + numpy 2.2.4 | all 59 experimental and lidar tests pass. Fused decisions on a recorded run are identical to Python 3.12 + numpy 2.5. numpy 2.2 on macOS prints harmless overflow warnings in a matrix product over no-return (NaN) beams; output unchanged |
| Python 3.13 + numpy 1.26.4 | all 59 tests pass |
| numpy on the Pi | **2.2.4, already installed system-wide** (`python3-numpy`), so Step 3 was not needed [V]. The deployed service does not import it: the service runs with only the `web` extra, and the project keeps numpy off it by design (`docs/learned-risk.md`) |
| Live sensor source for fusion | **none.** The live world (`roadsense_highway.wbt`) has RoadSense's radars and cameras only. The lidar and extra radars exist only in generated sensor-profile worlds, which record to disk and stream nothing. A live RSSF sender would be new development |
| Measured Pi cost | 54–59 ms p50, 73–118 ms p99; 23 of 1,390 cycles over 100 ms (§11.1) [V] |

### 16.2 Plan and execution (approved and run 2026-11-18)

**What was executed.**
- Steps 1, 2 and 4 ran as written. Step 3 was skipped: numpy 2.2.4 was already present.
- Before and after: service `roadsense-edge` active, MainPID 1038, 0 restarts, `/api/system` 200, no warnings in its
  journal.
- `~/roadsense-edge` was not modified (directory last changed 16 November).
- The 36 bundle files passed `sha256sum -c`.
- **Network finding.** `SmashMario.local` resolved to an IPv6 link-local address. That route dropped during the
  first copy, and the Pi was then reached at its Wi-Fi IPv4 address with the recorded host key enforced
  (`-o HostKeyAlias=smashmario.local`). The first, partial copy was deleted and the copy repeated.
- The copy used `tar` over `ssh` rather than `scp`.

The commands below are the plan as approved.

**Step 0, the fallback, already in place.**
- The deployed service `roadsense-edge` (systemd user unit, port 8001, events-only history) stays exactly as it is.
- No file in `~/roadsense-edge` changes, and no restart happens.
- If anything below fails, nothing needs rolling back: the service was never touched.

**Step 1, read-only preflight** (one SSH session).

```sh
ROADSENSE_PI=omgaupale@SmashMario.local       # confirm the address first
ssh -o BatchMode=yes -o ConnectTimeout=5 "$ROADSENSE_PI" '
  hostname; python3 --version; df -h ~ | tail -1; free -m | head -2
  python3 -c "import numpy; print(numpy.__version__)" || echo "numpy: absent"
  systemctl --user show roadsense-edge -p MainPID -p NRestarts -p ActiveState
  curl --fail -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8001/api/system'
```

**Step 2, copy the self-contained bundle** into a new, empty directory. No `~/roadsense-edge` file is read or
written.
- Bundle: `~/Library/Application Support/RoadSense/analysis/fusion-demo-bundle-20261118` (158 MB, with
  `SHA256SUMS`). It holds `roadsense.autonomy` + `roadsense.experimental`, the replay tool and two recorded runs.

```sh
ssh "$ROADSENSE_PI" 'test ! -e ~/roadsense-fusion-demo-20261118 && mkdir ~/roadsense-fusion-demo-20261118'
scp -r "$HOME/Library/Application Support/RoadSense/analysis/fusion-demo-bundle-20261118/." \
  "$ROADSENSE_PI":roadsense-fusion-demo-20261118/
ssh "$ROADSENSE_PI" 'cd ~/roadsense-fusion-demo-20261118 && sha256sum -c --quiet SHA256SUMS && echo verified'
```

**Step 3, only if Step 1 reports numpy absent.** Install it into a private virtual environment *inside the demo
directory*, never into the service's dependencies:

```sh
ssh "$ROADSENSE_PI" 'cd ~/roadsense-fusion-demo-20261118 && python3 -m venv .venv && .venv/bin/pip install "numpy==2.2.4"'
```

**Step 4, run the shadow demonstration and timing** at low priority. Then re-check that the service's MainPID and
NRestarts are unchanged.

```sh
ssh "$ROADSENSE_PI" 'cd ~/roadsense-fusion-demo-20261118 && PY=$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 ) &&
  PYTHONPATH="$PWD/src:$PWD/tools" nice -n 10 $PY -s tools/experimental_replay.py demo runs/corr-stationary --from 44 --to 50 &&
  PYTHONPATH="$PWD/src:$PWD/tools" nice -n 10 $PY -s tools/experimental_replay.py demo runs/corr-truck_brake --from 47 --to 53'
ssh "$ROADSENSE_PI" 'systemctl --user show roadsense-edge -p MainPID -p NRestarts -p ActiveState'
```

The demo header prints the fused cycle p95 on the Pi. That is the first real measurement of Pi feasibility.

**Removal.** `rm -rf ~/roadsense-fusion-demo-20261118` on the Pi removes everything Steps 2–4 created.

**Not planned: live shadow fusion on the Pi.** It would need:
- a streaming RSSF sender in the sensor-profile controller;
- a lidar in the live world;
- a separate shadow receiver process on the Pi.

That is new development, outside the freeze, and the measured readiness (§15) does not justify it.

## 17. Trained models and the new pipeline

| Model | Input contract | Trained on | Compatible with the experimental reports? |
|---|---|---|---|
| Phase 7A logistic (`roadsense.forward-risk.v1`, 30 features) | `history.features(state)` slots | legacy tracker on the original simulator; Phase 6D labels | **mechanically yes, validly no** |
| Phase 7A.2 `roadsense-7a2-hgb15-v1` (44 features, v2 contract) | the same, plus causal trends | the same, expanded | **mechanically yes, validly no**; it also **failed** its acceptance |

**Mechanically compatible.** The experimental reports carry every field the feature extractor reads:
- `relative_position`, `relative_velocity`, `relative_speed_mps`;
- `age_s`, `last_seen`, `lane_relation`.

**Statistically incompatible.** Both models learned from:
- legacy-tracker features, at 5 Hz, on the installed simulator, with its radar-origin bearing defect;
- labels defined on wrapper-referenced intended boxes (§8).

The experimental tracker differs in noise, age semantics, rate and lane assignment. Feeding it to these models would
be an untested domain shift. **They are not connected to the new pipeline.**

**Recorded-data ML demonstration (separate, prepared and verified).**
- `tools/pi_offline_demo.py` replays the frozen 7A.2 model on recorded feature frames.
- Its label states: "EXPERIMENTAL offline demonstration; failed predictive acceptance; NOT safety-authoritative".
- Bundle: `~/Library/Application Support/RoadSense/analysis/ml-demo-7a2-20261118`, holding 22,868 frames from 40
  held-out runs.
- Mac check today:
  - parity 0 differences;
  - 35.6 µs p50 per frame (features + model);
  - 1,211 offline warnings over the set.
- The same demo already ran on the Pi in Phase 7A.3 (`~/roadsense-offline-demo-7a2-20261117`, 128 µs p50, parity
  5.6e-17). It can be re-run there with your approval, without copying anything.

**A model for the new pipeline needs a new corpus** on the corrected baseline (§14). That is not started, as
instructed.

## 18. Final demonstration: exact startup sequence

**Part 1, live system** (the stable deployment; Pi untouched unless it is down)
1. Power the Pi and the Mac on the same network. Find the Pi's **IPv4** address with the router's DHCP list or
   `ssh omgaupale@SmashMario.local 'hostname -I'`, and use it as `PI_IP` (it was `192.168.0.102` on Wi-Fi on
   18 November). Don't use `SmashMario.local` in URLs: it resolved to an IPv6 link-local address that dropped
   during testing.
2. Check the service (read-only):
   `curl --fail http://PI_IP:8001/api/system`. If this fails, see Fallback below; do not restart without approval.
3. Open `http://PI_IP:8001/` in Vivaldi. Before Webots runs, it shows **NO PRODUCER**.
4. Mac, terminal 1:
   ```sh
   cd ~/Desktop/PROJECTS/ROADSENSE
   export SUMO_HOME="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/share/sumo"
   export PATH="/Library/Frameworks/EclipseSUMO.framework/Versions/1.27.1/EclipseSUMO/bin:$PATH"
   export WEBOTS_EXTRA_PROJECT_PATH="/Applications/Webots.app/Contents/projects/vehicles"
   export ROADSENSE_EDGE_URL=http://PI_IP:8001/api/sensor-frame
   /Applications/Webots.app/Contents/MacOS/webots simulation/webots/worlds/roadsense_highway.wbt
   ```
5. Within a few seconds the dashboard shows live tracks and decisions. `curl http://PI_IP:8001/api/live-state`
   returns 200.

**Verified 18 November** (the deployed service, a 3-minute run over Wi-Fi):
- **Frames:** 762 accepted, 0 rejected, 47 missed. The publisher logged two Wi-Fi outages and resumed by itself.
- **Pi timing:** processing p95 2.1 ms, handler p95 6.7 ms.
- **Live state:** 7 tracks and 4 hazards (simulated potholes).
- **Ollama** (`qwen3:0.6b`) explanations: p50 2.9 s, p95 6.3 s.
- **History:** 33 decisions recorded, none lost.
- **Service afterwards:** still MainPID 1038, 0 restarts.
- **Dashboard:** the page and its assets are served (HTTP 200). A rendered screenshot could not be taken, because
  headless Vivaldi produces no screenshot on this Mac (the known DevTools limitation). **Open it manually in
  Vivaldi before presenting.**
- **For the presentation:** prefer Ethernet or a stable Wi-Fi link between the Mac and the Pi.

**Part 2, experimental fusion, recorded-data shadow demo on the Pi** (verified 18 November; identical output to
the Mac)

```sh
ssh omgaupale@PI_IP 'cd ~/roadsense-fusion-demo-20261118 &&
  PYTHONPATH="$PWD/src:$PWD/tools" nice -n 10 python3 -s tools/experimental_replay.py demo runs/corr-stationary --from 44 --to 50 &&
  PYTHONPATH="$PWD/src:$PWD/tools" nice -n 10 python3 -s tools/experimental_replay.py demo runs/corr-truck_brake --from 47 --to 53'
```

Each replay takes about 40–50 s on the Pi, because it replays the whole recording before printing the window. Its
header prints the fused cycle p95 measured on the Pi (58 ms and 97 ms).

The same on the Mac (no network), with the comparison tables:

```sh
cd ~/Desktop/PROJECTS/ROADSENSE
R="$HOME/Library/Application Support/RoadSense/campaigns/experimental-perception-20261118/runs"
PYTHONPATH=src venv/bin/python tools/experimental_replay.py demo "$R/corr-stationary" --from 44 --to 50
PYTHONPATH=src venv/bin/python tools/experimental_replay.py demo "$R/corr-truck_brake" --from 47 --to 53
PYTHONPATH=src venv/bin/python tools/experimental_replay.py tables \
  docs/evidence/experimental-perception/results.json docs/evidence/experimental-perception/warnings.json
```

What it shows:
- the stationary lead: shadow HIGH at +1.0 s with the oracle, the authoritative pipeline at +1.6 s;
- the truck run, including its conservative CAUTIONs;
- the full comparison tables.

**Part 3, recorded-data ML demonstration** (separate; the frozen Phase 7A.2 model on its own historical feature
frames; never connected to the live pipeline)

Mac, verified 18 November (parity 0):

```sh
cd "$HOME/Library/Application Support/RoadSense/analysis/ml-demo-7a2-20261118"
nice -n 10 python3.12 -s pi_offline_demo.py bench . "$HOME/Desktop/ml-demo-report.json"
```

Pi: the Phase 7A.3 bundle is still in place, and its checksums were verified on 18 November. It was not re-run this
session.

```sh
ssh omgaupale@PI_IP 'cd ~/roadsense-offline-demo-7a2-20261117 && nice -n 10 python3 -s pi_offline_demo.py bench . ~/ml-demo-report.json'
```

**Separation.** None of the three demonstrations can override another:
- Part 1 is the only one that makes safety decisions.
- Parts 2 and 3 read recordings and print text.

**Fallback, if the Pi is unreachable** (everything on the Mac)
1. Mac, terminal 0:
   `PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python312" python3.12 -m roadsense.web --no-browser`
2. Open `http://127.0.0.1:8000/`.
3. In terminal 1, set `ROADSENSE_EDGE_URL=http://127.0.0.1:8000/api/sensor-frame` before launching Webots.

Parts 2 and 3 also run on the Mac (commands above), so no part of the demonstration depends on the Pi.

**Shut down.**
- Close Webots. The Pi service keeps running as before.
- Stop the local fallback server with Ctrl-C, if used.
