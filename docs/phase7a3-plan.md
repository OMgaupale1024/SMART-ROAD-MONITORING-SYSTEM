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

Missed-tracking rate for observable vehicles, after the 0.6 s confirmation allowance (**corrected audit**,
[perception-audit-corrected.json](evidence/phase7a3/perception-audit-corrected.json); see the errata below):

| World speed | 0–5 | 5–10 | 10–15 | 15–20 | 20–25 | 25–30 m/s |
|---|---|---|---|---|---|---|
| all ranges | 25.3% | 6.2% | 2.3% | 6.5% | 11.1% | **50.1%** |
| within 100 m | 25.3% | 5.5% | 1.1% | 1.2% | 1.8% | **40.2%** (612 frames) |

| Range | <25 | 25–50 | 50–75 | 75–100 | 100–125 | 125–150 m |
|---|---|---|---|---|---|---|
| miss rate | 2.0% | 1.1% | 2.9% | 2.6% | 15.7% | 28.9% |

By type, all ranges: motorcycle 15.9%, car 3.5%, truck 2.4%, autorickshaw 1.3%, bus 1.0%; within 100 m: car
2.9%, truck 2.6%, motorcycle 2.0%, autorickshaw 1.4%, bus 0.9%. The motorcycle's high overall rate is mostly long
range (small radar cross-section near the range limit) plus speed. The 0–5 m/s band (25%) is vehicles that are
stopped or just inserted, where the zero-velocity prediction is right but confirmation competes with the staging
insertion.

> **Errata.** Two sets of figures first published here in `1210f38` were wrong; the tables above supersede them.
> The as-run file [perception-audit.json](evidence/phase7a3/perception-audit.json) is kept unchanged.
>
> 1. *Denominator* (fixed in `b89e25c`): every miss rate counted each missed frame twice, giving m/(n+m) instead
>    of m/n. As first published, now **erroneous**: by speed 20.2 / 5.9 / 2.3 / 6.1 / 10.0 / 33.4%; by range
>    2.0 / 1.1 / 2.8 / 2.5 / 13.6 / 22.4%; by type motorcycle 13.7%, car 3.4%, truck 2.3%, autorickshaw 1.2%,
>    bus 1.0%; the right slot 12.9% (corrected 14.8%).
> 2. *Mislabelled* (found in Stage 5): "within 80 m the motorcycle rate drops to 2.0% (slow) / 3.5% (≥22 m/s);
>    cars ≥22 m/s within 80 m: 7.5%" came from a separate script that applied no 80 m filter and divided by slot
>    frames whether or not the vehicle was yet observable. **Withdrawn**; the within-100 m figures above use the
>    audit's definition.
>
> Slot outcome counts, identity, kinematics, latencies and the frame traces were not affected.

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

| # | Metric | Baseline, 7A.2 campaign (corrected audit) | Accept |
|---|---|---|---|
| T1 | synthetic constant-velocity target, every world speed 0–40 m/s and lateral speed 0–2 m/s, noiseless and with the world's noise | never confirms > 25 m/s | confirms ≤ 0.6 s at every speed |
| T2 | missed_tracking, observable within 100 m, each speed band incl. 25–30 m/s | worst 40.2% (25–30 m/s); 0–5 m/s 25.3% | ≤ 3% in every band |
| T3 | missed_tracking, observable within 100 m, each vehicle type | worst 2.9% (car) | ≤ 3% every type |
| T4 | missed_tracking 100–150 m | 15.7% / 28.9% | ≤ 10% (report; radar-limited) |
| T5 | identity switches + 3 s fragmentations per 1,000 matched slot frames | 2.9 | ≤ 1.0 |
| T6 | wrong_vehicle + spurious, share of non-empty adjacent-slot frames | 8.5% left, 18.1% right | ≤ 5% |
| T7 | v_lat MAE / p95 during lateral motion | 0.57 / 2.08 m/s | ≤ 0.30 / ≤ 1.00 m/s |
| T8 | false lateral motion (\|v_lat err\| > 0.3 m/s) on steady vehicles, tracks < 1.5 s / all | 29.7% / 9.1% | ≤ 10% / ≤ 4% |
| T9 | lateral cue latency p50 / p90 | 0.6 / 2.0 s | ≤ 0.4 / ≤ 0.8 s |
| T10 | lon / lat MAE on correct slots | 1.57 (bias) / 0.31 m | no worse than baseline +10% |
| T11 | blind zone: frames with a vehicle alongside (\|lon\| < 5 m, adjacent lane) represented in the slot | 0 (radar-only) | ≥ 90% (only if P5 approved) |
| T12 | rule-based safety: full test suite, `tests/browser_smoke.py` in Vivaldi, and a decision diff of the recorded replays reviewed by hand | — | all pass, every changed decision explained |
| T13 | tracker update cost on the Pi 5 at 5 Hz (measured on the Pi, not the Mac) | not measured | p99 ≤ 2 ms per frame |

The limits are unchanged since `1210f38`. The baseline column is `criteria()` of `tools/perception_audit.py` on
the corrected audit (its `criteria` block). Baselines first published in this table that were wrong: T2
"2.3–33.4%" and T4 "13.6% / 22.4%" (denominator, §3.1 errata 1), T3 "car ≥22 m/s 7.5% (≤80 m)" (withdrawn,
errata 2) and T8 "28.9% / 8.4%", whose 8.4% was tracks older than 3 s, not all tracks.

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

