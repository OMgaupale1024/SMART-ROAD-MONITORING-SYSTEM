# Phase 7A — trained forward-risk logistic feasibility model

**A real lightweight numerical model is trained and reproducible. Do not proceed to live Pi integration yet.** The selected unweighted logistic model improves this small test set over the forward-TTC baseline, but stress performance is worse on several important measures. Validation/test have only two positive configuration groups each; test has only one evaluable pre-onset incident. Training-only OOF recall is 36.9%, validation recall 18.0%, and stress recall 19.2%. These are material misses, not evidence of general road safety.

## Phase 6D publication and data verification

Published commit: `7ae80d6c9015b4bb18c6c551a2cbe213b782ce87` on origin/roadsense-v2-edge-ai, verified by remote ref after push. Reviewed source, 30-feature audit and compact evidence; no campaign data or credentials were staged. All 611 retained checksums and recording-source hashes verified. A fresh export reproduced the sample bytes; manifest fields match except the exporter source hash after fixing checksum paths for exports outside the campaign directory. The regression check covers that destination.

Dataset SHA256: `18d58ee83e05e11164876b68feb0cebacba9755eb3395d6f0a8097cb2eb652ed`. The frozen corpus contains 52,135 recorded and 43,233 supervised samples (738 positive /42,495 negative), 70 runs, 63 configurations and 61 groups. Exclusions: 229 unusable/missing truth matches, 1,939 incomplete future windows, 6,734 samples at/after first overlap proxy =8,902. Labels were recomputed from physical Webots truth within the same execution over all 15 future 200 ms observations. No future information or simulator truth is an inference input.

## Method and freeze

Exact 30-feature ordering retained; median imputation (keep empty columns), StandardScaler and L2 logistic C=1, lbfgs, tol=1e-6, max_iter=2000. Four shuffled StratifiedGroupKFold splits, seed 20261117, only training groups; imputation/scaling and balanced weights fit separately inside each fold. No feature selection, oversampling, calibration fitting, hyperparameter sweep, RF, boosting or neural networks. Ten fits: four CV folds plus one full-train fit per candidate.

Thresholds and the selected model were frozen from training OOF F2, with predetermined false-alarm/incident/lead tie-breaks, before a single held-out evaluation. Validation, test and stress did not fit preprocessing, coefficients, class weights or selection. Frozen unweighted threshold: **0.102320683738**; balanced: **0.800351042846**. The 0.5 diagnostic is saved but was not used for selection. Balanced full-train weights were negative 0.509766, positive 26.099810; unweighted used None.

Source commit: `7ae80d6c9015b4bb18c6c551a2cbe213b782ce87` (Phase 6D base). Training code was uncommitted; exact source hashes and copies are in the artifact source/ directory. No claim is made that the training code is contained in that base commit. Fit time 4.312 s; peak training-process RSS 474.0 MB.

| Fold | Fit groups | OOF groups | Fit positive frames | OOF positive frames |
| --- | --- | --- | --- | --- |
| 1 | 32 | 8 | 372 | 154 |
| 2 | 30 | 10 | 404 | 122 |
| 3 | 28 | 12 | 386 | 140 |
| 4 | 30 | 10 | 416 | 110 |

## Split and incident coverage

| Split | Samples | Positive /negative | Groups | Positive groups | Temporal incidents | Pre-onset evaluable | During evaluable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| train | 27457 | 526 /26931 | 40 | 16 | 26 | 23 | 22 |
| validation | 4840 | 61 /4779 | 7 | 2 | 3 | 3 | 3 |
| test | 6552 | 47 /6505 | 7 | 2 | 2 | 1 | 2 |
| stress | 4384 | 104 /4280 | 7 | 3 | 6 | 6 | 5 |

Incidents are physical-truth temporal clusters with <=1 s between positive frames, cut at first overlap proxy. They are not statistically independent crashes. All repetitions remain grouped. An incident without a retained pre-event sample is unavailable for early-warning evaluation, not counted as a successful prediction or an avoidable miss. The second positive test group has developing-risk frames but no usable pre-onset window.

## Training out-of-fold comparison

| Model | Threshold | Precision | Recall | F2 | AP | False alarms/h | Pre-onset detected | Median lead s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| always_negative | 1.000000 | — | 0.0% | 0.000 | 0.019 | 0.0 | 0/23 | — |
| ttc_forward | 0.181818 | 14.5% | 17.3% | 0.167 | 0.114 | 54.4 | 7/23 | 1.0 |
| unweighted | 0.102321 | 27.0% | 36.9% | 0.344 | 0.218 | 85.2 | 10/23 | 2.9 |
| balanced | 0.800351 | 12.9% | 36.1% | 0.265 | 0.137 | 179.0 | 12/23 | 2.8 |

