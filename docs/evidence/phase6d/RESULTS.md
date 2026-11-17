# Phase 6D dataset validation — 2026-11-17

These are newly executed local runs, distinct from Claude's Phase 6C pilot. No model was trained, no Pi action was taken, and the original Webots and development server were left running.

Campaign: `/Users/smash_mario/Library/Application Support/RoadSense/campaigns/phase6d-20261117`

Export: `/Users/smash_mario/Library/Application Support/RoadSense/campaigns/phase6d-20261117/dataset-v1`

| Measurement | Verified result |
| --- | --- |
| Attempted / successful runs | 70 / 70 |
| Configurations / groups | 63 / 61 |
| Seeds | 11, 12, 13, 14, 15, 16, 17, 18, 19 |
| Recorded / supervised samples | 52135 / 43233 |
| Positive / negative samples | 738 / 42495 (1.71% positive) |
| Exclusions | {"at_or_after_first_overlap_proxy": 6734, "incomplete_future_window": 1939, "unmatched_truth": 229} |
| Near-boundary negative samples | 1344 |
| Recording acceptance | {"accepted": 52205, "missed": 0, "rejected": 0} |
| Raw / usable physical truth frames | 52065 / 51906 |
| Forward near-miss episodes / overlap-proxy episodes | 50 / 25 |
| Forward near-miss / overlap-proxy frames | 277 / 92 |
| Tailgating episodes / frames | 68 / 1081 |
| Simulation / measured Webots wall seconds | 10427.0 / 1608.9 (6.48×) |
| Campaign size at report time | 389.3 MB |
| Child CPU time | 2509.6 s (156.0% of measured Webots wall time) |
| Peak child RSS | 1686.1 MB |

Near-boundary negatives have future minimum lead TTC in [1.5,3.0) s or gap in [2,8) m, while remaining negative under the unchanged target definition. This is a descriptive difficulty measure, not a balancing rule.


CPU/RSS are OS child-process accounting, with cumulative maximum RSS (a largest-child measure); they are not a simultaneous aggregate of every simulator process. Wall time excludes per-run startup/flush overhead.

Geometric pothole encounters: 43 distinct run/hazard pairs (intended boxes; not wheel impacts).

There are 37 forward-risk temporal episodes before or at the first overlap proxy in each run. The larger raw episode/frame totals above include later observations; they are not independent training accidents. Usable positives span 23 configuration groups, with 16 in train, 2 in validation, 2 in test and 3 in stress.

## Family distribution

| Family | Runs | Positive | Negative | Forward-risk episodes | Proxy episodes |
| --- | --- | --- | --- | --- | --- |
| combined | 10 | 240 | 5379 | 18 | 12 |
| cut_in | 10 | 61 | 6606 | 4 | 1 |
| mixed | 10 | 15 | 6664 | 1 | 1 |
| normal | 10 | 0 | 7108 | 0 | 0 |
| potholes | 10 | 15 | 6230 | 2 | 1 |
| sudden_brake | 10 | 358 | 4275 | 21 | 9 |
| tailgating | 10 | 49 | 6233 | 4 | 1 |

## Staged-event execution

Staged actions executed in 34/40 event runs; 33 released before termination.

Final event phases: `{'none': 30, 'done': 33, 'failed': 5, 'waiting': 1, 'acting': 1}`. A completed simulation is not necessarily a successfully staged event. Blocked/unreached events remain valid observed traffic runs; they do not count as executed interventions.


## Frozen grouped splits

| Split | Groups | Runs with samples | Positive groups | Positive runs | Positive samples | Negative samples |
| --- | --- | --- | --- | --- | --- | --- |
| train | 40 | 45 | 16 | 16 | 526 | 26931 |
| validation | 7 | 8 | 2 | 2 | 61 | 4779 |
| test | 7 | 10 | 2 | 2 | 47 | 6505 |
| stress | 7 | 7 | 3 | 3 | 104 | 4280 |