*Resolved: root cause in §10.4 (the history writer), fix deployed in Stage 6.*

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

Provenance (`stage/recording-provenance.json`, every run at revision `85f3991`): `run_one` snapshots the working
tree per run, and work continued in the tree while the campaign ran. **13 runs are clean. 10 runs captured
uncommitted source changes**: `tracking.py` and `pipeline.py` with the P1 code (default still legacy) and
`tools/scenario_dataset.py`. 7 more captured an uncommitted `tools/scenario_dataset.py` only, an analysis module
that no participating component (batch runner, scenario plugin, controller, server) imports. The exact replay
against the committed legacy tracker shows the live outputs of all 30 equal the legacy tracker's; that parity
does not remove the caveat that 10 runs did not execute a committed revision.

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

| Criterion (§5) | legacy, recordings | P1, recordings | limit | P1 status |
|---|---|---|---|---|
| T1 synthetic confirmation, every speed | fails above 25 m/s | ≤ 0.6 s | ≤ 0.6 s | **pass** |
| T2 worst speed band within 100 m | 87.5% | 14.1% (0–5 m/s; 25–30 m/s 12.5%) | ≤ 3% | **fail** |
| T3 worst vehicle type within 100 m | 3.56% (motorcycle) | 3.56% | ≤ 3% | **fail** |
| T4 worst band 100–150 m | 25.6% | 23.1% | ≤ 10% (report) | **fail** |
| T5 identity switches + fragmentations / 1,000 | 4.87 | 4.65 | ≤ 1.0 | **fail** |
| T6 wrong + spurious, worse adjacent side | 12.5% | 12.4% | ≤ 5% | **fail** |
| T7 v_lat MAE / p95 in lateral motion | 0.51 / 1.80 | 0.51 / 1.80 | ≤ 0.30 / 1.00 | **fail** |
| T8 false lateral, young / all tracks | 33.7% / 9.7% | 34.4% / 9.8% | ≤ 10% / 4% | **fail** |
| T9 lateral cue p50 / p90 | 0.6 / 2.4 s | 0.6 / 2.4 s | ≤ 0.4 / 0.8 s | **fail** |
| T10 lon / lat MAE | 1.574 / 0.309 m | 1.571 / 0.310 m | ≤ +10% | **pass** |
| T11 blind zone | not measured (P5 not approved) | not measured | ≥ 90% | **unverified** |
| T12 regression | 600 tests pass; decision diff below; Vivaldi smoke **not run** (see below) | | all pass | **unverified** |
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

## 11. Stages 5–10 (2026-11-17)

Commits: Stage 5 published `1210f38`…`4cf9cb3`; history fix `769e10d` and its deployment record `1e49916`; P2
`33bcf12`; Stage 8 tools `149d04e`; the frozen Stage 9 campaign `077b2ee`; P1's T1 cases through P2, the Stage 9
coverage command, and this report with its evidence in the commits that follow. Evidence:
[evidence/phase7a3/stage8/](evidence/phase7a3/stage8/), [stage9/](evidence/phase7a3/stage9/),
[stage10/](evidence/phase7a3/stage10/), [stage6/](evidence/phase7a3/stage6/). P1 and P2 stay opt-in: nothing outside
tests and the offline tools selects them, and the Pi runs neither.

### 11.1 Stage 5: publication

The seven Stage 1–4 commits were reviewed and pushed (fast-forward, `29443b6..4cf9cb3`). Nothing of Phase 7A.2 was
rewritten: its datasets, artifacts, failed acceptance and original audit outputs are as published. §3.1 leads with
the corrected audit and keeps both errata; §5's baseline column is the corrected audit's `criteria()`, with the
wrong first-published baselines listed; §10.1 keeps the provenance caveat (10 runs recorded uncommitted source);
§10.3's matrix gives pass, fail or unverified for every criterion. No recordings, credentials or private paths
were committed (one absolute home path in an unpushed evidence file was found and replaced with `~/` first).

### 11.2 Stage 6: Raspberry Pi history writer

`769e10d`: the writer waits until its next real deadline (a flush only when rows or dirty segments exist, the
heartbeat only when something was written, retries only while the database is closed), so it never computes a
zero timeout. Regression tests cover idle operation, empty flush deadlines, an unavailable and an incompatible
database, a paused writer, new records after idling and clean shutdown; they fail on the old code (462,213 writer
steps in 0.6 s) and pass on the new. Deployed alone (only `history.py` changed, checked by 182 checksums) after a
tar backup and an SQLite online-API backup with integrity check; one restart. CPU of the service process:
**99.99% of a core before, 0.04% after** (120 s idle, several flush intervals); integrity ok, counts preserved,
previous processor closed `clean`, every endpoint as before. An isolated instance (own port and database) handled
300 recorded frames at 0.87% and idled at 0.05%. Details: [history.md](history.md) and `evidence/phase7a3/stage6/`.

### 11.3 Stage 7: P2, a Kalman filter per track (opt-in)

