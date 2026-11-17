# Learned forward risk — Phase 7A

A logistic model has been trained on the frozen Phase 6D corpus. [Results and limitations](evidence/phase7a/RESULTS.md) recommend expanding independent coverage before live/Pi integration. This is an uncalibrated score for a synthetic forward near-miss proxy within three seconds. It is not a probability of physical collision or a braking controller.

Phase 6D is published at `7ae80d6`. Phase 7A files remain local for review. No new simulation campaign, live-server change or Pi action was performed.

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
