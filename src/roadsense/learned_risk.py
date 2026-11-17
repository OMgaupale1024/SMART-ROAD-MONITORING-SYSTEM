"""Inspectable logistic shadow inference. Standard library only; never chooses a safety action."""
import json
import math
from pathlib import Path

INPUT_SCHEMA = "roadsense.forward-inputs.v1"
MODEL_SCHEMA = "roadsense.logistic.v1"
FEATURES = (
    "ego_speed_mps", "ego_acceleration_mps2", "ego_steering_rad", "left_lane_driving", "right_lane_driving", "track_count",
    "lead_present", "lead_lon_m", "lead_lat_m", "lead_v_lon_mps", "lead_v_lat_mps", "lead_range_rate_mps", "lead_age_s", "lead_unseen_s",
    "left_present", "left_lon_m", "left_lat_m", "left_v_lon_mps", "left_v_lat_mps", "left_range_rate_mps", "left_age_s", "left_unseen_s",
    "right_present", "right_lon_m", "right_lat_m", "right_v_lon_mps", "right_v_lat_mps", "right_range_rate_mps", "right_age_s", "right_unseen_s",
)


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("expected a finite number")
    return float(value)


class Predictor:
    def __init__(self, artifact):
        if not isinstance(artifact, dict) or artifact.get("schema") != MODEL_SCHEMA or artifact.get("input_schema") != INPUT_SCHEMA:
            raise ValueError("incompatible model schema")
        if artifact.get("features") != list(FEATURES) or artifact.get("target") != "forward_risk_3s":
            raise ValueError("incompatible feature order or target")
        self.version = artifact.get("model_version")
        if not isinstance(self.version, str) or not self.version or len(self.version) > 128:
            raise ValueError("invalid model version")
        if artifact.get("output") != "uncalibrated_risk_score":
            raise ValueError("unsupported output semantics")
        for name in ("impute_median", "scale_mean", "scale_scale", "coefficients"):
            values = artifact.get(name)
            if not isinstance(values, list) or len(values) != len(FEATURES):
                raise ValueError("invalid " + name)
            setattr(self, name, tuple(number(v) for v in values))
        if any(v <= 0 for v in self.scale_scale):
            raise ValueError("scales must be positive")
        self.intercept = number(artifact.get("intercept"))
        self.threshold = number(artifact.get("threshold"))
        if not 0 < self.threshold <= 1:
            raise ValueError("threshold out of range")

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as source:
            data = source.read(65537)
        if len(data) > 65536:
            raise ValueError("model artifact exceeds 64 KiB")
        def reject(value):
            raise ValueError("non-finite JSON value " + value)
        return cls(json.loads(data, parse_constant=reject))

    def predict(self, features, schema=INPUT_SCHEMA):
        result = {"model_version": self.version, "status": "invalid_input", "risk_score": None,
                  "warning": None, "shadow": True, "probability_calibrated": False}
        try:
            if schema != INPUT_SCHEMA or not isinstance(features, dict) or set(features) != set(FEATURES):
                raise ValueError("incompatible input schema or keys")
            if features["ego_speed_mps"] is None:
                result["status"] = "insufficient_input"
                return result
            values = [self.impute_median[i] if features[name] is None else number(features[name])
                      for i, name in enumerate(FEATURES)]
            for name in ("lead_present", "left_present", "right_present", "left_lane_driving", "right_lane_driving"):
                if features[name] not in (0, 1) or type(features[name]) is bool:
                    raise ValueError("invalid presence or road flag")
            z = self.intercept + math.fsum(w * ((v - mean) / scale)
                                          for v, w, mean, scale in zip(values, self.coefficients, self.scale_mean, self.scale_scale))
            if not math.isfinite(z):
                raise ValueError("non-finite logit")
            score = 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))
            result.update(status="ok", risk_score=score, warning=score >= self.threshold,
                          imputed_features=sum(features[n] is None for n in FEATURES))
        except (ValueError, TypeError, OverflowError):
            pass
        return result