`Tracker(initiation="range_rate", estimator="kalman")`; `estimator="kalman"` without P1's initiation is refused.
Frames, as in `tracking.py`: tracks live in the world frame (Webots x, y); the ego frame has its origin at the rear
axle, x forward, y left; each radar's line of sight **u** runs from its mount (placed with the ego pose) to the
detection, **n** is **u** turned +90°; the road axes are e_a = (cos h, sin h), e_c = (−sin h, cos h) with h the
carriageway heading (0 or π here, so v_x is the speed along the road, v_y across it).

- **State** s = (x, y, v_x, v_y), covariance P (4×4), stored at the time of its last detection.
- **Prediction** over the actual interval dt = t − t_last ≥ 0 (never backwards): x += v_x dt, y += v_y dt;
  P ← F P Fᵀ + Q, Q = [[A dt³/3, A dt²/2], [A dt²/2, A dt]], A = q_a e_a e_aᵀ + q_c e_c e_cᵀ, q = σ² × 0.2 s with
  σ_a = 3 m/s² along (braking, accelerating; an 8 m/s² stop still passes the 3 m gate) and σ_c = 1 m/s² across
  (a 3.75 m sinusoidal lane change peaks at 3.75π²/(2T²): 2.1 m/s² over T = 3 s, 0.74 over 5 s).
- **Updates**, scalar and sequential, Joseph form, symmetrised: position along **u** (σ 0.30 m), position along
  **n** (σ 0.20 m), and the world radial speed z = range rate + **u**·v_ego with h = (0, 0, u_x, u_y), σ 0.30 m/s:
  the line-of-sight component only, never a 2-D velocity; skipped when the innovation exceeds 3σ (χ²₁ 9) or
  |z| > 60 m/s (Webots first-refresh artefacts).
- **Birth**: position at the detection with the measurement covariance turned into world axes; velocity and its
  covariance from P1's information-form fusion of the road prior (σ 40 m/s along, 3 m/s across) and the radial
  speed (σ 0.30 m/s); no position–velocity correlation at birth.
- **Lifecycle, unchanged except where the filter replaces the window**: an unconfirmed track is gated with P1's
  χ² 13.8 (≤ 12 m) on the filter's predicted covariance plus 0.09 m², or the legacy 5 m around its last
  detection; a confirmed one with the legacy 3 m around the filter's prediction; greedy assignment; confirmation
  after 0.4 s of detections; the reported velocity is the filter's, only once confirmed; a cycle without a
  detection only predicts; expiry by the legacy rule (seen span, at most 3 s). A non-finite state or a
  non-positive variance restarts the filter at the detection. Pure Python, Python 3.9 compatible, no dependency.

**Noise, measured** (`tools/radar_noise.py` on the Stage 1 recordings; truth pairs detections with vehicles,
offline only; `stage8/radar-noise.json`):

| | 0–25 m | 25–50 m | 50–100 m | 100–150 m |
|---|---|---|---|---|
| along the line of sight, cycle to cycle (σ) | 0.26 m | 0.25 m | 0.25 m | 0.25 m |
| across it, cycle to cycle (σ) | 0.17 m | 0.08 m | 0.13 m | 0.17 m |
| along, offset from the true centre: median / robust σ | +0.17 / 1.63 m | +0.93 / 1.12 m | +0.78 / 1.66 m | −1.25 / 0.73 m |
| across, offset: median / robust σ | +0.97 / 0.82 m | +0.43 / 0.34 m | +0.22 / 0.22 m | +0.22 / 0.20 m |

World radial speed: robust σ 0.17 m/s, p95 0.55 m/s; 0.08% beyond 3 m/s, but 1.4% on a target's first refresh.
Adding the yaw-rate lever arm of the radar mount changes nothing measurable (0.170 vs 0.171 m/s), so the radial
model leaves it out; a radar-to-pose time offset fitted on steady targets while the ego turns is 0.011 s
(negligible). The filter uses the cycle-to-cycle noise (with margin); the large, slowly varying offsets are where
on the body the radar places its return, which changes with aspect: not white noise, and not removable by a
filter of the vehicle's centre (see T7 below).

**Lateral response versus false lateral motion** (`stage8/p2-across-noise-sensitivity.json`; σ_c was fixed from
the lane-change kinematics above before this sweep and not changed after it):

| σ_c (m/s²) | T7 MAE / p95 | T8 young / all | T9 p50 / p90 | T5 |
|---|---|---|---|---|
| 0.25 | 0.507 / 1.79 | 27.8% / 8.7% | 0.8 / 2.4 s | 3.67 |
| 0.5 | 0.501 / 1.79 | 27.6% / 8.7% | 0.8 / 2.2 s | 4.32 |
| **1.0** | 0.509 / 1.80 | 28.4% / 8.5% | 0.6 / 2.4 s | 4.09 |
| 2.0 | 0.502 / 1.79 | 29.3% / 10.1% | 0.4 / 2.4 s | 4.23 |

More lateral process noise gives an earlier lateral cue (p50 0.8 → 0.4 s) for more false lateral motion (all
tracks 8.5 → 10.1%), and never moves T7. **Numerical health** on all 16,618 frames: the radial gate rejected
0.24% of radial speeds (164 of 68,116), at most 4 in a row on one track, no numerical restart, position
variance ≤ 0.13 m². Tests (`tests/test_tracking_p2.py`, 59): every T1 case of P1 through the filter (every speed
0–40 m/s, lateral ±2 m/s, oncoming at 52 m/s, overtaking, neighbouring lanes, crossing, wrong first range rates),
a 1 m/s lane change followed within 0.3 m/s, irregular timestamps with a gap and a repeated time, and the default
staying on the window estimator.