The TTC baseline uses only the measured forward lead: clearance=max(0,lead_lon−3.9 m), closing=−lead_v_lon>0.1 m/s, ego speed>2 m/s, warning for TTC<=4.5 s or clearance<2 m. Its score is 1/(1+TTC), or 1 for a sub-2 m gap. This fixed 4.5 s threshold extrapolates the 1.5 s near-miss boundary over the 3 s horizon under constant velocity. It does not change deployed safety logic and is not a comparison to unrelated rear/hazard actions. Always negative has no positive predictions and precision is undefined.

## Held-out test comparison — all four baselines/models

| Model | TN /FP /FN /TP | Precision | Recall | F1 | F2 | AP | ROC-AUC |
| --- | --- | --- | --- | --- | --- | --- | --- |
| always_negative | 6505 /0 /47 /0 | — | 0.0% | 0.000 | 0.000 | 0.007 | 0.500 |
| ttc_forward | 6381 /124 /39 /8 | 6.1% | 17.0% | 0.089 | 0.125 | 0.100 | 0.531 |
| unweighted | 6443 /62 /21 /26 | 29.5% | 55.3% | 0.385 | 0.471 | 0.368 | 0.716 |
| balanced | 6341 /164 /19 /28 | 14.6% | 59.6% | 0.234 | 0.368 | 0.437 | 0.745 |

| Model | FNR | FPR | Brier | 10-bin ECE | False alarms/h | Warning duty | Pre-onset detected | Median lead s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| always_negative | 100.0% | 0.0% | 0.00717 | 0.00717 | 0.0 | 0.0% | 0/1 | — |
| ttc_forward | 83.0% | 1.9% | 0.00925 | 0.01964 | 79.7 | 2.0% | 1/1 | 0.4 |
| unweighted | 44.7% | 1.0% | 0.00551 | 0.00871 | 52.2 | 1.3% | 1/1 | 1.4 |
| balanced | 40.4% | 2.5% | 0.12352 | 0.28060 | 85.2 | 2.9% | 1/1 | 1.8 |

The selected model improves test F2/AP and false alarms relative to this TTC baseline; it does **not** establish a reliable advantage across unseen configurations. On stress, TTC detects 4/6 evaluable incidents, selected logistic 2/6; TTC has F2 .214/AP .126 versus logistic .189/.064. No threshold or model was changed after seeing this.

## Selected model on every partition

| Split | TN /FP /FN /TP | Precision | Recall | FNR | FPR | F1 | F2 | AP | ROC-AUC | Brier |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| train | 26548 /383 /279 /247 | 39.2% | 47.0% | 53.0% | 1.4% | 0.427 | 0.452 | 0.330 | 0.818 | 0.01489 |
| validation | 4730 /49 /50 /11 | 18.3% | 18.0% | 82.0% | 1.0% | 0.182 | 0.181 | 0.163 | 0.505 | 0.01167 |
| test | 6443 /62 /21 /26 | 29.5% | 55.3% | 44.7% | 1.0% | 0.385 | 0.471 | 0.368 | 0.716 | 0.00551 |
| stress | 4186 /94 /84 /20 | 17.5% | 19.2% | 80.8% | 2.2% | 0.183 | 0.189 | 0.064 | 0.589 | 0.02486 |

The train row is fitted-model performance, not an unbiased estimate; use the earlier OOF table for training generalization. Full per-family/per-model metrics, default-threshold diagnostics and both confusion matrices are in evaluation.json.

## Warning timing and false alarms

