# Phase 7A.3 — Perception and track observability (investigation and proposal)

Status: investigation accepted (`1210f38`). **Stages 1–4 implemented and reported in §10**: raw-frame recording
and a 30-run baseline (P8), label and cut-in staging corrections (P7), opt-in range-rate track initiation (P1, not
active anywhere by default), and the Raspberry Pi CPU root cause. No model was trained or retrained; Phase 7B
(live integration) is not started; the deployed service and the safety authority are unchanged. P2–P5 wait for
review of the P1 results.

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

> **Correction (Stage 3):** the first version of the audit counted each missed frame twice in the denominator, so
> the rates below are m/(n+m) instead of m/n. Corrected: by speed 25.3 / 6.2 / 2.3 / 6.5 / 11.1 / **50.1%**; by
> range 2.0 / 1.1 / 2.9 / 2.6 / 15.7 / 28.9%; motorcycle 15.9%, car 3.5%; within 100 m the 25–30 m/s band is
> **40.2%** (612 frames). The original figures and file are kept as produced;
> [perception-audit-corrected.json](evidence/phase7a3/perception-audit-corrected.json) has every corrected rate.
> Slot outcomes, identity, kinematics, latencies and traces were not affected.

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
| T2 | missed_tracking, observable within 100 m, each speed band incl. 25–30 m/s | 2.3–33.4% (all ranges; corrected within 100 m: 1.1–40.2%) | ≤ 3% in every band |
| T3 | missed_tracking, observable within 100 m, each vehicle type | car ≥22 m/s 7.5% (≤80 m) | ≤ 3% every type |
| T4 | missed_tracking 100–150 m | 13.6% / 22.4% (corrected 15.7% / 28.9%) | ≤ 10% (report; radar-limited) |
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

## 10. Implementation results, Stages 1–4 (2026-11-17)

Commits: investigation `1210f38`; P8 recording and replay `85f3991`; audit correction and T-criteria `b89e25c`;
P7 labels and cut-in staging `e74384f`; P1 initiation `2be33c7`; this report and its evidence in the docs commit
that follows. Evidence: [evidence/phase7a3/stage/](evidence/phase7a3/stage/). Bulk data (recordings, replays, frame
traces) stays outside the repository under `~/Library/Application Support/RoadSense/`.

### 10.1 Stage 1: raw sensor recording (P8) and the baseline

`ROADSENSE_FRAME_CAPTURE=path` makes the ego controller append every frame it sends to the edge server, byte for
byte, to a file it never overwrites; it is off by default and changes nothing sent or computed.
`tools/phase7a3_recording.py` re-ran the 30 Phase 7A.2 test and stress configurations once, each in its own
Webots (batch, fast, no rendering, SUMO port 8901) with its own RoadSense server (port 8011); the open Webots
session, the development server and the Pi were not touched. One 40 s capture smoke run preceded it. These
configurations had been evaluated before: the recordings are **diagnostic regression data, not an untouched
test set**.