### 11.4 Stage 8: legacy (A), P1 (B) and P1+P2 (C) on the 16,618 Stage 1 frames

Deterministic replays of the identical frames (`tools/tracker_replay.py`; P1+P2 replays reproduce exactly). These
recordings are diagnostic regression data (§10.1), so this is not untouched evidence: §11.5 is.

| Within 100 m, by true speed | 0–5 | 5–10 | 10–15 | 15–20 | 20–25 | 25–30 m/s |
|---|---|---|---|---|---|---|
| observable frames | 362 | 1,293 | 10,427 | 3,372 | 1,937 | 16 |
| A missed | 14.1% | 7.0% | 1.6% | 0.4% | 1.5% | 87.5% |
| B missed | 14.1% | 7.0% | 1.7% | 0.4% | 0.7% | 12.5% |
| C missed | 15.2% | 7.0% | 1.7% | 0.4% | 0.7% | 18.8% (3) |

| Within 100 m, by type (A / B / C) | autorickshaw | bus | car | motorcycle | truck |
|---|---|---|---|---|---|
| missed | 2.5 / 2.5 / 2.6% | 1.4 / 1.4 / 1.6% | 1.1 / 0.5 / 0.5% | 3.6 / 3.6 / 3.5% | 0.4 / 0.5 / 0.4% |

| By range (A / B / C) | 0–25 m | 25–50 m | 50–75 m | 75–100 m | 100–125 m | 125–150 m |
|---|---|---|---|---|---|---|
| missed | 3.4 / 3.4 / 3.5% | 0.6 / 0.7 / 0.7% | 1.7 / 1.7 / 1.6% | 2.4 / 1.6 / 1.6% | 13.8 / 11.7 / 11.9% | 25.6 / 23.1 / 23.4% |

**Track-level confirmation** (`stage8/track-confirmation.json`): each episode in which the radars detect a vehicle
for at least 1 s, by its true speed, time to the first confirmed track on it.

| True speed | episodes (runs) | A ≤ 0.6 s | B ≤ 0.6 s | C ≤ 0.6 s |
|---|---|---|---|---|
| 15–20 m/s | 392 (21) | 98.2% | 100% | 100% |
| 20–25 m/s | 413 (22) | 65.4% (10 never) | 98.1% | 98.1% |
| 25–30 m/s | 94 (11) | **4.3% (55 never)** | **100%** | **100%** |

Most of the 94 high-speed episodes are at 120–150 m, which is why the frame-level band within 100 m has 16 frames.

| Identity, slots, kinematics (correct slots) | A | B | C |
|---|---|---|---|
| identity switches / fragmentations within 3 s | 40 / 64 | 39 / 61 | 37 / 51 |
| per 1,000 matched slot frames (T5) | 4.87 | 4.65 | 4.09 |
| left slot: wrong vehicle / spurious frames | 499 / 257 | 497 / 300 | 572 / 298 |
| right slot: wrong vehicle / spurious frames | 197 / 149 | 195 / 158 | 194 / 173 |
| lead slot: mislocalized frames | 57 | 44 | 19 |
| lon error MAE (mean) | 1.574 (−1.57) m | 1.571 m | 1.561 m |
| lat error MAE / p95 | 0.309 / 0.96 m | 0.310 / 0.96 m | 0.313 / 1.00 m |
| v_lon error MAE / p95 | 0.307 / 0.87 m/s | 0.309 / 0.90 m/s | **0.186 / 0.61 m/s** |
| v_lat error in lateral motion MAE / p95 | 0.508 / 1.80 | 0.508 / 1.80 | 0.509 / 1.80 |
| v_lat error, steady vehicles MAE / p95 | 0.141 / 0.56 | 0.146 / 0.57 | 0.130 / 0.48 |
| v_lat error, steady and track < 1.5 s MAE / p95 | 0.408 / 1.53 | 0.501 / 2.31 | **0.328 / 0.93** |
| false lateral motion, young / all (T8) | 33.7% / 9.7% | 34.4% / 9.8% | 28.4% / 8.5% |
| lateral cue latency p50 / p90 / max (T9) | 0.6 / 2.4 / 6.4 s | 0.6 / 2.4 / 6.4 s | 0.6 / 2.4 / 7.0 s |

**Why lateral velocity in lateral motion does not improve.** The detections themselves bound it: a centred 0.8 s
difference of a vehicle's own detections (non-causal, so better than any real-time estimator could do from
positions at that smoothing) has MAE 0.48 m/s (p95 1.74) against the true lateral speed while vehicles move
across, and exceeds 0.3 m/s on 13.1% of steady vehicles. The radar's return point moves over the body as the
aspect changes; in one cut-in it lagged the true centre by up to 0.9 m, then jumped 1.8 m in one cycle. P2's own
errors in lateral motion: MAE 0.31 m/s when only the ego moves across, 1.00 when the other vehicle does, 1.34 when
both. A filter of the vehicle's centre cannot remove a measurement point that wanders over the body; an extended-
object (body-point or yaw) model would be a different design, not a noise setting.

