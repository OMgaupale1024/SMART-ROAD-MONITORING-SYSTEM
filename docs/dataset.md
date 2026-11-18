# Phase 6D: forward-risk dataset

The first task is **elevated forward near-miss risk in the next 3.0 seconds**, from sensor-derived observables at the current timestamp. RoadSense remains advisory. These labels describe outcomes of Webots' sample ego controller, not accident prevention by RoadSense. No model training or Pi changes are part of Phase 6D.

## Definitions (roadsense.forward-risk.v1)

Truth uses physical Webots traffic poses at the supervisor's current simulation time, sampled every 0.2 seconds. SUMO logical poses remain separately recorded for audit. Webots smooths lane changes over several seconds, so logical positions alone are unsuitable for radar-aligned labels. Traffic origins are converted from the Webots centre to a front bumper using intended SUMO length. Intended dimensions can differ from visual models (especially autorickshaws); all footprint outcomes remain proxies.

| Measurement | Definition and units |
| --- | --- |
| Ego box | Rear axle origin; longitudinal extent −1.0 to +3.9 m; lateral ±0.95 m |
| Vehicle box | Intended SUMO length/width, rotated by the physical Webots yaw |
| `collision_proxy` | Oriented boxes overlap on all separating axes; includes touching boundaries; **not measured physical contact** |
| `forward_overlap_proxy` | Overlap with other box's longitudinal centre at or ahead of ego box centre (+1.45 m) |
| Lead/follower | Nearest box ahead/behind whose projected lateral interval intersects ego width; longitudinal gap in metres; crossing/angled vehicles use a conservative projected extent |
| Lead TTC | Gap / closing longitudinal speed, seconds; null if closing speed ≤0.1 m/s; heading-projected physical velocity; forward overlap has zero proxy clearance/TTC |
| Headway | Gap / following vehicle speed, seconds; null if following speed ≤2 m/s |
| Unsafe headway / tailgated | Lead / follower headway strictly <1.0 s; separate front/rear observations |
| Forward near miss | Forward overlap proxy, or (ego speed >2 m/s and (lead gap <2.0 m or lead TTC <1.5 s)) |
| `forward_risk_3s` | Any forward near-miss observation in **(t, t+3.0 s]**: 15 future observations, all required |
| Continuous outcomes | Minimum lead gap (m) and TTC (s) within the same future window; null if no applicable observed lead; never imputed as zero |
| Independent episode | A temporal cluster of positive observations within one execution, with more than 1.0 s between positive observations starting a new cluster. Temporal episodes, not claims of statistically independent accidents |
| Hazard encounter (v1 descriptive) | Intended pothole extent overlaps ego box; ignores wheel path/yaw and is not an impact label. Preserved in run outcomes; excluded from forward target |

A 3 s horizon gives 15 observations at 5 Hz and supports warning-lead-time evaluation. The pilot has long safe sections and sparse severe observations; 3 s creates a useful warning interval without changing near-miss thresholds for balancing. Future windows include already-developing situations; Phase 7 must separately evaluate pre-onset warnings and persistence. Near miss is a kinematic risk proxy, not proof that contact would have occurred. Actual contact is not established.

