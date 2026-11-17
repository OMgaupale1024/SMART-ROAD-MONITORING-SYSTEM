# Phase 7A.3 — Perception and track observability (investigation and proposal)

Status: **investigation complete, changes proposed, nothing in the authoritative pipeline modified.**
No simulation was run and no model was trained or retrained for this phase. Phase 7B (live integration) is not started.

The frozen Phase 7A.2 model `roadsense-7a2-hgb15-v1` failed predictive acceptance and stays frozen and
experimental. Its source, compact evidence, model and failed results were published in `29443b6`
([RESULTS](evidence/phase7a2/RESULTS.md), [learned-risk.md](learned-risk.md)).

## 1. Method

`tools/perception_audit.py` (offline, read-only) joins, frame by frame, the 140 runs of the Phase 7A.2 campaign:

| Layer | Source |
|---|---|
| Truth (evaluation only) | `runs/*/ground_truth.jsonl` v2 physical Webots poses and staging events |
| Observability | geometric model of the two RoadSense radars: mount, range, 2.6 rad FOV, occlusion by nearer vehicle boxes |
| Tracked objects | the recorded slot features (`lead`, `follower`, `left`, `right`) in the campaign history database |
| Temporal features / model | the v2 causal features and the frozen model's scores (`features-final/samples.jsonl`) |
| Labels | `tools/scenario_dataset.outcomes`, the same near-miss definition the labels used |

Every slot frame is classified as `correct`, `empty_ok`, `missed_{fov,range,occluded}`, `missed_confirming`
(within 0.6 s of first visibility), `missed_tracking`, `spurious`, `wrong_vehicle`, `lane_misassigned` or `mislocalized`.

**Limitation:** raw radar targets were not recorded, so visibility is a geometric model, not the radar's own
output, and a candidate tracker cannot be replayed against this campaign. This shapes the implementation plan (§6).

```
PYTHONPATH=src python3 tools/perception_audit.py CAMPAIGN OUT_DIR     # ~15 s; refuses an existing OUT_DIR
```

Compact result: [evidence/phase7a3/perception-audit.json](evidence/phase7a3/perception-audit.json). The 106 MB per-frame trace
stays local, at `~/Library/Application Support/RoadSense/analysis/phase7a3-20261117/`.

## 2. Sensors actually available

| Sensor | Used by RoadSense tracking | Notes |
|---|---|---|
| `radar front` (x 4.05 m, 150 m, 2.6 rad) | yes | rangeNoise 0.25 m, speedNoise 0.12 m/s, occlusion on |
| `radar rear` (x −1.16 m, facing back, 100 m, 2.6 rad) | yes | |
| 60 m / 0.9 rad radar | no | belongs to the sample child robot |
| IbeoLux `ibeo 3`, `ibeo 4` (rear corners) | **no, never enabled** | RoadSense has **no LiDAR observations** today |
| distance sensors left/right (4 m, 2 rays) | lane availability only | the only coverage of the zone alongside |
| hazard camera | potholes only | |

The two radars leave a **blind zone alongside the ego** (between the two 2.6 rad fields of view), measured as
`missed_fov` in the adjacent-lane slots: left 213, right 291 frames.

## 3. Findings

### 3.1 Missing tracks: a hard speed cliff in confirmation (root cause found)

An unconfirmed track has no velocity, so it is predicted stationary and must re-associate inside
`NEW_TRACK_GATE_M = 5.0` m. At 5 Hz a target whose *world* speed exceeds 25 m/s moves more than 5 m between
frames and **never confirms**: each detection opens a new track. A noiseless synthetic test against the real
`Tracker` confirms at 24.9 m/s and never at 25.1 m/s (`tests/test_perception_audit.py`). Vehicles above 90 km/h
are invisible to the tracker; with noise the boundary is lower.

Missed-tracking rate for observable vehicles, after the 0.6 s confirmation allowance:

| World speed | 0–5 | 5–10 | 10–15 | 15–20 | 20–25 | 25–30 m/s |
|---|---|---|---|---|---|---|
| miss rate | 20.2% | 5.9% | 2.3% | 6.1% | 10.0% | **33.4%** |