**Safety decisions** (`stage8/decision-*.json`). A, B and C drive the unchanged deterministic rules. Changes are
judged against an **oracle**: the same rules on the true position and velocity of every vehicle the radars
detected, kept as long as a tracker may coast it (evaluation only; `TruthOracle`, never a tracker). A first
oracle version that dropped vehicles 1 s after their last detection was superseded: it discarded a motorcycle about
1 m ahead of the ego's front, inside the radar's 1 m minimum range and so no longer detected, and so called a real
near miss safe. Over all 16,618 frames:

| | A | B | C | oracle |
|---|---|---|---|---|
| frames rated as the oracle | 16,126 | 16,126 | **16,173** | — |
| warning missing (oracle warns, tracker SAFE) | 208 | 212 | **143** | — |
| oracle CRITICAL not rated CRITICAL | 86 | 86 | **39** | of 272 |
| warning the oracle does not give | 186 | 182 | 184 | — |
| CRITICAL the oracle does not give (of which oracle SAFE) | 70 (39) | 70 (39) | **90 (52)** | — |

Legacy → P1+P2 changes **348 decisions** (2.1%) in 161 episodes over 25 of 30 runs (P1 alone: 8): 197 move to the
oracle's decision, 148 away from it, 3 neither. Of 213 raised frames the oracle supports 90; of 133 lowered ones it
supports 107. A forward near miss followed within 3 s in 70 raised and 23 lowered frames. Most changes are
threshold crossings (a conflict on the predicted trajectory is CAUTION, within 3 s HIGH, within 1.5 s CRITICAL)
from a different closing speed. C's v_lon is 40% more accurate, so in braking it closes in time: in
sudden_brake-1112 a motorcycle braking ahead from 15 to 4 m/s closed at −4.2, −7.5, −9.0 and −8.7 m/s (every other
frame), C read −3.1, −6.4, −6.8 and −8.8, the window (B) −0.8, −2.4, −2.7 and −5.0. The motorcycle then held station
about 1 m ahead, inside the radar's minimum range, and both tracks coasted: B's drifted ahead to 13 m and B rated
SAFE; C's kept a small closing speed, drew 2.7 m too close and rated CRITICAL. All 13 of those frames are labelled
near misses and the oracle rates them CRITICAL. C's unsupported CRITICAL frames form 24 episodes: 10 at TTC
1.27–1.5 s, just inside the CRITICAL line; 12 envelope overlaps of vehicles beside or just ahead, from lateral
position or speed error; 8 frames of one coasting track (cut_in-1124: a vehicle finished its lane change unseen and
the coast kept −1.47 m/s across for 2 s into a predicted conflict, where B rated SAFE); and 4 frames of a false
4.6 m/s lateral speed on a vehicle two lanes away (sudden_brake-1125, true 0). Constant-velocity coasting and
association are P4's and P3's subjects.

**The four cautions P1 lost** (`stage8/lost-cautions.json`, lht-sudden_brake-1107, a car closing from behind in the
right lane while drifting toward the ego lane at 1.0–1.4 m/s):

| Sequence | 229 | 230–232 | 233–234 | 235–236 | 237–238 | 239–240 |
|---|---|---|---|---|---|---|
| A legacy | SAFE | SAFE | CAUTION | CAUTION | CAUTION | HIGH |
| B P1 | SAFE | SAFE | SAFE | SAFE | CAUTION | HIGH |
| C P1+P2 | SAFE | SAFE | SAFE | **CAUTION** | CAUTION | CAUTION |
| oracle | SAFE | CAUTION | CAUTION | CAUTION | CAUTION | CAUTION |

P1+P2 restores **two of the four** (235–236), 0.4 s after legacy and 1.0 s after the oracle; its lateral speed
there (0.62 and 0.76 m/s against the true 1.01 and 0.95) still lags because the detections do (above), and matches
the truth from 237. At 239–240 it stays at CAUTION as the oracle does, where A and B escalate to HIGH. All three
trackers are late on this vehicle, which moved across fastest (1.2–1.4 m/s) before any of them warned.

**Raspberry Pi 5** (Python 3.13.5, 5,458 recorded frames, run in a new directory, service untouched:
`stage8/tracker-bench-pi.json`):

| Tracker update | mean | p50 | p99 | max | tracker memory peak |
|---|---|---|---|---|---|
| A legacy | 13.0 µs | 8.8 µs | 50.5 µs | 70.6 µs | 6.3 KiB |
| B P1 | 17.1 µs | 12.1 µs | 62.2 µs | 96.9 µs | 8.2 KiB |
| C P1+P2 | 116.3 µs | 79.8 µs | **387.2 µs** | 487.8 µs | 20.7 KiB |