| Split | Model | Pre-onset detection | During detection | Median pre-onset lead s | False alarms/h | FP frames/h |
| --- | --- | --- | --- | --- | --- | --- |
| train | always_negative | 0/23 | 0 (22 eligible) | — | 0.0 | 0.0 |
| train | ttc_forward | 7/23 | 0.409 (22 eligible) | 1.0 | 54.4 | 350.7 |
| train | unweighted | 14/23 | 0.455 (22 eligible) | 2.5 | 60.3 | 251.1 |
| train | balanced | 14/23 | 0.455 (22 eligible) | 2.6 | 129.1 | 516.6 |
| validation | always_negative | 0/3 | 0 (3 eligible) | — | 0.0 | 0.0 |
| validation | ttc_forward | 1/3 | 0.333 (3 eligible) | 0.2 | 81.8 | 516.9 |
| validation | unweighted | 1/3 | 0.333 (3 eligible) | 0.8 | 55.8 | 182.2 |
| validation | balanced | 1/3 | 0.333 (3 eligible) | 1.0 | 145.0 | 554.1 |
| test | always_negative | 0/1 | 0 (2 eligible) | — | 0.0 | 0.0 |
| test | ttc_forward | 1/1 | 0.500 (2 eligible) | 0.4 | 79.7 | 340.7 |
| test | unweighted | 1/1 | 0.500 (2 eligible) | 1.4 | 52.2 | 170.3 |
| test | balanced | 1/1 | 0.500 (2 eligible) | 1.8 | 85.2 | 450.5 |
| stress | always_negative | 0/6 | 0 (5 eligible) | — | 0.0 | 0.0 |
| stress | ttc_forward | 4/6 | 0.600 (5 eligible) | 1.2 | 69.8 | 500.9 |
| stress | unweighted | 2/6 | 0.200 (5 eligible) | 2.1 | 94.4 | 385.9 |
| stress | balanced | 2/6 | 0.200 (5 eligible) | 2.2 | 156.0 | 652.8 |

Lead time is the earliest warning sample within the 3 s target window, outside developing risk and after the previous incident; it is capped by the horizon and is not first alarm onset or time before measured contact. False alarms are consecutive warning episodes with no target-positive frame, divided by valid sample exposure (n×0.2 s). Gaps and wall time are excluded. Sustained mixed episodes can mask nuisance alarms, so frame FPR, FP frames/h and duty cycle are also reported. Even the selected test model produces about 52 false alarm episodes/hour; validation ~56 and stress ~94. This is unsuitable as an automatic action trigger.

| Split | Future-positive frames before developing | Recall before developing | Future-positive frames already developing | Recall already developing |
| --- | --- | --- | --- | --- |
| train | 389 | 33.2% | 137 | 86.1% |
| validation | 45 | 8.9% | 16 | 43.8% |
| test | 41 | 48.8% | 6 | 100.0% |
| stress | 90 | 14.4% | 14 | 50.0% |

The selected model catches many developing situations better than early ones. Validation pre-developing recall is only 8.9%, stress 14.4%; test is 48.8%. This distinction materially weakens the early-prediction claim.

## Selected model by scenario family

| Split | Family | Positive frames | Precision | Recall | F2 | Pre-onset incidents detected |
| --- | --- | --- | --- | --- | --- | --- |
| train | combined | 181 | 69.3% | 53.6% | 0.561 | 4/6 |
| train | cut_in | 61 | 0.0% | 0.0% | 0.000 | 0/4 |
| train | mixed | 0 | 0.0% | — | 0.000 | 0/0 |
| train | normal | 0 | 0.0% | — | 0.000 | 0/0 |
| train | potholes | 15 | 5.6% | 6.7% | 0.064 | 1/1 |
| train | sudden_brake | 220 | 51.0% | 58.6% | 0.569 | 8/10 |
| train | tailgating | 49 | 21.7% | 40.8% | 0.347 | 1/2 |
| validation | combined | 15 | 0.0% | 0.0% | 0.000 | 0/1 |
| validation | cut_in | 0 | 0.0% | — | 0.000 | 0/0 |
| validation | mixed | 0 | 0.0% | — | 0.000 | 0/0 |
| validation | normal | 0 | 0.0% | — | 0.000 | 0/0 |
| validation | potholes | 0 | 0.0% | — | 0.000 | 0/0 |
| validation | sudden_brake | 46 | 84.6% | 23.9% | 0.279 | 1/2 |
| validation | tailgating | 0 | 0.0% | — | 0.000 | 0/0 |
| test | combined | 12 | 81.8% | 75.0% | 0.763 | 0/0 |
| test | cut_in | 0 | 0.0% | — | 0.000 | 0/0 |
| test | mixed | 0 | 0.0% | — | 0.000 | 0/0 |
| test | normal | 0 | 0.0% | — | 0.000 | 0/0 |
| test | potholes | 0 | 0.0% | — | 0.000 | 0/0 |
| test | sudden_brake | 35 | 73.9% | 48.6% | 0.521 | 1/1 |
| test | tailgating | 0 | 0.0% | — | 0.000 | 0/0 |
| stress | combined | 32 | 0.0% | 0.0% | 0.000 | 0/2 |
| stress | cut_in | 0 | 0.0% | — | 0.000 | 0/0 |
| stress | mixed | 15 | 0.0% | 0.0% | 0.000 | 0/1 |
| stress | normal | 0 | 0.0% | — | 0.000 | 0/0 |
| stress | potholes | 0 | 0.0% | — | 0.000 | 0/0 |
| stress | sudden_brake | 57 | 32.3% | 35.1% | 0.345 | 2/3 |
| stress | tailgating | 0 | 0.0% | — | 0.000 | 0/0 |