| Check (`tools/tracker_replay.py verify`) | Result |
|---|---|
| runs completed | 30 / 30, 581 s wall, 82 MB |
| frames, sequences, timestamps | 16,618; contiguous from 1 in every run; every step 200 ms |
| checksums | 90 / 90 files match (`stage/recording-SHA256SUMS`); each capture matches its campaign record |
| server | accepted every captured frame plus the end marker; 0 missed, 0 rejected |
| ground-truth join | 16,588 / 16,618 (each run's first frame, t = 0.2 s, precedes the first truth frame) |
| **deterministic replay** | the server's code path (`edge.EdgeRun`) reproduces **all 16,618 live slot-feature rows exactly**, twice |

Provenance: `run_one` snapshots the working tree per run, and the P1 edit was made while the campaign ran, so 10
runs record a dirty `tracking.py`/`pipeline.py` (P1 code present, default legacy). The exact replay against the
committed legacy tracker shows their live behaviour was the legacy tracker's.

Two radar facts measured from the captures: the range-rate sign is "positive = opening" (agrees with the change
of distance in 96.6% of matched returns); a target's first refresh often carries an implausible range rate
(above 30 m/s), which P1 guards against.

### 10.2 Stage 2: label corrections (P7) and cut-in staging v2

`scenario_dataset.correct_initialization` replaces a vehicle's first-frame physical speed only when it reads
still (< 0.5 m/s) while its displacement to the next frame and SUMO's logical speed both exceed 2 m/s and agree
within 3 m/s. Stationary vehicles, slow vehicles, every later frame and near misses not resting on the artifact
are untouched (tests in `tests/test_label_corrections.py`). On the Phase 7A.2 campaign the original labels are
reproduced exactly and kept beside the corrected ones (`tools/label_corrections.py`, `stage/label-corrections.json`):

| Split | fixes (staged / traffic) | labels 1→0 | labels 0→1 | incidents original → corrected |
|---|---|---|---|---|
| train | 65 / 936 | 76 | 0 | 41 → 27 |
| validation | 15 / 202 | 48 | 0 | 27 → 20 |
| test | 22 / 210 | 45 | 0 | 17 → 11 |
| stress | 12 / 86 | 0 | 0 | 21 → 19 |
| ordinary | 0 / 133 | 0 | 0 | 6 → 6 |

Traffic fixes are reused Webots nodes whose first frame is far from the ego; only staged insertions changed labels.
Post hoc, under corrected labels, the **frozen** model's test pre-onset recall is 6/11 (54.5%) instead of 6/14
(42.9%), early-frame recall 22.2% instead of 17.8%; stress (15/18) and ordinary (37.8 false alarms/h) are
unchanged. It still fails the 70% target; the 7A.2 result and artifacts are unchanged.

`tools/cutin_staging.py` draws a cut-in by how long the cutter is in view before it moves, the gap and speed
difference when it starts and the lane-change time (the supervisor's sinusoidal profile, 2.5 m/s cap):

| Kind | observe | gap at start | Δspeed | lane change | design warning (200 draws) | use |
|---|---|---|---|---|---|---|
| gradual | 3–6 s | 10–30 m | −15…+3 km/h | 3–5 s | 3.9–7.9 s | ordinary/test |
| assertive | 1.5–3 s | 5–18 m | −20…0 km/h | 2–3 s | 2.3–4.2 s | ordinary/test |
| sudden | 0.4–1 s | 2–10 m | −30…−8 km/h | 1–1.5 s | 0.9–1.7 s | **stress only** |

A bounded check (6 isolated 95–115 s runs, two per kind, captures verified exactly) staged every cut-in as drawn:
insertion → lane entry observed 5.8 / 6.0 s (gradual, design 5.5 / 5.9), 4.2 / 3.8 s (assertive, 3.6 / 3.5),
1.6 / 1.8 s (sudden, 1.3 / 1.4). Two of the six produced a true near miss (assertive 4.2 s and sudden 2.0 s after
insertion); the gradual ones did not. The v2 staging is ready for a future collection; none was collected.

### 10.3 Stage 3: P1 range-rate initiation

`Tracker(initiation="range_rate")` (default `"legacy"`, unchanged) predicts an unconfirmed track with a velocity
fused, in information form, from the range rate made a world radial speed with the ego's velocity (the
line-of-sight component only), the track's displacement once it has two detections, and a weak prior (σ 40 m/s
along the carriageway, 3 m/s across); its covariance sets a 99.9% Mahalanobis gate (capped at 12 m). Detections
inside the legacy gate are still accepted; radial speeds above 60 m/s (first-refresh artefacts) are ignored;
confirmed tracks are unchanged. Reversible: one constructor argument; nothing selects it outside tests and the
offline replay.