C costs about 7× B in pure-Python 4×4 updates, still under a fifth of T13's 2 ms and 0.2% of the 200 ms cycle;
the same bundle runs under Python 3.9 (the Webots controllers' version).

### 11.5 Stage 9: the frozen high-speed recording campaign

Frozen in `077b2ee` before any run was recorded ([protocol](evidence/phase7a3/stage9/protocol.md), `plan.json`
sha256 `1c58ea74…`): 40 configurations, each recorded once from a clean worktree of that commit, in isolated batch
Webots (SUMO 8901, server 8011). Edits made meanwhile in the main checkout (this report, tests) never reached the
worktree; each run records revision `077b2ee`, `source_dirty: false` and the checksum of every source file. The open
Webots session and the development server kept their processes throughout. **All 40 attempts succeeded**: none
failed, none was retried, no stopping condition was reached. 6 diagnostic, 30 evaluation and 4 stress runs, 25,833
frames; every subset's `SHA256SUMS` verifies and every frame replays exactly (`verify`: 0 mismatches). Bulk data
(147 MB) stays in `~/Library/Application Support/RoadSense/campaigns/phase7a3-s9-*-20261117/`; the repository holds
each subset's `frozen.json`, `SHA256SUMS`, run records and analysis outputs. The evaluation ran once, as the
protocol states ([evaluate.sh](evidence/phase7a3/stage9/evaluate.sh)); A, B and C are as committed, nothing tuned.
The diagnostic subset was looked at while the evaluation runs recorded; nothing was changed as a result.

**Coverage: the pre-registered adequacy is not met.** Observable frames as the audit counts them (T2's
denominator), with the distinct vehicles and runs behind them
(`tools/phase7a3_highspeed.py coverage`, cross-checked against the audit):

| Evaluation subset, within 100 m | 0–5 | 5–10 | 10–15 | 15–20 | 20–25 | 25–30 | 30–35 | 35–40 m/s |
|---|---|---|---|---|---|---|---|---|
| observable frames | 237 | 210 | 5,821 | 1,672 | 3,087 | 877 | 487 | 3 |
| vehicles / runs | 8 / 4 | 10 / 6 | 94 / 27 | 36 / 18 | 50 / 20 | 52 / 20 | 26 / 9 | 1 / 1 |

Above 25 m/s: **1,367 frames from 62 vehicles in 20 runs** against 2,000 / 100 / 10; above 30 m/s: 490 frames
against 200. Two of the four conditions fail, so the coverage is reported as insufficient and the campaign is not
extended. It is still 85 times the 16 frames of the Stage 1 recordings. My pre-recording estimate was wrong about
the traffic, not the protocol about the rule: it assumed same-direction traffic would approach from behind, but
`tools/scenarios.py routes()` departs every vehicle at t = 0 and none behind the ego's start on its carriageway, so a
constant-speed ego closes only on slower vehicles while faster ones ahead draw away, and the many fast oncoming
vehicles never occupy a lead, follower, left or right slot. Of the 62, 35 came from the 4 stationary-lead runs
(883 frames), 18 from 7 of the 14 expressway runs (255 frames) and 9 from cut-ins (229 frames). Fast oncoming
vehicles do count at track level, below.

**Evaluation subset (untouched), A legacy, B P1, C P1+P2.** Missed within 100 m:

| By true speed | 0–5 | 5–10 | 10–15 | 15–20 | 20–25 | 25–30 | 30–35 m/s |
|---|---|---|---|---|---|---|---|
| A | 33.3% | 13.8% | 1.9% | 2.1% | 0.8% | **45.3%** | **45.6%** |
| B | 32.5% | 13.8% | 1.9% | 2.1% | 0.4% | **2.1%** | **0.4%** |
| C | 37.5% | 14.3% | 2.0% | 2.3% | 0.5% | **1.7%** | **0.6%** |

| By type (A / B / C) | autorickshaw | bus | car | motorcycle | truck |
|---|---|---|---|---|---|
| missed | 1.0 / 1.0 / 1.1% | 1.3 / 1.3 / 1.2% | 18.5 / 4.0 / 4.5% | 14.5 / 1.7 / 1.5% | 5.1 / 5.1 / 5.4% |

| By range (A / B / C) | 0–25 m | 25–50 m | 50–75 m | 75–100 m | 100–125 m | 125–150 m |
|---|---|---|---|---|---|---|
| missed | 8.2 / 4.6 / 5.0% | 4.7 / 1.7 / 1.8% | 8.9 / 1.2 / 1.4% | 8.4 / 1.1 / 1.1% | 18.4 / 7.2 / 6.9% | 36.2 / 23.4 / 23.4% |

| Track-level confirmation, episodes ≥ 1 s | episodes (runs) | A ≤ 0.6 s | B ≤ 0.6 s | C ≤ 0.6 s |
|---|---|---|---|---|
| 20–25 m/s | 161 (26) | 70.2% (3 never) | 96.3% | 96.3% |
| 25–30 m/s | 625 (29) | **6.2% (486 never)** | **97.4%** | **97.3%** |
| ≥ 30 m/s | 577 (30) | **2.1% (528 never)** | **97.2%** (1 never) | **97.2%** (1 never) |

On recorded traffic B and C confirm about 97% of high-speed episodes within T1's 0.6 s, not all; the rest within
3.0 s. The high-speed boundary is gone in the recorded data as in the synthetic tests.

| Identity, slots, kinematics (correct slots) | A | B | C |
|---|---|---|---|
| identity switches / fragmentations within 3 s | 18 / 28 | 20 / 45 | 18 / 37 |
| per 1,000 matched slot frames (T5) | 3.08 | 4.06 | 3.42 |
| left slot: wrong vehicle / spurious frames | 259 / 127 | 195 / 159 | 248 / 157 |
| right slot: wrong vehicle / spurious frames | 374 / 74 | 200 / 147 | 210 / 145 |
| lead slot: spurious / mislocalized frames | 116 / 11 | 285 / 7 | 269 / 5 |
| lon error MAE (mean) | 1.711 (−1.71) m | 1.683 m | 1.682 m |
| lat error MAE / p95 | 0.304 / 0.95 m | 0.314 / 0.98 m | 0.316 / 1.02 m |
| v_lon error MAE / p95 | 0.411 / 1.36 m/s | 0.372 / 1.39 m/s | **0.192 / 0.69 m/s** |
| v_lat error in lateral motion MAE / p95 | 0.578 / 1.42 | 0.500 / 1.32 | 0.474 / 1.26 |
| v_lat error, steady vehicles MAE / p95 | 0.150 / 0.63 | 0.152 / 0.67 | 0.138 / 0.56 |
| v_lat error, steady and track < 1.5 s MAE / p95 | 0.593 / 2.22 | 0.444 / 2.11 | **0.366 / 1.82** |
| false lateral motion, young / all (T8) | 25.1% / 11.1% | 26.1% / 11.7% | 23.0% / 11.0% |
| lateral cue latency p50 / p90 / max (T9) | 0.8 / 2.6 / 9.4 s | 0.8 / 2.0 / 4.8 s | 0.6 / 2.0 / 4.8 s |

For B and C, T2 now fails on stopped and slow vehicles, not fast ones. All 89 of C's 0–5 m/s misses are in the
stationary-lead runs, 72 of them while the ego itself has stopped: with both stopped, no radar detection lay within
3 m of the stopped vehicle ahead in 48 of 88 frames, and a track not refreshed for 3 s expires by the legacy rule. That is the sensor and
coasting (P4), not P2. B and C confirm fast vehicles A never confirmed, and the lead slot's spurious frames rose
from 116 to 285 / 269; their cause is not analysed in this stage.

| Decisions, 19,994 frames (oracle: 559 warnings, 90 CRITICAL) | A | B | C |
|---|---|---|---|
| frames rated as the oracle | 19,538 | 19,606 | **19,703** |
| warning the oracle does not give | 331 | 272 | **176** |
| warning missing (oracle warns, tracker SAFE) | 86 | **76** | 88 |
| oracle CRITICAL not rated CRITICAL | 36 | **29** | 32 |
| CRITICAL the oracle does not give (of which oracle SAFE) | 71 (57) | 72 (57) | **91 (77)** |

One stationary-lead run collided (sudden_brake-101); its 161 later frames are left out of the change counts.
Before any collision, legacy → P1 changes 96 decisions in 15 runs (82 toward the oracle), legacy → P1+P2 185 in 24
runs (134 toward, 51 away), P1 → P1+P2 95 in 17 runs (55 toward, 40 away). P1+P2's 22 CRITICAL frames beyond P1's
that the oracle does not support (12 episodes, 8 runs) are all lateral, at the edge of the conflict envelope:
vehicles about 2 m to the side whose estimated lateral position, or a lateral speed of up to 1.3 m/s where the true
one is about zero, puts them inside the envelope (7 episodes), or vehicles cutting in whose lateral speed is over- or
under-estimated by 0.5–1.9 m/s (5): the lateral measurement floor behind T7 again. On untouched data P1+P2 removes 47% of legacy's unsupported warnings (35% of P1's)
and agrees with the oracle most often, but adds unsupported CRITICAL frames and misses slightly more warnings than
P1.