| Range | <25 | 25–50 | 50–75 | 75–100 | 100–125 | 125–150 m |
|---|---|---|---|---|---|---|
| miss rate | 2.0% | 1.1% | 2.8% | 2.5% | 13.6% | 22.4% |

By type: motorcycle 13.7%, car 3.4%, truck 2.3%, autorickshaw 1.2%, bus 1.0%. Within 80 m the motorcycle rate
drops to 2.0% (slow) / 3.5% (≥22 m/s); its high overall rate is mostly long range (small radar cross-section
near the range limit) plus speed. Cars ≥22 m/s within 80 m: 7.5%. The 0–5 m/s band (20%) is vehicles that
are stopped or just inserted, where the zero-velocity prediction is right but confirmation competes with
the staging insertion.

### 3.2 Slot assignment, association and lane errors

| Slot | correct | missed_tracking | missed_confirming | spurious | wrong_vehicle | mislocalized | lane_misassigned |
|---|---|---|---|---|---|---|---|
| lead | 27,760 | 2,159 | 521 | 1,278 | 411 | 228 | 89 |
| left | 47,022 | 1,232 | 419 | 1,252 | **3,250** | 897 | 350 |
| right | 9,645 | 1,670 | 499 | 899 | 1,330 | 297 | 130 |
| follower | 11,246 | 491 | 77 | 87 | 35 | 31 | 414 |

`wrong_vehicle` (the slot holds a real vehicle, but not the nearest one in that lane) dominates the adjacent
slots: a coasting track for a vehicle that has entered the blind zone, or a vehicle missed by the cliff, is
replaced by the next one. Most `spurious` slots are coasting tracks (up to 3 s) whose vehicle has left or
been occluded.

### 3.3 Identity switches and fragmentation

Over 100,510 continuous matched slot frames: **106 identity switches** (the slot's track age continues but
the matched vehicle changes) and **190 fragmentations** (the same vehicle reappears under a younger track
within 3 s). Combined 2.9 per 1,000 matched frames. Greedy nearest-pair association with a 3 m isotropic gate
causes both: two vehicles in adjacent lanes 3.75 m apart are within gate range of each other's predictions.

### 3.4 Kinematics of correctly assigned slots

| Quantity | MAE | p95 | mean (bias) |
|---|---|---|---|
| lon | 1.57 m | 2.96 m | −1.56 m |
| lat | 0.31 m | 0.98 m | +0.03 m |
| v_lon | 0.30 m/s | 0.82 m/s | 0.00 |
| v_lat (vehicle steady) | 0.14 m/s | 0.54 m/s | 0.00 |
| v_lat (vehicle moving laterally, \|v\|>0.3) | **0.57 m/s** | **2.08 m/s** | −0.15 |

The lon bias is a reference-point convention: radar returns the nearest surface, truth is the box centre.
It is consistent and not an error to fix in tracking.

Lateral velocity is the weak quantity. It is a (last − first) difference over a 1.5 s window, so it lags real
lateral motion and is noisy on young tracks: on steady vehicles, a false |v_lat| > 0.3 m/s appears in 28.9% of
frames for tracks younger than 1.5 s, 14.5% at 1.5–3 s and 8.4% beyond; it is worst within 20 m (18.9%)
where the radar's angular noise is largest in metres per unit angle change. **Lateral cue latency** (true
lateral-motion onset to the recorded slot first showing it): p50 0.6 s, p90 2.0 s, max 7.2 s over 584 vehicles.

### 3.5 Missed early-warning incidents: where each failed

The 8 missed test incidents and 3 missed stress incidents of Phase 7A.2, traced frame by frame over the
15 frames (3 s) before each label onset:

| Incident | Failure stage |
|---|---|
| test combined-1105 @52.8, combined-1110 @18.2, sudden_brake-1112 @49.2 | **label artifact**: the staged lead's first recorded speed is 0.0, giving a one-frame TTC near miss; the threat did not exist during the warning window |
| test cut_in-1106 @52.4 | autorickshaw inserted 2.4 s before onset; mixed: not yet inserted (3), blind zone (2), not in any slot (2), lateral not estimated (5), model below threshold (3) |
| test cut_in-1109 @14.4 | unstaged SUMO lane change; lateral lag (5 frames), model (10) |
| test cut_in-1109 @44.0 | motorcycle tracked as lead with correct kinematics; **model** max 0.725 < 0.760 |
| test cut_in-1109 @77.8 | motorcycle; **lateral lag** (truth −1.2 m/s, recorded −0.35 m/s); model max 0.685 |
| test sudden_brake-1102 @73.2 | motorcycle lead tracked correctly; braking not observable until ~0.6 s before onset; **model** max 0.691 |
| stress cut_in-1123 @99.2 | motorcycle in the **radar blind zone** for all 15 frames; the right slot held a stale coasting track |
| stress cut_in-1126 @63.6 | truck tracked as lead, correct kinematics; **model** 0.553 |
| stress cut_in-1128 @44.4 | motorcycle; blind zone (6), not in a slot (2), model (7) |

So of 11 misses: 3 are label artifacts, 2 are dominated by sensing coverage (blind zone / late insertion),
2 by lateral-velocity lag, and 4 had good tracking and failed in the model. Perception is a real but partial
cause; the model and the scenario staging are the rest.

**Insertion-speed artifact, dataset-wide:** train 15 incidents (91 positive frames, ~15% of new-train
positives), validation 3 (30), test 5 (45 of 311 test positives, ~14%), stress 2 (0). This is a post-hoc
observation; the frozen 7A.2 results are not changed by it.

### 3.6 Are radar observations early enough for cut-ins?

63 staged cut-ins (autorickshaw 34, motorcycle 19, car 10; 3 more never executed). Medians:

| Interval | p50 | p90 |
|---|---|---|
| insertion → first confirmed track | 0.6 s | 2.2 s |
| staging `act` → true lateral onset | 0.4 s | |
| true lateral onset → recorded lateral cue | 0.6 s | 1.2 s |
| true lateral onset → entering the ego lane | 1.0 s | |

38 of 63 never entered the ego lane ahead; 6 entered but were never recorded as lead. For the 11 cut-ins whose
cutter made a true near miss, the near miss came 0.8–1.6 s after the true lateral onset in 7 cases and 1.8–4.6 s
after insertion in most. **The physical warning window the staging creates is shorter than the 3 s horizon**,
so a 70% pre-onset recall target is not reachable from any sensor for these scenarios, independent of
the tracker. The existing radars see these cutters early enough *when they are ahead and outside the blind
zone*; what they lack is (a) any coverage alongside and (b) a lateral velocity that responds in under 0.5 s.
LiDAR cannot contribute today because it is not enabled.

## 4. Proposed architecture changes (none applied)

`tracking.py`, `perception.py` and `pipeline.py` are the authoritative safety pipeline; every item marked **A**
needs approval. All changes keep the output contract (tracks, slots, unified safety) unchanged.