**T1 (synthetic radar with the world's noise, `tests/test_tracking_p1.py`, 61 cases): pass.** Every world
speed 0–40 m/s, ego 11 and 22 m/s, noiseless and noisy, lateral −2…+2 m/s, confirms within 0.6 s; an oncoming
vehicle closing at 52 m/s and one overtaking from behind at 36 m/s confirm; two vehicles side by side in
neighbouring lanes stay two tracks, each in its lane; a crossing pedestrian (1.5 m/s) confirms within 0.6 s and a
2.5 m/s lane change within 0.8 s; a wrong first range rate (0, 70 or −45 m/s) delays confirmation by at most one
cycle. The legacy tracker still confirms 24 m/s and never 26 m/s.

On the identical 16,618 recorded frames (`stage/audit-recording-*.json`, `stage/criteria-legacy-vs-p1.json`):

| Within 100 m, by world speed | 0–5 | 5–10 | 10–15 | 15–20 | 20–25 | 25–30 m/s |
|---|---|---|---|---|---|---|
| observable frames | 362 | 1,293 | 10,427 | 3,372 | 1,937 | **16** |
| legacy miss rate | 14.1% | 7.0% | 1.6% | 0.4% | 1.5% | **87.5%** |
| P1 miss rate | 14.1% | 7.0% | 1.7% | 0.4% | 0.7% | **12.5%** |

| Criterion (§5) | legacy | P1 | limit | P1 |
|---|---|---|---|---|
| T1 synthetic confirmation, every speed | fails above 25 m/s | ≤ 0.6 s | ≤ 0.6 s | **pass** |
| T2 worst speed band within 100 m | 87.5% | 14.1% (0–5 m/s; 25–30 m/s 12.5%) | ≤ 3% | fail |
| T3 worst vehicle type within 100 m | 3.56% (motorcycle) | 3.56% | ≤ 3% | fail |
| T4 worst band 100–150 m | 25.6% | 23.1% | ≤ 10% (report) | fail |
| T5 identity switches + fragmentations / 1,000 | 4.87 | 4.65 | ≤ 1.0 | fail |
| T6 wrong + spurious, worse adjacent side | 12.5% | 12.4% | ≤ 5% | fail |
| T7 v_lat MAE / p95 in lateral motion | 0.51 / 1.80 | 0.51 / 1.80 | ≤ 0.30 / 1.00 | fail |
| T8 false lateral, young / all tracks | 33.7% / 9.7% | 34.4% / 9.8% | ≤ 10% / 4% | fail |
| T9 lateral cue p50 / p90 | 0.6 / 2.4 s | 0.6 / 2.4 s | ≤ 0.4 / 0.8 s | fail |
| T10 lon / lat MAE | 1.574 / 0.309 m | 1.571 / 0.310 m | ≤ +10% | pass |
| T11 blind zone | not applicable (P5 not approved) | | | — |
| T12 regression | 600 tests pass; decision diff below; Vivaldi smoke **not run** (see below) | | | partial |
| T13 tracker update on the Pi 5, p99 | 45 µs | **62 µs** | ≤ 2 ms | **pass** |

P1 eliminates the 25 m/s boundary in the synthetic tests and removes most of it in the recordings (14 → 2
missed frames of 16, and 20–25 m/s halved), but the recordings hold only 16 observable frames above 25 m/s, so the
recorded evidence for that band is thin. P1 was never expected to move T5–T9: those are the velocity window,
association and slot problems of P2–P4. T2 still fails on slow and stopped vehicles (0–5 m/s, 14.1%, unchanged).

**Decision diff (T12, `stage/decision-diff.json`):** 8 of 16,618 decisions differ, all P1 removing a legacy
CAUTION or BRAKE. Two BRAKEs were false alarms on oncoming motorcycles whose fresh legacy tracks had lateral
speeds of 6.8 and 7.0 m/s (truth 1.3 and 0.0). Two CAUTIONs concerned a motorcycle moving away; the real threat
in that run (a truck) is flagged identically by both trackers. **Four CAUTIONs were a plausible warning lost**: a
car from behind drifting toward the ego lane at 1.0–1.2 m/s, which P1 confirmed earlier, so its full 1.5 s
finite-difference velocity lagged (0.2–0.5 m/s); no near miss followed. That is the unchanged velocity estimator
(P2's subject) interacting with earlier confirmation, and it is a reason not to activate P1 alone.

**Regression:** `pytest` 600 passed (including 61 P1, 5 label/staging and 5 audit tests). The Vivaldi smoke
(`tests/browser_smoke.py`, isolated profile, headless and `--headed`) could not run: Vivaldi 8.3 started and
listened for DevTools but its page never answered (`CVDisplayLinkCreateWithCGDisplay failed` in its log; the
display appears unavailable); no fallback browser was launched and no test browser was left running. It passed
earlier the same day; it should be re-run with the display awake before anything is activated.

### 10.4 Stage 4: Raspberry Pi CPU (read-only; `pi-cpu-investigation.json`)

| | |
|---|---|
| Process / thread | `roadsense-edge` main PID 1041; thread **TID 1167**, the second thread `server.lifespan` starts: the **history writer** (`HistoryRecorder._run`); TID 1166, started first, is the AI explainer (`futex_do_wait`) |
| CPU / scheduling | 100% of one core (501 user ticks, 0 system, over 5 s); state R, `wchan` 0; SCHED_OTHER, nice 0; 4,547 s user, 1.4 s system over 4,575 s uptime |
| Status | history `recording`, events only, 0 states observed since start; deployed `history.py` matches `eb4b1c4` |
| Source | `src/roadsense/web/history.py`, `HistoryRecorder._step`: the flush deadline `last_flush + HEARTBEAT_S` is always included, but a flush happens only when rows or dirty segments exist |

**Root cause:** with nothing to write (no frames since start, or 10 s after the last flush once a run ends) the
heartbeat deadline is never advanced, so `queue.get(timeout=max(0, due − now))` gets 0 forever and returns at once
without a system call: a pure user-space spin until the next state arrives. A permanent open failure
(`incompatible`) spins the same way on a maintenance deadline that is never reached. Not busy polling of a
socket, a retry loop or a timer: a deadline nothing can move.

Reproduced on the Mac with the deployed and current files: 0% while states arrive, 0% for the first 10 idle
seconds, then 84–99% of a core; 85% at once with an incompatible file. CPU while the Pi processes frames was not
measured on the Pi: sending frames to the service would write to its real history, which was not authorised; the
Mac measurement stands in. **Proposed fix** (`history-idle-spin.patch`, not applied or deployed): deadlines only
for work that can happen. On a patched copy: 0% idle, 0% after a run, 0% incompatible; `test_history.py` and
`test_web.py` 77 passed. Deploying it needs approval and a service restart.

### 10.5 Remaining failure cases

- Slow and stopped vehicles: 0–5 m/s miss rate 14.1% within 100 m (25.3% in 7A.2), unchanged by P1.
- Lateral velocity: MAE 0.51 m/s in lateral motion, false lateral motion on 34% of young tracks; cue p90 2.4 s;
  staged cutters' lateral cue lags the true onset 0–2.0 s and the lead slot lags lane entry 0.4–1.6 s.
- Identity: 4.7 switches/fragmentations per 1,000 matched frames; 12% wrong or spurious right-slot frames.
- Long range: 23–26% of observable frames missed at 125–150 m.
- Blind zone alongside: unchanged (P5 not approved).
- P1 removed four plausible CAUTION frames through the lagging velocity window (§10.3).
- Evidence above 25 m/s on real recordings is 16 frames; the T1 synthetic tests carry most of the P1 claim.
- The browser smoke and the Pi CPU while processing frames remain unmeasured in this stage.

### 10.6 Recommended next stage

1. Review P1 (keep it opt-in) and approve the history writer fix for the Pi (one restart).
2. **P2 next**: a constant-velocity Kalman filter per track replacing the 1.5 s finite difference, initialised
   from P1's estimate and covariance. It targets T7–T9 directly and removes the lag that cost the four CAUTIONs;
   P1 and P2 should be evaluated, and ever activated, together. Then P3 (global association with P2's gates) for
   T5/T6, and P4 (covariance-based coasting) for spurious slots.
3. A second bounded recording with more traffic above 25 m/s (ego 80 km/h, limit 100 km/h) to test T2's top band
   on more than 16 frames, using the v2 cut-in staging.
4. P6 offline only: track IDs used solely to reset temporal history on identity change, never as model inputs.
5. P5 only after its sensor specification, placement, coverage and cost review.