Assignments were frozen before simulation. Exact repetitions and near configurations share a group. No frame-level random splitting or outcome-based reassignment was used. Stress reserves unseen configurations, not an unseen road environment.

## Missing features and schema

The exact 30 inference keys and the audit of all 51 source features are in manifest.json. Missing track-slot fields are structural when no track exists; missing values remain null.

| Feature | Missing rate |
| --- | --- |
| lead_age_s | 38.855% |
| lead_lat_m | 38.855% |
| lead_lon_m | 38.855% |
| lead_range_rate_mps | 38.855% |
| lead_unseen_s | 38.855% |
| lead_v_lat_mps | 38.855% |
| lead_v_lon_mps | 38.855% |
| left_age_s | 10.709% |
| left_lat_m | 10.709% |
| left_lon_m | 10.709% |
| left_range_rate_mps | 10.709% |
| left_unseen_s | 10.709% |
| left_v_lat_mps | 10.709% |
| left_v_lon_mps | 10.709% |
| right_age_s | 76.567% |
| right_lat_m | 76.567% |
| right_lon_m | 76.567% |
| right_range_rate_mps | 76.567% |
| right_unseen_s | 76.567% |
| right_v_lat_mps | 76.567% |
| right_v_lon_mps | 76.567% |

Inference rows contain only the explicit observable feature whitelist; no simulator truth, future minima, risk indicators, rule decisions or controller target lane/manoeuvre are inside features. Labels, baseline and metadata remain separate. The current-risk field is evaluation-only privileged information.

## Reproduction

Use tools/scenario_dataset.py on the retained campaign and a new output directory. See docs/dataset.md for commands, target definitions, leakage audit, grouping policy and the Phase 7 plan. SHA256SUMS covers retained source data and export files; manifest.json records the sample checksum and the frozen plan checksum.

Sample SHA256: `18d58ee83e05e11164876b68feb0cebacba9755eb3395d6f0a8097cb2eb652ed`.

## Limits and training recommendation

These are 5 Hz synthetic observations from one highway layout and an existing controller. Physical-pose truth improves alignment; intended vehicle dimensions, kinematic near misses and oriented-box overlap are still proxies. Contact is not independently established. Motorcycles remain lane-structured and autorickshaws use car visuals. Runs can have variable physical outcomes under identical scenario inputs. Episode counts are temporal clusters within runs, not statistically independent crashes.

**Readiness: suitable for an initial Mac-only logistic-regression feasibility experiment after review. Insufficient for reliable model selection or broad generalization claims:** validation and test each have only two positive configuration groups. Keep RF/XGBoost comparison provisional until episode-level results and uncertainty justify it. No labels or groups were moved to improve this distribution. Do not tune on test or stress, train a final model, deploy to Pi, or launch a larger campaign without review.

## Verification and handoff

479 Python tests passed, including 49 scenario/handedness/dataset tests. The Phase 6C review also passed 65 JavaScript and 60 archived Arduino tests; UI code was unchanged. All 70 execution IDs, canonical parameter/configuration hashes, recorded source hashes and checksums were verified. SQLite quick_check passed. All seven original/repetition pairs have byte-identical world/network/route inputs. No unexpected missing selected feature was found when its track slot was present. A real resume invocation skipped all 70 completed tasks, and a second export reproduced samples.jsonl and manifest.json byte-for-byte. The original Webots and development-server PIDs remained 36894 and 36233. Vivaldi automation was not rerun; its earlier limitation remains. Phase 6B manual verification is documented, not newly performed here.

Phase 6C was published as f4d40477db5ab11f92b7f3ef48fa048007a4f1d2; Phase 6B was already published as 0b48c59. Phase 6D publication and a separate bounded Phase 7A training experiment were approved on 2026-11-17. No credentials, databases, sample rows or bulk simulation data are included in the Git working tree.