| # | Change | Fixes | Auth. |
|---|---|---|---|
| P1 | **Initialise velocity at birth** from radar range rate (radial component, already stored) plus ego velocity, and use an **anisotropic new-track gate** in the ego frame (≈9 m along the road, ≈1.5 m across) | the 25 m/s cliff, 0–5 m/s churn, part of fragmentation | A |
| P2 | **Constant-velocity Kalman filter per track** (state x, y, vx, vy; radar range/azimuth/range-rate measurement; process noise tuned per axis) replacing the 1.5 s finite difference | lateral lag and young-track noise, v_lat accuracy | A |
| P3 | **Global (Hungarian or gated-optimal) association** with a Mahalanobis gate from P2's covariance instead of greedy nearest pair with a fixed 3 m gate | identity switches between adjacent lanes | A |
| P4 | **Coasting by covariance, not a fixed 3 s**; drop a coasting track when its predicted position leaves every sensor's FOV or its uncertainty exceeds a lane width; **hand-off** between front and rear radar through the blind zone | spurious/stale slots, wrong_vehicle in adjacent slots | A |
| P5 | **Blind-zone fusion**: feed the side distance sensors into tracking as presence/lateral-gap measurements, and optionally enable the two IbeoLux LiDARs (world change: enable + add to the frame) for the rear quarters | missed_fov alongside, cut-ins from alongside | A (+ world change) |
| P6 | **Track IDs and filter covariance in the slot features** (new feature schema v3; v1/v2 untouched) | model cannot tell a new vehicle from the same one | features only |
| P7 | **Data/label fixes**: record `departSpeed` (or skip a vehicle's first frame in `outcomes`) and stage cut-ins with a realistic observable lead time | 3 of 11 misses, ~14% of positives | tools only |
| P8 | **Raw frame capture** (opt-in env flag in the ego controller, writes the frame dict it already builds) | makes tracker A/B replay possible at all | diagnostic, off by default |

P1 alone is a small diff and removes the largest single defect; P2–P4 are one coherent tracker rewrite inside
`tracking.py` with the same public API.

## 5. Tracking acceptance criteria (fixed before any implementation)

Measured by `tools/perception_audit.py` on the **same captured frames** replayed through the baseline and the
candidate tracker (§6). A criterion fails the candidate if any limit is missed.

| # | Metric | Baseline (7A.2 campaign) | Accept |
|---|---|---|---|
| T1 | synthetic constant-velocity target, every world speed 0–40 m/s and lateral speed 0–2 m/s, noiseless and with the world's noise | never confirms > 25 m/s | confirms ≤ 0.6 s at every speed |
| T2 | missed_tracking, observable within 100 m, each speed band incl. 25–30 m/s | 2.3–33.4% (all ranges) | ≤ 3% in every band |
| T3 | missed_tracking, observable within 100 m, each vehicle type | car ≥22 m/s 7.5% (≤80 m) | ≤ 3% every type |
| T4 | missed_tracking 100–150 m | 13.6% / 22.4% | ≤ 10% (report; radar-limited) |
| T5 | identity switches + 3 s fragmentations per 1,000 matched slot frames | 2.9 | ≤ 1.0 |
| T6 | wrong_vehicle + spurious, share of non-empty adjacent-slot frames | 8.5% left, 18.1% right | ≤ 5% |
| T7 | v_lat MAE / p95 during lateral motion | 0.57 / 2.08 m/s | ≤ 0.30 / ≤ 1.00 m/s |
| T8 | false lateral motion (\|v_lat err\| > 0.3 m/s) on steady vehicles, tracks < 1.5 s / all | 28.9% / 8.4% | ≤ 10% / ≤ 4% |
| T9 | lateral cue latency p50 / p90 | 0.6 / 2.0 s | ≤ 0.4 / ≤ 0.8 s |
| T10 | lon / lat MAE on correct slots | 1.57 (bias) / 0.31 m | no worse than baseline +10% |
| T11 | blind zone: frames with a vehicle alongside (\|lon\| < 5 m, adjacent lane) represented in the slot | 0 (radar-only) | ≥ 90% (only if P5 approved) |
| T12 | rule-based safety: full test suite, `tests/browser_smoke.py` in Vivaldi, and a decision diff of the recorded replays reviewed by hand | — | all pass, every changed decision explained |
| T13 | tracker update cost on the Pi 5 at 5 Hz (measured on the Pi, not the Mac) | not measured | p99 ≤ 2 ms per frame |

Model metrics are deliberately not tracking criteria: a retrain is a separate, later approval (§6 step 6).

## 6. Implementation plan (each step waits for approval of the previous result)

1. **P8 capture (small).** Opt-in `ROADSENSE_FRAME_CAPTURE` in `roadsense_ego.py` appends the frame dict to
   `runs/<id>/frames.jsonl`; off by default, no effect on the pipeline. Add `tools/tracker_replay.py`
   (frames → `pipeline.process` → slots in the audit's format). *Needs approval for a small diagnostic set,
   proposed: the 30 test+stress scenario specs of 7A.2 re-run once (about 1 h), not a campaign.*
2. **Baseline on captured frames.** Replay with the current tracker, run the audit, confirm the baseline
   numbers reproduce within noise (Webots/SUMO staging is not bit-deterministic).
3. **P1** (gate and velocity at birth) with T1 as a failing test first; replay; report T1–T13.
4. **P2–P4** tracker rewrite behind the same API; replay; report. P5 only if T11 is wanted and the world change
   is approved.
5. **T12/T13**: suite, Vivaldi smoke, decision diff, Pi timing. Commit tracker changes only on a full pass.
6. **Then, separately:** P6/P7, label protocol correction, and a new data collection and training round,
   each with its own approval. The frozen 7A.2 model and its failed results are never altered.

## 7. Affected files

| File | Change | Step |
|---|---|---|
| `simulation/webots/controllers/roadsense_ego/roadsense_ego.py` | opt-in frame capture | 1 |
| `tools/tracker_replay.py` (new) | offline replay of captured frames | 1 |
| `tools/perception_audit.py` | read replayed slots as well as history | 1 |
| `src/roadsense/autonomy/tracking.py` | P1–P4 (**authoritative**) | 3–4 |
| `src/roadsense/autonomy/perception.py` | range-rate in detections, side-sensor measurements (**authoritative**) | 3, P5 |
| `src/roadsense/autonomy/pipeline.py` | pass side-sensor/LiDAR measurements to tracking (**authoritative**) | P5 |
| `simulation/webots/worlds/roadsense_highway.wbt` | enable IbeoLux (P5 only) | P5 |
| `src/roadsense/web/history.py`, `src/roadsense/expanded_risk.py` | v3 slot features with track ID (new schema) | 6 |
| `tools/scenario_dataset.py`, `tools/scenario_plugin.py` | insertion speed, cut-in staging | 6 |
| `tests/test_tracking.py`, `tests/test_perception_audit.py` | T1 and replay fixtures | 3 |

## 8. Offline Raspberry Pi demonstration (done)

`tools/pi_offline_demo.py` — **EXPERIMENTAL, failed predictive acceptance, NOT safety-authoritative.** It
builds a self-contained bundle (the frozen `model.json`, the two standard-library modules it needs, 22,868
recorded feature frames from the 40 held-out test/stress/ordinary runs, and Mac reference outputs) and
replays it batch-1. The bundle runs from its own directory on the Pi
(`~/roadsense-offline-demo-7a2-20261117`), with `nice -n 10`, `python3 -s`, no `PYTHONPATH`; it does not
import the installed RoadSense, and the `roadsense-edge` service, its units and its events-only history mode
were not touched.

| | Mac (arm64, Python 3.12) | Raspberry Pi 5 (Python 3.13, nice 10) |
|---|---|---|
| features + model p50 / p95 / p99 | 33 / 37 / 42 µs | 128 / 138 / 141 µs |
| model only p50 / p99 | 26 / 33 µs | 102 / 110 µs |
| model load (incl. imports) | 3.6 ms | 10.8 ms, +1.5 MiB RSS |
| peak RSS after load | 24 MiB | 21 MiB |
| CPU at the live 5 Hz rate | 0.02% of a core | 0.06% of a core |
| parity vs Mac: max \|Δscore\| / feature / threshold disagreements | 0 / 0 / 0 | 5.6e-17 / 0 / 0 over 22,868 frames |

Peak RSS at the end (≈195 MiB) is the replay holding every frame in memory, not a live requirement. These are
offline measurements of compute cost and numerical parity only; they say nothing about the model's
usefulness, which failed acceptance.

```
PYTHONPATH=src python3 tools/pi_offline_demo.py prepare CAMPAIGN BUNDLE_DIR
scp -r BUNDLE_DIR/. pi:~/roadsense-offline-demo-…/     # into a new, empty directory
ssh pi 'cd ~/roadsense-offline-demo-… && nice -n 10 python3 -s pi_offline_demo.py bench . ~/report.json'
```

Evidence: [mac-bench.json](evidence/phase7a3/mac-bench.json), [pi-bench.json](evidence/phase7a3/pi-bench.json),
[pi-demo-manifest.json](evidence/phase7a3/pi-demo-manifest.json).

## 9. Observation on the Pi (not acted on)

While preparing the demo, one thread of the `roadsense-edge` service (main PID 1041) was using 100% of a core
for the whole 45 min since boot, with no edge frames received and history idle (queue 0). The deployed
history files match their Phase 6B checksums, and an idle history recorder on the Mac uses no CPU, so the
spinning thread is not identified yet. The Pi benchmark ran with this load present (load average ≈1.0 of 4
cores). It needs a separate, read-only investigation before anything is changed.
