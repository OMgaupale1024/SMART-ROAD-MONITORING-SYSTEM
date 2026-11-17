import json
from pathlib import Path
import sys

import pytest

from roadsense.learned_risk import FEATURES, INPUT_SCHEMA, MODEL_SCHEMA, Predictor
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import forward_risk_metrics as metrics


def artifact():
    return {"schema": MODEL_SCHEMA, "input_schema": INPUT_SCHEMA, "features": list(FEATURES),
            "target": "forward_risk_3s", "model_version": "test-v1", "output": "uncalibrated_risk_score",
            "impute_median": [0.] * 30, "scale_mean": [0.] * 30, "scale_scale": [1.] * 30,
            "coefficients": [0.] * 30, "intercept": -2., "threshold": .2}


def features():
    return {n: 0. for n in FEATURES}


def test_portable_math_missingness_and_file_roundtrip(tmp_path):
    a = artifact()
    a["coefficients"][7], a["impute_median"][7], a["scale_scale"][7] = 1., 4., 2.
    row = features()
    row["lead_lon_m"] = None
    p = tmp_path / "model.json"
    p.write_text(json.dumps(a))
    result = Predictor.load(p).predict(row)
    assert result["status"] == "ok" and result["risk_score"] == pytest.approx(.5)
    assert result["warning"] and result["imputed_features"] == 1 and result["shadow"]
    assert result["probability_calibrated"] is False


@pytest.mark.parametrize("change", [
    {"schema": "unknown"}, {"input_schema": "unknown"}, {"features": list(reversed(FEATURES))},
    {"target": "collision"}, {"model_version": ""}, {"coefficients": [0.] * 29},
    {"scale_scale": [0.] * 30}, {"intercept": float("nan")}, {"threshold": 0},
    {"threshold": 1.1}, {"output": "calibrated_probability"}])
def test_incompatible_model_is_rejected(change):
    with pytest.raises(ValueError):
        Predictor(dict(artifact(), **change))


@pytest.mark.parametrize("value", [float("inf"), float("nan"), "20", True, []])
def test_invalid_inputs_return_status_without_score(value):
    row = features()
    row["ego_speed_mps"] = value
    r = Predictor(artifact()).predict(row)
    assert r["status"] == "invalid_input" and r["risk_score"] is None and r["warning"] is None


def test_schema_missing_keys_controller_intent_and_missing_ego():
    p = Predictor(artifact())
    assert p.predict(features(), "unknown")["status"] == "invalid_input"
    assert p.predict({})["status"] == "invalid_input"
    assert p.predict(dict(features(), future_min_ttc_s=0))["status"] == "invalid_input"
    assert p.predict(dict(features(), ego_lane_change=1))["status"] == "invalid_input"
    assert p.predict(dict(features(), ego_speed_mps=None))["status"] == "insufficient_input"
    assert p.predict(dict(features(), lead_present=None))["status"] == "invalid_input"


def test_sigmoid_extremes_and_bounded_json_loading(tmp_path):
    for z, score in ((1000., 1.), (-1000., 0.)):
        assert Predictor(dict(artifact(), intercept=z)).predict(features())["risk_score"] == score
    p = tmp_path / "huge.json"
    p.write_text(" " * 65537)
    with pytest.raises(ValueError):
        Predictor.load(p)
    p.write_text(json.dumps(dict(artifact(), intercept=float("nan"))))
    with pytest.raises(ValueError):
        Predictor.load(p)


def test_binary_metrics_and_undefined_denominators():
    r = metrics.binary([0, 0, 1, 1], [0, 1, 0, 1])
    assert r["confusion_matrix_tn_fp_fn_tp"] == [[1, 1], [1, 1]]
    assert r["precision"] == r["recall"] == r["f1"] == r["f2"] == .5
    assert metrics.binary([0], [0])["recall"] is None


def test_temporal_incidents_stop_at_first_proxy_and_merge_flicker():
    observations = [{"t": t, "forward_near_miss": True, "collision_proxy": proxy}
                    for t, proxy in [(1., False), (1.4, False), (2.8, True), (5., False)]]
    assert metrics.incidents(observations) == [{"onset_s": 1., "end_s": 1.4}, {"onset_s": 2.8, "end_s": 2.8}]


def row(t, target, developing=False):
    return {"run_id": "run", "group": "group", "family": "normal", "sim_s": t,
            "labels": {"forward_risk_3s": target}, "current_forward_risk": developing}


def test_alarm_exposure_and_pre_event_lead_are_distinct_from_developing_warning():
    rows = [row(t / 5, int(t >= 3)) for t in range(6)]
    events = {"run": [{"onset_s": 3.6, "end_s": 4.0}]}
    r = metrics.evaluate(rows, [True, True, False, False, True, True], events)
    assert r["false_alarm_episodes"] == 1 and r["warning_episodes"] == 2
    assert r["exposure_hours"] == pytest.approx(1.2 / 3600)
    assert r["pre_event_detection_rate"] == 1 and r["median_warning_lead_s"] == pytest.approx(2.8)
    assert r["during_event_detection_rate"] is None
    boundary = metrics.evaluate(rows, [False, False, False, True, False, False], events)
    assert boundary["median_warning_lead_s"] == pytest.approx(3.)
    rows += [row(3.6, 1, True), row(3.8, 1, True)]
    r = metrics.evaluate(rows, [False] * 6 + [True, True], events)
    assert r["pre_event_detection_rate"] == 0 and r["during_event_detection_rate"] == 1
    assert r["median_warning_lead_s"] is None
