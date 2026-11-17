# Learned forward risk — Phase 7A

A logistic model has been trained on the frozen Phase 6D corpus. [Results and limitations](evidence/phase7a/RESULTS.md) recommend expanding independent coverage before live/Pi integration. This is an uncalibrated score for a synthetic forward near-miss proxy within three seconds. It is not a probability of physical collision or a braking controller.

Phase 6D is published at `7ae80d6`; the reviewed Phase 7A experiment is published at `fac5857`. Its original artifact and negative findings are preserved. Phase 7A.2 source, compact evidence and the frozen `roadsense-7a2-hgb15-v1` artifact are published with its failed acceptance result; [its report](evidence/phase7a2/RESULTS.md) is checksummed and kept exactly as written at evaluation time, so its "remain local for review" sentence predates publication. The separate [Phase 7A.2 protocol](phase7a2-plan.md) governs the bounded expansion and candidate comparison; neither experiment is approved for live deployment.

## What is available

- `src/roadsense/learned_risk.py`: standard-library-only JSON loader and predictor; exact 30-feature schema/order, finite numerical parameters, positive scaler scales, bounded artifact size, explicit null imputation and status on invalid inputs.
- `requirements-ml.txt`: pinned Mac training dependencies, separate from web/Pi dependencies. No mandatory virtual environment.
- `tools/train_forward_risk.py`: `fit` performs only grouped training CV and final training, then freezes candidate/model checksums and thresholds; `evaluate` checks that freeze, computes the single held-out comparison and refuses to overwrite it.
- `docs/phase7a-plan.md`: protocol captured in the trained artifact's source snapshot before fitting. No held-out tuning occurred.
- External artifact directory: `~/Library/Application Support/RoadSense/ml/phase7a-20261117/`, containing `model.json`, both candidate JSONs, `freeze.json`, `training-cv.json`, `evaluation.json`, `SHA256SUMS`, and exact `source/` copies.

See the results report for reproduction commands, all confusion matrices, baseline comparisons, group uncertainty, incident timing and calibration. The selected threshold is 0.10232068373836772; it is a score threshold, not a claimed 10.2% collision probability. The selected JSON is 6,131 bytes. Full numerical and dataset provenance is inside it; the base source commit is Phase 6D and the then-uncommitted training sources are captured by hashes/copies.

## Standalone inference

```python
from roadsense.learned_risk import FEATURES, INPUT_SCHEMA, Predictor

predictor = Predictor.load("model.json")
# current_features has exactly the 30 named fields, with None for absent slot measurements.
result = predictor.predict(current_features, INPUT_SCHEMA)
# result: model_version, status, shadow=True, probability_calibrated=False,
# risk_score, warning, and imputed_features when successful.
```

Do not flatten a whole training row into inputs. `labels`, `baseline`, `current_forward_risk`, run/configuration/group IDs, timestamps, family and simulator truth are never inputs. A missing key/schema mismatch or non-finite value returns `invalid_input` and null score/warning; missing ego speed returns `insufficient_input`. Explicit null slot measurements use the training median. Presence flags must be valid current observations. Invalid model/schema/dimensions fail loading with ValueError; I/O failures propagate as OSError so a future adapter can expose model-unavailable status.

## Future shadow adapter design — not connected

1. Load the approved JSON once outside the authoritative safety loop. Verify its published checksum and model/input version. Catch load errors and retain an unavailable status while the existing engine continues.
2. Observe an already-published current state, after the deterministic decision is complete. Reuse the existing pure `history.features(state, None, 0, None)` extractor and select only `FEATURES`; the omitted recording metadata fields do not enter the model. This does not require creating a recorder or touching SQLite. Track ages, acceleration and unseen time must use only that frame's existing past/current information.
3. Call the standalone predictor in a separate observer/failure boundary. Retain model version/status and the state timestamp/sequence as output metadata so future clients can reject stale scores. Catch observer errors, log a diagnostic without changing deterministic decisions, and publish a separate unavailable learned status.
4. Keep the three product concepts distinct: authoritative rule recommendation, shadow learned risk score, and Ollama explanation. A learned warning is not a safety action and does not override the rule engine. No dashboard changes are implemented now.
5. Before any later Pi deployment, separately approve/check ARM64 numerical parity, model validation failure isolation, model-load memory and batch-1 inference latency alongside the current pipeline. The Pi needs this module and JSON only; numpy/scipy/scikit-learn belong exclusively to Mac training. The current Mac latency does not establish Pi performance.

No server hook, service, thread pool, endpoint, database schema or dashboard integration was added. The existing deterministic safety and Ollama code are unchanged.

## Phase 7A.2 causal contract and portable trees

`src/roadsense/expanded_risk.py` preserves all 30 v1 inputs in order and appends 14 causal inputs under `roadsense.forward-inputs.v2`. They cover measured lead TTC, nominal side corridor-crossing time, steering rate, lead range/closing-speed/absolute-speed trends, side range/lateral trends and slot continuity. The slot continuity measure checks observed age and motion; it is not a simulator identity. A one-second buffer resets on run/sequence/time discontinuities, absent/stale tracks, age resets or implausible slot jumps. Derivatives need at least 0.4 seconds of continuous observations. Nulls remain explicit and use training-only medians in the model.

The TTC and corridor inputs are ordinary current kinematic calculations, so improvement cannot be attributed to learning them independently. The corridor uses a fixed nominal 1.85 m half-width sum rather than privileged vehicle dimensions. Version 2 does not consume scenario IDs, simulator poses, intended maneuvers, risk labels or future minima. The export processes every raw observed frame before applying the supervised eligibility mask; missing future truth never changes the past feature buffer.

```python
from roadsense.expanded_risk import TemporalFeatures, Predictor

stream = TemporalFeatures()
predictor = Predictor.load("phase7a2-model.json")
# base_features contains the same current 30 observable fields as Phase 7A.
current = stream.observe(base_features, sim_s, run_id, sequence)
result = predictor.predict(current, "roadsense.forward-inputs.v2")
```

Run ID, sequence and timestamp reset the feature buffer and are excluded from numerical model inputs. Call `observe` on every frame, including frames that would lack an offline training label. A future observer must keep the same timing/units and isolate failures as described above. The current stream assumes the recorded 5 Hz contract; a different rate needs a separately validated feature version.

Versioned JSON supports logistic regression, numerical random forests and numerical histogram boosting. Loading validates schema, dimensions, finite parameters and acyclic trees; inference uses only Python's standard library. Forest input rounding reproduces scikit-learn's float32 behavior; boosted trees use the exported numerical thresholds, weighted leaves and intercept. No pickle, training stack, server adapter or Pi deployment is required or performed. Scores remain uncalibrated on this enriched synthetic corpus. The [completed Phase 7A.2 report](evidence/phase7a2/RESULTS.md) records 140 attempts and the validation-selected boosted model's failed predictive acceptance; it is suitable only for a labeled offline demonstration, pending further approved research.
