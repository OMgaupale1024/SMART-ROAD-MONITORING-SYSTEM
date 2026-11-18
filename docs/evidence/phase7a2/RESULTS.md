# Phase 7A.2 — bounded prospective comparison, 2026-11-17

**Outcome: useful stress-frame improvement, but the frozen candidate failed predictive acceptance. Do not proceed to live/Pi integration.** The validation-selected 60-stage, 15-leaf histogram-boosting model was frozen before all 40 fresh held-out executions. No post-test model swap, threshold adjustment, extra campaign or Pi action was performed. Phase 7A is published at [`fac5857`](https://github.com/OMgaupale1024/SMART-ROAD-MONITORING-SYSTEM/commit/fac585738ce14d38f4e50a28c77f7f33d673f883); remote branch equality was verified. Phase 7A.2 source/evidence remain local for review.

## Failure diagnosis before expansion

The original experiment was re-evaluated before publication; the artifact, historical splits and negative results were preserved byte-for-byte. Across the historical 43,233 samples, 434 false negatives included 121 without a lead and only nine with an immature lead. There were side tracks in 387 missed frames and lateral motion >0.3 m/s in 167. Of 588 false-positive frames, 185 lacked a lead and 271 had lateral motion. These categories overlap and are descriptive, not causal attribution. Two missed historical stress incidents had a mature lead throughout their 15 warning frames; another had no lead throughout. Missing observability, lateral/temporal representation and a linear model all mattered; more positive frames or a threshold change alone were not established solutions. [Recorded case analysis](diagnosis.json).

This justified a 30-feature LR control, a causal 44-feature contract, small nonlinear models and varied staged cut-ins/braking. SUMO mode 512 was found to enforce safety gaps despite the old comment; the new plan records explicit safe mode 512 or deliberate mode 0, without changing the target. The [official SUMO definition](https://sumo.dlr.de/docs/TraCI/Change_Vehicle_State.html#lane-change-mode-0xb6) supports that correction. An abrupt staged insertion can still be inherently unobservable before a vehicle appears; no simulator intent was supplied to the model.

## Collection and quality

All **140/140 authorized attempts completed**; the first six smoke attempts are included. There are 140 distinct configurations/seeds and 140 independent split groups, pairwise non-near to all 63 old configurations and one another under the conservative grouping relation. Seeds are the exact 140 values in the frozen plan (1001–1141 with one candidate input rejected before simulation). Five targeted families and eleven variants cover braking, motorcycle/autorickshaw cut-ins, slow entry, dual threats with hazards, mixed traffic and safe/resolving/ordinary intent. The original seven-family corpus is retained; no new standalone rear-threat model was trained.

| New split | Runs / groups | Supervised samples | Positive | Negative | Positive groups |
| --- | --- | --- | --- | --- | --- |
| train | 80 | 36442 | 627 | 35815 | 24 |
| validation | 20 | 9693 | 403 | 9290 | 9 |
| test | 20 | 8958 | 311 | 8647 | 8 |
| stress | 10 | 4855 | 377 | 4478 | 6 |
| ordinary | 10 | 5709 | 93 | 5616 | 1 |

Recorded **78,365** sensor samples; **65,657** supervised samples; **1,811** positives. Accepted-frame counters total 78,505, including one terminal marker per run; missed/rejected frames and recorder loss counters are zero. All runs terminated at their configured duration.

| Exclusion (mutually exclusive in exporter order) | Samples | Reason |
| --- | --- | --- |
| Unmatched current truth | 661 | 140 first samples without a truth frame; 521 physical-pose mapping gaps |
| At/after first footprint overlap | 5691 | Conservative censoring; overlap is not verified contact |
| Incomplete future window | 6356 | One or more of all 15 future observations missing, including run endings |

The independent audit recomputed every retained eligibility decision, future label and causal feature vector directly from raw observations, verified 1,147 manifest checksums and all per-run source snapshots, and found no discrepancy. No interpolation or invented truth filled the gaps. Complete 3 s future windows remain within one run; current risk and future minima are metadata/labels only. [Audit evidence](quality-audit.json), [re-run script](audit.py).

Retained-truth / eligible pre-onset incident counts are train 41/33, validation 27/23, test 17/14, stress 21/18 and ordinary 6/6. Multiple incidents in one configuration are not independent configurations. Structural missing-slot rates are lead 62.2%, left 30.8%, right 85.0%; lead range-trend null rate is 64.9%. The [44-feature audit](feature-audit.json) records every category, ordering and missing rate. These nulls represent absent or discontinuous observations, not recording loss.

Staging quality differs from run completion: 120 planned event actions yielded 114 completed actions, two autorickshaw insertion failures, two blocked actions in one combined run and two never-triggered actions because the ego did not reach their trigger. All failures were in development/validation; fresh test/stress actions completed. The six smoke attempts contained the two insertion failures and blocked combined run. Capping insertion speed at the declared vehicle maximum fixed the insertion bug; no attempt was retried or relabeled. Earlier progress shorthand attributing all three affected smoke runs to insertion speed was inaccurate. Count final event phases, not `spawn_requested` messages: the old failure path also logs a request after a failed insertion.

The [staging audit](staging-audit.json) retains the six noncompleted actions and their execution identities.

| Family | Negative samples | Positive samples |
| --- | --- | --- |
| combined | 9903 | 417 |
| cut_in | 23146 | 722 |
| mixed | 4673 | 25 |
| normal | 13180 | 139 |
| sudden_brake | 12944 | 508 |

Entire-execution truth contains 135 temporal forward-near-miss episodes and 27 footprint-overlap episodes (678 and 128 frames respectively). These merge positives separated by <=1 s and include observations beyond the supervised censoring boundary. They differ from eligible pre-onset incidents below; neither frames nor episode proxies are measured physical collisions.

Webots R2025b / SUMO 1.27.1 generated 15,673.0 simulated seconds in 2,547.0 Webots wall seconds (6.15×; excludes startup/flush and training). Campaign storage at completion: 894,126,352 bytes, below 4 GB. Observed waited-child CPU time: 3,860.93 s; cumulative peak child RSS across runners: 1,764,884,480 bytes. These are available resource counters, not per-process instantaneous peak CPU measurements. Training was 28 fits in 69.68 s, peak training-process RSS 1,322,729,472 bytes. No Ollama workload, Pi traffic or live-server change was used.

## Target, features and grouped selection

Target remains `roadsense.forward-risk.v1`: any forward physical-pose near miss in **(t,t+3 s]**, with all 15 future 200 ms frames present. A current forward near miss is intended forward-box overlap, or ego speed >2 m/s and forward gap <2 m or closing TTC <1.5 s. TTC requires closing speed >0.1 m/s. Footprints use oriented intended dimensions and are a collision proxy; physical contact is not established. Ground-truth and current-risk fields never enter inference.

Version 2 preserves the original 30 inputs in order and adds 14 causal kinematic/one-second trend/continuity inputs. Every raw frame is processed before supervised filtering. Buffers reset on run, sequence/time gaps, absence/staleness, age resets and implausible motion; trends require >=0.4 s continuity. Slot continuity is a proxy without object IDs; ego-relative acceleration is an approximation during turning. The 1.85 m corridor constant uses nominal dimensions, not truth. Measured TTC can reproduce an existing calculation; its presence is stated openly. No rule decisions, IDs, future minima, simulator poses, planned maneuvers or labels are inference inputs. Structural nulls are retained, not invented measurements. Full feature names/order and train medians are in [model.json](model.json); missing rates are in the audit and external manifests.

Training uses original **train only** (27,457 samples, 526 positives, 40 groups) plus new train (36,442 / 627 / 80): **63,899 samples, 1,153 positives, 120 groups, 40 positive groups**. New validation has 20 groups / nine positive groups / 23 eligible incidents. Three-fold stratified group CV fits imputation/scaling/class weights only within training folds; full fits use training only. Existing historical validation/test/stress never enter fitting. No synthetic oversampling or random frame split was used.

Fixed search: LR C=1 (30 inputs; 44 unweighted/balanced); RF 80 trees, depth 6/8, leaf minimum 10, balanced subsamples; histogram boosting 60 iterations, 7/15 leaves, learning rate 0.1, leaf minimum 15, balanced classes and no early stopping. Group CV seed 20261117. Validation selects among a fixed 22-score threshold grid, requiring FPR <=5% and <=20 false episodes/hour on unstaged validation ordinary/dense runs, then ranks pre-onset incident recall, F2, lead, false alarms and size. A threshold of 1 produces no warnings for Boost 7; it is not a useful model at that operating point.

| Candidate | OOF AP (train groups) | Validation F2 | Validation pre-onset incidents | Frozen threshold | JSON bytes |
| --- | --- | --- | --- | --- | --- |
| Boost 15 leaves (selected) | 0.344 | 0.607 | 16/23 | 0.7599796006 | 53075 |
| Boost 7 leaves | 0.327 | 0.000 | 0/23 | 1.0000000000 | 27563 |
| LR 30 | 0.167 | 0.241 | 8/23 | 0.2025982257 | 4564 |
| LR 44 | 0.178 | 0.290 | 8/23 | 0.1555257764 | 5918 |
| LR 44 balanced | 0.149 | 0.067 | 3/23 | 0.9900000000 | 5924 |
| RF depth 6 | 0.287 | 0.314 | 12/23 | 0.7599796006 | 190658 |
| RF depth 8 | 0.297 | 0.203 | 9/23 | 0.7599796006 | 397682 |

Selected **hgb15**, threshold **0.759979600592**, using validation only. The matched TTC score threshold is 0.070356236397 (1/(1+TTC), about 13.21 s, with the same <2 m gap override). Fixed TTC 4.5 s is also reported. These share validation feasibility constraints, not identical FPR/alert counts. All candidates/thresholds and the numerical acceptance protocol were frozen before collecting the 40 held-out runs; the gate and artifact hashes bind the freeze to the plan. No retuning followed evaluation.

## Fresh test comparison

| Model | Precision | Recall | F2 | AP | False alarms/h | Pre-onset incidents | Pre-developing frame recall | Median lead (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Frozen Phase 7A | 9.5% | 37.9% | 0.237 | 0.244 | 134.6 | 7/14 | 32.2% | 3.000 |
| TTC 4.5 s | 29.3% | 28.0% | 0.282 | 0.192 | 48.2 | 5/14 | 17.8% | 1.000 |
| TTC matched | 15.3% | 36.7% | 0.287 | 0.192 | 78.4 | 8/14 | 28.7% | 1.100 |
| LR 30 | 47.0% | 22.5% | 0.251 | 0.359 | 46.2 | 5/14 | 14.8% | 1.200 |
| LR 44 | 44.2% | 28.0% | 0.302 | 0.361 | 72.3 | 7/14 | 22.2% | 3.000 |
| LR 44 balanced | 71.9% | 14.8% | 0.176 | 0.287 | 10.0 | 3/14 | 6.1% | 0.400 |
| RF depth 6 | 55.8% | 32.5% | 0.354 | 0.428 | 38.2 | 7/14 | 13.5% | 0.400 |
| RF depth 8 | 73.6% | 29.6% | 0.336 | 0.418 | 10.0 | 6/14 | 12.2% | 0.400 |
| Boost 7 leaves | — | 0.0% | 0.000 | 0.370 | 0.0 | 0/14 | 0.0% | — |
| Boost 15 leaves (selected) | 37.2% | 37.9% | 0.378 | 0.367 | 80.4 | 6/14 | 17.8% | 0.800 |

## Fresh stress comparison

| Model | Precision | Recall | F2 | AP | False alarms/h | Pre-onset incidents | Pre-developing frame recall | Median lead (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Frozen Phase 7A | 35.5% | 42.4% | 0.408 | 0.324 | 129.8 | 15/18 | 44.7% | 1.600 |
| TTC 4.5 s | 59.2% | 50.4% | 0.519 | 0.472 | 70.4 | 11/18 | 41.6% | 2.400 |
| TTC matched | 34.8% | 53.1% | 0.480 | 0.472 | 63.0 | 12/18 | 45.5% | 2.300 |
| LR 30 | 61.9% | 27.6% | 0.310 | 0.429 | 48.2 | 11/18 | 26.1% | 1.000 |
| LR 44 | 61.4% | 32.1% | 0.355 | 0.447 | 55.6 | 14/18 | 31.5% | 1.500 |
| LR 44 balanced | 57.9% | 5.8% | 0.071 | 0.383 | 0.0 | 5/18 | 5.1% | 1.200 |
| RF depth 6 | 64.6% | 43.5% | 0.465 | 0.577 | 70.4 | 12/18 | 37.0% | 2.200 |
| RF depth 8 | 62.5% | 27.9% | 0.313 | 0.554 | 44.5 | 11/18 | 22.2% | 1.600 |
| Boost 7 leaves | — | 0.0% | 0.000 | 0.548 | 0.0 | 0/18 | 0.0% | — |
| Boost 15 leaves (selected) | 54.1% | 71.4% | 0.671 | 0.553 | 166.8 | 15/18 | 64.6% | 1.800 |

## Fresh ordinary comparison

| Model | Precision | Recall | F2 | AP | False alarms/h | Pre-onset incidents | Pre-developing frame recall | Median lead (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Frozen Phase 7A | 0.8% | 10.8% | 0.030 | 0.021 | 123.0 | 3/6 | 11.7% | 1.200 |
| TTC 4.5 s | 19.1% | 19.4% | 0.193 | 0.172 | 25.2 | 2/6 | 7.8% | 0.600 |
| TTC matched | 7.5% | 21.5% | 0.157 | 0.172 | 88.3 | 2/6 | 10.4% | 0.800 |
| LR 30 | 10.0% | 2.2% | 0.026 | 0.073 | 25.2 | 2/6 | 2.6% | 1.200 |
| LR 44 | 13.2% | 5.4% | 0.061 | 0.071 | 44.1 | 2/6 | 6.5% | 1.100 |
| LR 44 balanced | 37.5% | 3.2% | 0.039 | 0.104 | 3.2 | 1/6 | 3.9% | 1.600 |
| RF depth 6 | 69.4% | 26.9% | 0.306 | 0.478 | 9.5 | 5/6 | 18.2% | 1.000 |
| RF depth 8 | 57.5% | 24.7% | 0.279 | 0.421 | 3.2 | 4/6 | 15.6% | 1.200 |
| Boost 7 leaves | — | 0.0% | 0.000 | 0.406 | 0.0 | 0/6 | 0.0% | — |
| Boost 15 leaves (selected) | 52.3% | 62.4% | 0.600 | 0.419 | 37.8 | 6/6 | 55.8% | 1.200 |

The selected model improves test precision/F2/AP over frozen Phase 7A, but keeps the same frame recall and detects fewer pre-onset test incidents (6/14 vs 7/14). Stress frame recall rises from 42.4% to 71.4%, AP from .324 to .553 and pre-developing frame recall from 44.7% to 64.6%; pre-onset incident detection is tied at 15/18, exceeding matched TTC 12/18. Stress false alarms increase from 129.8 to 166.8/h. The ordinary cohort improves detection and reduces alarms from 123.0 to 37.8/h, still far above the <=10/h target. RF depth 6 has promising descriptive test AP and ordinary alarms, but was not selected on validation and cannot replace the frozen candidate after viewing these outcomes. No candidate demonstrates all acceptance targets at its frozen threshold.

Post-freeze [failure-case inspection](fresh-failure-cases.json) found that three of eight missed test incidents had no lead throughout their warning window; one had no lead or side track at all. Four had mature leads throughout, although slot presence does not establish that the threatening vehicle was selected. One mature-lead miss already had observed TTC around 1.52 s, a detectable kinematic cue; another had observed TTC above 35 s, indicating an observability/association discrepancy. Several maximum scores were 0.62–0.73, below the frozen 0.76 threshold. Lowering it might recover some warnings but cannot be claimed to solve the already excessive false-alert rate. Of 199 false-positive test frames, 141 had lateral motion >0.3 m/s; lateral ambiguity remains material. This inspection changed neither model nor threshold.

## Selected-model errors, timing and uncertainty

| Split | Confusion [[TN,FP],[FN,TP]] | FNR | FPR | F1 | ROC AUC | Brier | ECE (10 bins) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| validation | [[9099, 191], [154, 249]] | 38.2% | 2.1% | 0.591 | 0.864 | 0.056 | 0.115 |
| test | [[8448, 199], [193, 118]] | 62.1% | 2.3% | 0.376 | 0.837 | 0.057 | 0.107 |
| stress | [[4250, 228], [108, 269]] | 28.6% | 5.1% | 0.616 | 0.927 | 0.088 | 0.149 |
| ordinary | [[5563, 53], [35, 58]] | 37.6% | 0.9% | 0.569 | 0.941 | 0.042 | 0.120 |

**test**: 17 retained-truth incidents; 6/14 with eligible pre-onset windows were detected; during-event eligible incidents 16, detection 62.5%. False episodes 40 over 0.4977 supervised simulated hours. Detected warning leads (seconds) [0.2, 0.2, 0.4, 1.2, 3.0, 3.0]; quartiles [0.25, 0.8, 2.55]. Group-bootstrap pre-onset recall 95% interval [0.26666666666666666, 0.6666666666666666]; undefined resamples 0/1000.

**stress**: 21 retained-truth incidents; 15/18 with eligible pre-onset windows were detected; during-event eligible incidents 18, detection 88.9%. False episodes 45 over 0.2697 supervised simulated hours. Detected warning leads (seconds) [0.2, 0.6, 1.2, 1.2, 1.6, 1.6, 1.6, 1.8, 2.2, 2.8, 3.0, 3.0, 3.0, 3.0, 3.0]; quartiles [1.4, 1.8, 3.0]. Group-bootstrap pre-onset recall 95% interval [0.5, 1.0]; undefined resamples 0/1000.

**ordinary**: 6 retained-truth incidents; 6/6 with eligible pre-onset windows were detected; during-event eligible incidents 6, detection 100.0%. False episodes 12 over 0.3172 supervised simulated hours. Detected warning leads (seconds) [0.2, 0.2, 1.0, 1.4, 2.2, 3.0]; quartiles [0.4, 1.2, 2.0]. Group-bootstrap pre-onset recall 95% interval [1.0, 1.0]; undefined resamples 355/1000.

Intervals resample whole configuration groups, not adjacent frames. Fresh test contains only eight positive groups, missing the target of ten; stress has six. Ordinary has ten groups and 0.3172 valid hours, but **all 93 positive samples and six eligible incidents come from one configuration**. Its conditional [1,1] incident-bootstrap interval is uninformative about generalization; 355/1,000 resamples have no positive incident. Detected-only lead distributions exclude misses and cannot imply good overall warning coverage.

A false alert is a contiguous warning episode with no target-positive retained frame. A gap >0.202 s breaks the episode. Exposure counts retained supervised samples ×0.2 s, not the whole elapsed run. Warning chatter is not suppressed by a new debounce policy. Pre-onset warnings exclude currently developing risk and preceding episode overlap; developing-event detections are reported separately. Counts merge nearby incidents temporally rather than claiming independent physical crashes.

| Split / family | Samples | Positives | Precision | Recall | F2 | Pre-onset incidents | False alarms/h |
| --- | --- | --- | --- | --- | --- | --- | --- |
| test / combined | 1735 | 57 | 56.0% | 24.6% | 0.277 | 1/3 | 31.1 |
| test / cut_in | 4826 | 152 | 28.4% | 36.2% | 0.343 | 3/7 | 108.2 |
| test / mixed | 464 | 0 | — | — | — | 0/0 | 0.0 |
| test / sudden_brake | 1933 | 102 | 50.0% | 48.0% | 0.484 | 2/4 | 74.5 |
| stress / combined | 1047 | 32 | 53.6% | 93.8% | 0.815 | 2/2 | 86.0 |
| stress / cut_in | 2670 | 211 | 56.3% | 65.4% | 0.634 | 7/10 | 175.3 |
| stress / sudden_brake | 1138 | 134 | 51.5% | 75.4% | 0.690 | 6/6 | 221.4 |
| ordinary / normal | 5709 | 93 | 52.3% | 62.4% | 0.600 | 6/6 | 37.8 |

Historical stress remains a benchmark: selected model 4/6 pre-onset incidents versus original 2/6. It was already examined during diagnosis and is not fresh acceptance evidence. Full historical comparisons and calibration bins remain in the external evaluation; compact fresh metrics are in [metrics.json](metrics.json). Balanced/enriched scores are explicitly **uncalibrated risk scores**, not real-world accident probabilities.

## Frozen acceptance and recommendation

| Criterion frozen before new test collection | Pass |
| --- | --- |
| beats_frozen_incident_recall | False |
| coverage | False |
| early_frame_recall | False |
| lead | False |
| model_size | True |
| native_parity | True |
| ordinary_false_alarms | False |
| stress_incident_recall | True |
| test_incident_recall | False |
| model_p95_le_2ms | True |
| feature_plus_model_p95_le_2ms | True |
| model_load_rss_le_64MiB | True |

Coverage fails solely because fresh test has eight rather than ten positive groups (14 eligible incidents meet the incident count target). Test pre-onset recall is 42.9% (<70%), test early frame recall 17.8% (<50%), test median detected lead 0.8 s (<1 s), and ordinary alarms 37.8/h (>10). The stress recall criterion passes but does not rescue these failures. These are prototype research gates, not certification. No groups were moved to repair a shortfall.

**Recommendation: do not proceed to Phase 7B live integration.** Keep a clearly labeled offline demonstration of the trained model and original baseline. Before another separately approved experiment, prioritize replay-based inspection of test cut-in/combined misses and warning fragmentation, and validate sensor slot continuity/object association. The LR 30/44 controls show only modest improvements; the experiment does not isolate temporal features from nonlinear model capacity. Better track-aligned lateral/lead history and fewer abrupt unobservable insertions are more justified than a larger blind campaign. Any future parameter/model/threshold work makes these evaluated holdouts historical and requires a new prospective evaluation plan; no further runs are authorized here.

## Artifact, runtime and reproduction

Selected version `roadsense-7a2-hgb15-v1`; inspectable JSON **53,075 bytes**, 44 ordered inputs, training medians, 60 numerical trees with weighted leaves/intercept, frozen threshold and source/dataset hashes. Standard-library Python only; no pickle or ML runtime on the Pi is required by this representation. Loading validates schema/numerics/dimensions/acyclic trees; null handling and statuses preserve the safety failure boundary. A future adapter must run as a separate shadow observer and catch loader/observer errors; no adapter is connected.

Mac ARM64 / Python 3.12.14, 10,000 recorded raw frames: model-only latency p50/p95/p99 **26.208/29.459/34.667 µs**; causal features + model **33.042/37.750/43.583 µs**. Model load 1262.459 µs; measured peak-RSS increase across loading before input allocation 245,760 bytes. Third-party ML imports were blocked. Measurements exclude DB I/O and are not Pi measurements.

All seven exported models match their native scikit-learn implementations: development parity covers 73,592 rows; final fresh plus historical holdouts covers 81,433 rows, with overlap. Together they cover all 108,890 distinct valid samples across both campaigns. Selected max absolute score error <=3.34e-16, zero threshold disagreements; worst of all models <5.7e-15. Forest float32 input semantics are reproduced. No untrusted serialized executable object is loaded.

External data/artifacts (bulk data stays outside Git):

```text
/Users/smash_mario/Library/Application Support/RoadSense/campaigns/phase7a2-20261117
  plan.json, campaign.jsonl, heldout-gate.json, completion.json
  runs/<unique execution>/scenario.json, ground_truth.jsonl, worlds/, logs
  source/<hash>/...
  history/roadsense-history.sqlite3
  history-development.sqlite3, development-checkpoint.json
  dataset-development/, features-development/  # immutable first-100 selection data
  dataset-final/  # original 30-feature labels, manifest, raw-input checksums
  features-final/ # causal 44 features, manifest, parent checksums
  features-historical/ # derived original corpus; original data unchanged
/Users/smash_mario/Library/Application Support/RoadSense/ml/phase7a2-20261117
  model.json, all seven candidates, freeze.json, development.json
  evaluation.json, runtime.json, source/
```

The first-100 database checkpoint retains SHA `893b7dbfa031c9f93956bc9cf6c417d400ecda7bfa7f31009385c4b44c7dfe2f`. Development checksum lists refer to that database state; the campaign database subsequently appended heldouts. The final export checksum list verifies the final database. The training runner compared all first-100 feature/label rows before and after collection and found them unchanged. Source base commit is `fac5857` with explicit dirty source hashes and retained exact snapshots; do not claim that commit alone contains Phase 7A.2 sources.

Reproduce exports/audit without launching simulation or changing the frozen evidence:

```sh
export PYTHONPATH="$HOME/.local/share/roadsense/python312:$PWD/src"
CAMPAIGN="$HOME/Library/Application Support/RoadSense/campaigns/phase7a2-20261117"
python3.12 tools/scenario_dataset.py "$CAMPAIGN" "$CAMPAIGN/dataset-verification"
python3.12 tools/expand_features.py "$CAMPAIGN/dataset-verification" "$CAMPAIGN/features-verification"
python3.12 docs/evidence/phase7a2/audit.py
```

Verification directories must be new. Base sample bytes reproduce; temporal rows embed the new base path in metadata `origin`, so compare `(run_id,sequence,features,labels)` rather than demanding byte identity across different destination paths. Training reproduction uses the frozen `source/` copies and a new output directory, never overwrites this experiment, and must not be confused with another independent final evaluation. Do not rerun the campaign for reproduction without new authorization.

## Limits and operational checks

This is a single highway environment with structured lanes, a sample ego controller, approximate intended boxes and car visuals for autorickshaws. RoadSense supplies advice and never controls the ego. Physical outcomes remain variable despite seeded inputs. Five recording source snapshots are retained with per-run hashes; all were audited. Retained labels have valid alignment, but 521 missing physical-pose frames and their future windows reduce coverage. Slot identity without IDs, staged insertion discontinuities, correlated scenarios beyond the declared grouping rule, coarse threshold search, class enrichment and limited ordinary positive diversity constrain conclusions. No real-world safety or complete Indian urban/rural coverage claim follows.

Regression validation: **529 Python, 65 JavaScript, 60 archived Arduino tests passed**. Original Webots world and authoritative autonomy/server source have no Phase 7A.2 diff. Original dev-server PID 36233 and Webots PID 36894 remained running; campaign processes exited. No browser test was needed for this offline change, and no Vivaldi automation result is claimed. No Pi connection/deployment/restart/reboot, systemd/history-mode change, live API hook, dashboard redesign, paid infrastructure, credentials or fabricated GPS were added.