Zero positive observations means recall is undefined, not perfect. The selected model misses all four evaluable training cut-in incidents and all stress combined/mixed incidents. Family labels never entered the inference vector; these are failure analyses, not extra feature tuning.

## Uncertainty and calibration

| Split | Positive groups | Recall 95% group bootstrap | Precision 95% group bootstrap | Pre-onset detected | Illustrative Wilson interval |
| --- | --- | --- | --- | --- | --- |
| train | 16 | [0.26805967111690004, 0.5836098637121286] | [0.18238880218493952, 0.5149411732365715] | 14/23 | [40.8%, 77.8%] |
| validation | 2 | [0.0, 0.2391304347826087] | [0.0, 0.5] | 1/3 | [6.1%, 79.2%] |
| test | 2 | [0.4857142857142857, 0.75] | [0.0, 0.6818779904306219] | 1/1 | [20.7%, 100.0%] |
| stress | 3 | [0.0, 0.3508771929824561] | [0.0, 0.29415386844392927] | 2/6 | [9.7%, 70.0%] |

Bootstrap: 1,000 whole-group resamples, seed 20261117. Zero-denominator replicates are omitted and counted in JSON. For the single detectable test incident, the conditional bootstrap incident interval degenerates to [1,1] and omits 323/1,000 replicates; **that is not useful uncertainty evidence**. The illustrative 1/1 Wilson interval is about [21%,100%], and even it assumes independent Bernoulli events, which repeated/configuration-correlated incidents do not guarantee. Frame intervals with two positive groups mostly reweight those same known configurations and cannot establish population generalization.

Scores remain **uncalibrated risk scores**, not reliable collision/near-miss probabilities. Balanced training shifts the class prior: test Brier .124/ECE .281 are much worse than unweighted .00551/.00871. Unweighted stress Brier .02486 is worse than always negative .02372. Small ECE/Brier can be dominated by negatives. No probability calibrator was fit with these few independent events.

| Test score bin | Samples | Mean selected score | Observed positive fraction |
| --- | --- | --- | --- |
| [0.0,0.1) | 6464 | 0.01083 | 0.00325 |
| [0.1,0.2) | 37 | 0.13899 | 0.08108 |
| [0.2,0.3) | 15 | 0.26688 | 0.20000 |
| [0.3,0.4) | 14 | 0.32895 | 0.35714 |
| [0.4,0.5) | 4 | 0.44421 | 0.75000 |
| [0.5,0.6) | 5 | 0.54345 | 0.80000 |
| [0.6,0.7) | 4 | 0.64125 | 0.50000 |
| [0.7,0.8) | 4 | 0.73628 | 0.75000 |
| [0.8,0.9) | 4 | 0.85458 | 0.50000 |
| [0.9,1.0] | 1 | 0.97530 | 1.00000 |

## Features, artifact and numerical parity

Inputs, in exact order:

```text
01 ego_speed_mps
02 ego_acceleration_mps2
03 ego_steering_rad
04 left_lane_driving
05 right_lane_driving
06 track_count
07 lead_present
08 lead_lon_m
09 lead_lat_m
10 lead_v_lon_mps
11 lead_v_lat_mps
12 lead_range_rate_mps
13 lead_age_s
14 lead_unseen_s
15 left_present
16 left_lon_m
17 left_lat_m
18 left_v_lon_mps
19 left_v_lat_mps
20 left_range_rate_mps
21 left_age_s
22 left_unseen_s
23 right_present
24 right_lon_m
25 right_lat_m
26 right_v_lon_mps
27 right_v_lat_mps
28 right_range_rate_mps
29 right_age_s
30 right_unseen_s
```

No derived pipeline TTC/conflict scores, rule decisions, future minima, simulator poses/types, controller intent, family, run ID or time is in that vector. Relative positions/velocities are ingredients of TTC, so a learned linear boundary can approximate existing kinematics. Large correlated velocity coefficients do not prove novel reasoning or causal importance. Steering/acceleration can reflect this simulated controller's response to a developing situation, limiting transfer to other controllers.