Webots exposes [Supervisor contact points](https://github.com/cyberbotics/webots/blob/master/docs/reference/supervisor.md), including descendant solids, but ground/wheel contacts also occur during ordinary driving. Reliable vehicle-specific contact attribution requires additional validation. SUMO collision events concern its logical traffic/mirrored ego and are not evidence of Webots physical contact. This phase retains explicitly named proxies and continuous measurements instead of claiming a physical collision label.

## Alignment and recording quality

Join within one execution on a 0.2 s timestamp grid, with at most 0.002 s floating tolerance; loose decimal rounding is rejected. Duplicate sample or truth timestamps are errors. The producer sample at 0.2 s has no supervisor truth (first frame at 0.4 s) and is excluded. Truth frames missing a physical vehicle are unusable. Every one of the 15 future frames must be valid; run-end windows and windows crossing truth holes are censored. No interpolation or invented ground truth. Samples at or after the first oriented-box overlap proxy are also excluded from training, to avoid learning from a potentially damaged or off-road controller state. Earlier future windows may still include that observed outcome; this conservative filter does not claim the proxy establishes contact.

Runs with recording losses, rejected/missed sensor frames, sequence gaps, simulator errors or incomplete termination are excluded. A new server/processor and UUID execution identity are used for every run. All runs share the campaign history database but join by run identity, never local hazard or vehicle IDs alone. The original world, current Webots session, port 8000 server and live Pi are untouched.

## Feature leakage audit

The export manifest classifies **all 51 frozen v1 numerical features** and lists the selected inference inputs. No future or simulator-only observations are added to `features`.

- Current prototype inference observables: ego speed, heading, steering; physical road geometry; confirmed-track count and relative track positions/velocities/range rates; mapped hazard observations. This is a simulation research prototype: camera recognition supplies privileged semantic hazard detections in the existing pipeline, and road geometry is configured. Neither establishes real-world sensor availability.
- Historical inference observables: ego acceleration from prior frames, track ages and time since last observation. These use only information available by t.
- Controller metadata: `ego_lane` is a **target lane index**, not measured physical lane. Excluded. `ego_lane_change` is the producer's current manoeuvre state, also controller metadata rather than an independent sensor observation; excluded.
- Heuristic safety outputs: `derived_min_ttc_s`, `derived_conflicts`; excluded. All five heuristic rule labels remain under `baseline`, outside inputs and independent labels.
- Recording metadata: sequence gaps, storage drops and `sim_dt_s`; excluded from inputs, used for quality filtering.
- Rear-track and road-hazard features are not selected for the first forward task. Absolute world heading and target lane index are also excluded. Selected features are ego speed/acceleration/steering, road lane availability, track count and lead/left/right track slots (30 inputs).
- Simulator truth, object types/identities, execution metadata, current risk and future outcomes are outside `features`. The training loader must explicitly read the manifest's input list and target key, not flatten entire rows.

The generated `feature_audit` is the exact per-column reference, including units. No future minima, collision flags, privileged simulator poses or deterministic action outputs are inference inputs. No GPS coordinates are generated.

## Campaign, grouping and freeze

`tools/scenario_campaign.py` uses the existing generator without changing scenario thresholds. There are 63 configurations: seven families × seeds 11–19, plus one repetition of each seed-11 family = **70 total attempted executions maximum**. The first seven form a smoke campaign. Random seeds vary density, speed limits, traffic mix, braking gap/intensity/timing, cut-in gap/timing and hazards using the existing distributions. RoadSense does not drive the ego, so parameter ranges do not guarantee dangerous outcomes.

The immutable `plan.json` is written before simulation and contains full parameters, configuration IDs, run tasks and split assignments. Configuration IDs hash canonical scenario parameters, excluding execution metadata. Executions receive UUID-suffixed run IDs and repetition numbers. Every scenario records simulator versions, handedness, feature/truth versions, source revision and source-file hashes. Scenario headers and end records preserve simulation times, termination and event outcomes. Campaign records preserve quality/loss counters and available child CPU/RSS statistics.

Exact repeats and near configurations share a group through transitive closure. Near means same family/profile, speed limit within 10 km/h, density within 3 vehicles/km/lane, mix L1 difference ≤0.25, equal pothole count with matched lane/severity and locations within 300 m, and matching event kind/side with timing within 300 m, gap within 10 m, deceleration within 2 m/s², target speed within 10 km/h, hold within 2 s, cut-in speed delta within 5 km/h, tailgap within 0.2 s and duration within 5 s. This explicit conservative policy is checked for every pair before splitting. Scenario families with qualitatively different event structures remain separate; correlations beyond this definition remain a limitation.

Groups are ordered by a fixed SHA256 salt within each family. First group is a reserved unseen-configuration stress set, second test, third validation, remainder train. Assignments depend only on parameters, never outcomes. All repetitions remain together. The test and stress sets are frozen before outcomes are inspected; report distribution shortfalls instead of moving positives across splits. Initial plan: 40 train, 7 validation, 7 test, 7 stress groups (61 groups, 63 configurations).

Storage limit: 2 GB campaign budget, 50 MB headroom before each run and ≥1 GB free disk; per-run Webots timeout 600 s. Runs are serial; ports 8011 and 8901 must be free. A file lock prevents duplicate campaign runners. Completed tasks are skipped on resume; interrupted directories are preserved and counted as failed attempts. Failed runs pause the campaign for inspection. No automatic retry campaign beyond the 70-attempt budget. No Ollama, paid service, external dataset or Pi workload.

## Commands and files

Plain Python with existing dependencies is supported:

```sh
export PYTHONPATH="$HOME/.local/share/roadsense/python312:$PWD/src"
python3.12 tools/scenario_campaign.py --campaign phase6d-20261117 --smoke
# After inspecting smoke export/quality:
python3.12 tools/scenario_campaign.py --campaign phase6d-20261117
python3.12 tools/scenario_dataset.py \
  "$HOME/Library/Application Support/RoadSense/campaigns/phase6d-20261117" \
  "$HOME/Library/Application Support/RoadSense/campaigns/phase6d-20261117/dataset-v1"
```

Exports are reproducible from retained raw data and refuse to overwrite an existing destination; choose a new directory to verify a rerun. `samples.jsonl` separates `features`, independent future `labels`, heuristic `baseline` and metadata. `manifest.json` contains the schema, input audit, counts, per-run/family/split results and sample SHA256. `SHA256SUMS` covers raw inputs/truth, campaign DB, plan and export. Campaign data lives outside Git. The operational history exporter remains available for heuristic datasets and is unchanged; it is not the supervised Phase 6D export.

## Phase 7 plan (requires review)

First validate the frozen export checksums, read only `features` listed in the manifest and predict `labels.forward_risk_3s`. Fit missing-value handling on train only; retain presence flags. Missing values for absent track slots are structural, not silently zero-filled. Any temporal window uses only earlier samples of the same uninterrupted run. Fit scaling, imputation and class weighting inside the train pipeline. Do not resplit frames or reassign groups after inspecting test outcomes.

Start with logistic regression as the minimum baseline, then Random Forest. Try gradient boosting only if positive independent episodes/configurations and validation results justify it. Do not oversample before splitting; measure class and episode coverage first. Tune thresholds and optional class weights on validation. Evaluate the deterministic rule outputs on the identical timestamps as a separate baseline, including a forward-only rule definition fixed before test evaluation.

Report dangerous-episode recall/miss rate, frame recall/false-positive rate, precision-recall AUC, false alerts per simulated driving hour, pre-onset warning lead time and calibration (Brier score/reliability bins). Give per-family and held-out configuration results plus uncertainty grouped by execution/configuration, because adjacent frames are correlated. Keep the final test unopened during tuning; use the separate stress export for unseen configurations. Do not claim unseen-family generalization from this family-stratified campaign.

Measure serialized size and batch-1 latency on Mac first. A later explicitly approved Pi benchmark must measure ARM64 latency/memory and shadow-mode behavior beside the rule engine. The trained model must remain outside the authoritative safety path; Ollama remains explanation-only. Future dashboard fields should distinguish rule advice, learned risk and AI explanation. No training or deployment has been performed in Phase 6D.

## Validated campaign and review gate

[The completed report](evidence/phase6d/RESULTS.md) covers all 70 successful executions: 52,135 recorded and 43,233 supervised samples, including 738 positives. The dataset has 23 positive configuration groups, but validation and test each contain only two. An initial logistic-regression feasibility experiment is reasonable after review; reliable model selection remains underpowered. The sample file is 50,660,722 bytes. The retained campaign including the byte-identical verification export is about 389 MB.

The smoke export is a developmental artifact with 31 inputs and earlier filtering; use **dataset-v1**, with 30 inputs and the final audit/censoring rules, for any later training. Exact recording and export source snapshots are retained under the campaign's source/ directory. All recorded source hashes were verified against those snapshots.

Phase 6D publication and a bounded Phase 7A logistic-regression experiment were approved on 2026-11-17. This report describes Phase 6D; later training is documented separately. Bulk campaign data remains outside Git.

The approved Phase 7A experiment is complete: see [learned-risk.md](learned-risk.md) and [the evaluation report](evidence/phase7a/RESULTS.md). No additional simulation or Pi deployment was performed.

## Phase 7A.2 prospective expansion

The separate [frozen protocol](phase7a2-plan.md) authorizes 140 additional attempts, including six smoke attempts. `tools/scenario_expansion.py` reuses the existing generator with explicit type/mode/timing controls and isolated local ports. Its immutable plan allocates 80 train, 20 validation, 20 test, 10 stress and 10 ordinary-cohort configurations. Every new configuration is non-near to the old campaign and every other new configuration under a conservative extension of the grouping rule above. No split is reassigned after outcomes.

The unchanged `roadsense.forward-risk.v1` target and quality mask are exported by `tools/scenario_dataset.py`. `tools/expand_features.py` appends the causal 14-field contract described in [learned-risk.md](learned-risk.md), processing all raw observations before filtering supervised rows. The original 30-feature export remains available. Model selection uses original train plus new train, and new validation only; original holdouts remain historical benchmarks. Fresh test/stress/ordinary collection requires a frozen model and threshold bound to the plan's checksum.

The bounded campaign, comparison, artifact locations, checksum audit and collection shortfalls are documented in [the Phase 7A.2 results](evidence/phase7a2/RESULTS.md). Bulk recordings and derived datasets remain outside Git. There is no authorization for another automatic campaign or Pi deployment.