**Stress subset** (4 sudden cut-ins; separate, never pooled): 2,004 frames. A, B and C fail T2–T9; three of the
four runs collide and all but 2 of the changed decisions come after the collision. Track level above 25 m/s: A
confirms 3% (25–30 m/s) and 0% (≥ 30 m/s) within 0.6 s, B and C 100% and 97.6%. **Diagnostic subset** (tuning data,
no statement rests on it): 3,835 frames, 178 observable above 25 m/s from 8 vehicles in one run.

### 11.6 Stage 10: regression and Vivaldi

At the commit that adds this report: **667 Python tests pass** (`pytest`), **65 of 65 JS** (`node --test tests/js/`,
Node 26.9.0), **60 Arduino-archive** tests. Log: `stage10-suites.log` in the analysis directory.

**The Vivaldi smoke test did not run; browser validation is not claimed.** `tests/browser_smoke.py` was run headless
and `--headed` with the display awake (`pmset`: user active, display-on assertion), a fresh temporary profile and the
mandated flags. Both stopped before any RoadSense page was opened: Vivaldi listed its `about:blank` tab, but the
first command to it (`Runtime.enable`) got no reply in 8 s. A probe without any RoadSense server, page or code
([stage10/cdp_probe.py](evidence/phase7a3/stage10/cdp_probe.py), same profile rules and flags) separates the two
levels: Vivaldi 8.3.4191.44 (Chromium 156) answers browser-level DevTools commands (`Browser.getVersion`,
`Target.getTargets`) but no page-level command, not even `Runtime.evaluate("1+1")` on `about:blank`, through the
page socket, an attached session or a newly created tab, headless or headed. Phase 6A recorded the same failure on
baseline code. **Classification: environmental (Vivaldi's page-level DevTools on this Mac), not an application
regression.** The server in those runs started and answered `/api/system`; the dashboard was never rendered. No
other browser was launched; the test's browsers and profiles were removed (none left running).

### 11.7 T1–T13 for A (legacy), B (P1) and C (P1+P2)

Values on the untouched Stage 9 evaluation subset, the Stage 1 recordings (diagnostic) in brackets. No limit was
changed.

| # | Limit | A legacy | B P1 | C P1+P2 | A / B / C |
|---|---|---|---|---|---|
| T1 | ≤ 0.6 s at 0–40 m/s, lateral 0–2 m/s (synthetic) | never confirms > 25 m/s | all cases | all cases (59 tests) | fail / **pass** / **pass** |
| T2 | ≤ 3% in every speed band | 45.6% (30–35) [87.5%] | 32.5% (0–5) [14.1%] | 37.5% (0–5) [18.8%] | fail / fail / fail |
| T3 | ≤ 3% every type | 18.5% car [3.6%] | 5.1% truck [3.6%] | 5.4% truck [3.5%] | fail / fail / fail |
| T4 | ≤ 10% at 100–150 m | 36.2% [25.6%] | 23.4% [23.1%] | 23.4% [23.4%] | fail / fail / fail |
| T5 | ≤ 1.0 per 1,000 | 3.08 [4.87] | 4.06 [4.65] | 3.42 [4.09] | fail / fail / fail |
| T6 | ≤ 5%, worse adjacent side | 22.0% [12.5%] | 13.9% [12.4%] | 14.2% [12.8%] | fail / fail / fail |
| T7 | ≤ 0.30 / 1.00 m/s | 0.58 / 1.42 [0.51 / 1.80] | 0.50 / 1.32 [0.51 / 1.80] | 0.47 / 1.26 [0.51 / 1.80] | fail / fail / fail |
| T8 | ≤ 10% / 4% | 25.1 / 11.1% [33.7 / 9.7%] | 26.1 / 11.7% [34.4 / 9.8%] | 23.0 / 11.0% [28.4 / 8.5%] | fail / fail / fail |
| T9 | ≤ 0.4 / 0.8 s | 0.8 / 2.6 [0.6 / 2.4] | 0.8 / 2.0 [0.6 / 2.4] | 0.6 / 2.0 [0.6 / 2.4] | fail / fail / fail |
| T10 | ≤ A + 10% (1.882 / 0.334 m) | 1.711 / 0.304 | 1.683 / 0.314 | 1.682 / 0.316 | pass / **pass** / **pass** |
| T11 | ≥ 90% blind zone (P5 only) | not measured | not measured | not measured | unverified (P5 not approved) |
| T12 | suites, Vivaldi smoke, diff explained | suites pass | suites pass; diffs explained | suites pass; diffs explained | **unverified**: Vivaldi did not run |
| T13 | p99 ≤ 2 ms on the Pi 5 | 50.5 µs | 62.2 µs | 387.2 µs | pass / **pass** / **pass** |

B and C pass T1, T10 and T13, fail T2–T9, and leave T11 and T12 unverified. **Neither passes overall
acceptance.** Both stay opt-in, off by default, and are not deployed; the Pi runs the legacy tracker.

### 11.8 Limitations

- Simulation only (Webots radar model, SUMO traffic): nothing here is real-world driving or safety evidence.
- The oracle applies the same rules to the truth of radar-detected vehicles; it judges what tracking changes in
  the decisions, not whether the rules are safe.
- P2's noise values were measured on the Stage 1 recordings, which also serve as diagnostic regression data; only
  the Stage 9 evaluation subset is untouched, one recording per configuration.
- Stage 9 coverage is below its pre-registered adequacy (62 vehicles above 25 m/s within 100 m); the generator places
  no traffic behind the ego, and fast oncoming traffic lies outside the slot-based criteria.
- T7–T9 are bounded by the detections (a return point that wanders over the vehicle body), not by P2's settings.
- When the ego and a vehicle ahead are both stopped, the radars often stop reporting it; the 3 s expiry then drops
  the track (T2's slow bands).
- B and C raise lead-slot spurious frames; not analysed yet. C adds unsupported CRITICAL frames on lateral motion.
- Browser validation did not run (Vivaldi page-level DevTools); the JS suite covers dashboard logic without a
  browser.
- The Pi bench measures tracker updates on recorded frames, not the deployed service with P1 or P2 (never enabled).
- Stage 1 caveat stands: 10 of its 30 runs were recorded with uncommitted changes (§10.1).

### 11.9 Recommended next stage

1. Keep P1 and P1+P2 off. P1 alone is not a deployment candidate; if a tracker change is ever activated it is P1+P2
   together, and only after the steps below and a full T1–T13 pass.
2. **P4 next** (covariance-based coasting and track management): the stopped-vehicle losses (T2's 0–5 m/s band),
   coasting with a stale lateral speed (the false HIGH/CRITICAL of cut_in-1124) and expiry. Then **P3** (global
   association gated with P2's covariance) for T5, T6 and the lead-slot spurious frames.
3. T7–T9 need a different measurement or object model (the radar return's position on the body, or another
   lateral sensor), a design decision rather than a noise setting. The limits stay.
4. A second campaign, only with approval: same-direction traffic starting behind the ego (or a slower ego), the
   diagnostic and evaluation split kept, and adequacy thresholds registered again before recording.
5. Vivaldi automation on this Mac needs a decision (e.g. a later Vivaldi build); until then a manual check in an
   isolated profile is the only browser evidence.
6. No P5, P6, retraining or Phase 7B without review.