| Largest standardized coefficient | Value |
| --- | --- |
| lead_range_rate_mps | -1.515957 |
| lead_v_lon_mps | 1.326555 |
| left_lane_driving | 0.951481 |
| ego_speed_mps | -0.716996 |
| right_v_lon_mps | -0.712370 |
| right_lat_m | 0.601704 |
| right_present | -0.487831 |
| right_age_s | 0.418293 |

Selected inspectable JSON: **6131 bytes**, 30 coefficients plus intercept, median/scaler arrays, threshold, model/input versions, dataset/source checksums, pinned training configuration. No pickle was written or loaded. Training-time end-to-end scikit-learn pipeline parity over 27,457 train rows: maximum absolute score error 4.44e-16. Across all 43,233 rows, independent NumPy/scipy-expit reconstruction versus the portable predictor: 6.66e-16. The actual JSON artifact also predicted all rows successfully with numpy/scipy/sklearn/joblib imports explicitly blocked.

Mac batch-1 inference (10,000 calls, Python 3.12.14, macOS-27.2.1-arm64-arm-64bit): p50 **5.375 µs**, p95 **6.125 µs**, p99 **7.166 µs**. Includes input validation, missing-value substitution, scaling, dot product, sigmoid and result status; excludes feature extraction, I/O and application scheduling. This is not a Pi latency measurement.

A separate zero-fit serialization check restored scikit-learn LogisticRegression from the frozen learned coefficients on all 43,233 samples: maximum score error 6.66e-16 and zero threshold mismatches. See parity-validation.json.

## Reproduction and proposed Pi approach

Artifacts: `/Users/smash_mario/Library/Application Support/RoadSense/ml/phase7a-20261117`. source/ preserves the exact fitting/evaluation source and protocol; SHA256SUMS covers it and all model/evaluation JSON. The freeze checksum is unchanged after evaluation. Scikit-learn and numerical packages are isolated in ~/.local/share/roadsense/ml-python312; requirements-ml.txt is separate from Pi/web dependencies.

From the repository root, with the existing dataset and a new external output directory:

```sh
python3.12 -m pip install --only-binary=:all: --target "$HOME/.local/share/roadsense/ml-python312" -r requirements-ml.txt
export PYTHONPATH="$HOME/.local/share/roadsense/ml-python312:$PWD/src"
DATASET="$HOME/Library/Application Support/RoadSense/campaigns/phase6d-20261117/dataset-v1"
MODEL_OUT="$HOME/Library/Application Support/RoadSense/ml/phase7a-reproduction"
python3.12 tools/train_forward_risk.py fit "$DATASET" "$MODEL_OUT"
python3.12 tools/train_forward_risk.py evaluate "$DATASET" "$MODEL_OUT"
```

Future Pi inference can use only learned_risk.py, this ~6 KB JSON and Python standard library. Load once outside the safety loop; incompatible artifacts raise a load error, which a future observer adapter must catch and expose as unavailable. Current-input validation failures return invalid_input/insufficient_input with null score/warning, model version and shadow status. The predictor never supplies a braking or steering action. No live hook was added; shadow adapter design is documented in docs/learned-risk.md. Pi inference latency and live coexistence remain unmeasured.

## Recommendation and restrictions

**Expand independent dataset coverage before Phase 7B live/Pi integration.** The experiment demonstrates genuinely trained, tiny, fast numerical inference and some test predictive signal, but high misses, nuisance warnings, failed cut-ins and stress degradation prevent a deployment recommendation. Any next campaign needs separate approval: target independent positive cut-in/combined/mixed configurations and matched difficult negatives; reserve more positive groups in new evaluation sets prospectively, without moving the current frozen groups or lowering truth thresholds. Revisit event/false-alarm utility with a new preregistered experiment, not this held-out test. Do not train larger model families just to improve an impressive table.

Phase 6D is published. Phase 7A source/artifacts are local for review. 501 Python regressions, 65 JavaScript tests and 60 archived Arduino tests pass, including 22 portable-model/incident cases. No new simulation, Pi connection/restart/reboot/deployment, live-server modification, authoritative-decision change or dashboard redesign was performed. All safety claims remain limited to synthetic proxy observations and this existing controller.

Implementation references: [scikit-learn LogisticRegression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html), [SimpleImputer / preserving empty features](https://scikit-learn.org/stable/modules/impute.html#keeping-the-number-of-features-constant). Training statistics are fit only on the training partition/fold; these references describe the APIs, not validation of RoadSense performance.
