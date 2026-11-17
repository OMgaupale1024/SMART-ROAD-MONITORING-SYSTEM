"""Causal v2 feature stream for offline experiments and a future shadow observer; no safety actions."""
from collections import deque
import math
from .learned_risk import FEATURES as BASE_FEATURES, number

INPUT_SCHEMA = "roadsense.forward-inputs.v2"
EXTRA_FEATURES = (
    "sensor_lead_ttc_s", "sensor_left_corridor_time_s", "sensor_right_corridor_time_s", "ego_steering_rate_radps",
    "lead_lon_trend_mps", "lead_closing_accel_mps2", "lead_speed_accel_mps2", "lead_continuity_s",
    "left_lat_trend_mps", "left_lon_trend_mps", "left_continuity_s",
    "right_lat_trend_mps", "right_lon_trend_mps", "right_continuity_s",
)
FEATURES = BASE_FEATURES + EXTRA_FEATURES


class TemporalFeatures:
    """One second of causal observations. Continuity is an age/motion consistency proxy, not object truth."""
    def __init__(self):
        self.last = None
        self.history = {s: deque(maxlen=6) for s in ("lead", "left", "right")}

    def observe(self, features, t, run_id, sequence):
        if set(features) != set(BASE_FEATURES) or not isinstance(run_id, str) or not run_id or type(sequence) is not int:
            raise ValueError("incompatible feature stream")
        number(t)
        for v in features.values():
            if v is not None:
                number(v)
        out = dict(features)
        out.update({n: None for n in EXTRA_FEATURES})
        prior = self.last
        contiguous = prior is not None and prior[2] == run_id and sequence == prior[3] + 1 and abs(t - prior[0] - .2) <= .002
        if not contiguous:
            for h in self.history.values():
                h.clear()
        if contiguous and features["ego_steering_rad"] is not None and prior[1]["ego_steering_rad"] is not None:
            out["ego_steering_rate_radps"] = (features["ego_steering_rad"] - prior[1]["ego_steering_rad"]) / (t - prior[0])
        for slot, h in self.history.items():
            out[slot + "_continuity_s"] = 0.
            current = [features[slot + "_" + key] for key in ("lon_m", "lat_m", "v_lon_mps", "age_s", "unseen_s")]
            if not features[slot + "_present"] or any(v is None for v in current) or current[-1] > .21:
                h.clear()
                continue
            lon, lat, velocity, age, _ = current
            if h:
                pt, pf = h[-1]
                dt = t - pt
                previous_age, previous_lon, previous_velocity = [pf[slot + "_" + key] for key in ("age_s", "lon_m", "v_lon_mps")]
                if abs(age - previous_age - dt) > .05 or abs(lon - previous_lon - previous_velocity * dt) > 3.:
                    h.clear()
            h.append((t, dict(features)))
            elapsed = t - h[0][0]
            out[slot + "_continuity_s"] = round(elapsed, 6)
            if elapsed >= .399:
                old = h[0][1]
                out[slot + "_lon_trend_mps"] = (lon - old[slot + "_lon_m"]) / elapsed
                if slot == "lead":
                    out["lead_closing_accel_mps2"] = -(velocity - old["lead_v_lon_mps"]) / elapsed
                    if features["ego_speed_mps"] is not None and old["ego_speed_mps"] is not None:
                        out["lead_speed_accel_mps2"] = (features["ego_speed_mps"] + velocity - old["ego_speed_mps"] - old["lead_v_lon_mps"]) / elapsed
                else:
                    out[slot + "_lat_trend_mps"] = (lat - old[slot + "_lat_m"]) / elapsed
        if features["lead_present"] and features["lead_lon_m"] is not None and features["lead_v_lon_mps"] is not None and features["lead_v_lon_mps"] < -.1:
            out["sensor_lead_ttc_s"] = max(0., features["lead_lon_m"] - 3.9) / -features["lead_v_lon_mps"]
        for side, sign in (("left", 1), ("right", -1)):
            lat, velocity = features[side + "_lat_m"], features[side + "_v_lat_mps"]
            if features[side + "_present"] and lat is not None and velocity is not None and sign * velocity < -.1:
                out["sensor_" + side + "_corridor_time_s"] = max(0., sign * lat - 1.85) / (-sign * velocity)
        self.last = (t, dict(features), run_id, sequence)
        return {name: out[name] for name in FEATURES}


MODEL_SCHEMA = "roadsense.expanded-risk.v1"


class Predictor:
    """Portable logistic/forest/boosted numerical trees, fitted offline and always shadow-only."""
    def __init__(self, artifact):
        if not isinstance(artifact,dict):raise ValueError('model must be an object')
        if artifact.get('schema') != MODEL_SCHEMA or artifact.get('target') != 'forward_risk_3s' or artifact.get('horizon_s') != 3:
            raise ValueError('incompatible expanded model')
        if not isinstance(artifact.get('features'),list):raise ValueError('feature names must be a list')
        self.names = tuple(artifact['features'])
        self.input_schema = artifact.get('input_schema')
        if (self.names,self.input_schema) not in ((BASE_FEATURES,'roadsense.forward-inputs.v1'),(FEATURES,INPUT_SCHEMA)):
            raise ValueError('incompatible feature contract')
        self.version = artifact.get('model_version')
        if not isinstance(self.version,str) or not self.version or len(self.version)>128 or artifact.get('output')!='uncalibrated_risk_score':
            raise ValueError('invalid model metadata')
        self.kind = artifact.get('algorithm')
        if self.kind not in ('logistic','random_forest','hist_gradient_boosting'):
            raise ValueError('unsupported model algorithm')
        self.threshold = number(artifact.get('threshold'))
        if not 0<self.threshold<=1:raise ValueError('invalid threshold')
        def vector(key):
            values=artifact.get(key)
            if not isinstance(values,list) or len(values)!=len(self.names):raise ValueError('invalid '+key)
            return tuple(number(v) for v in values)
        self.medians=vector('impute_median')
        self.base=number(artifact.get('intercept',0.))
        self.trees=[]
        if self.kind=='logistic':
            self.coefficients=vector('coefficients');self.mean=vector('scale_mean');self.scale=vector('scale_scale')
            if any(v<=0 for v in self.scale):raise ValueError('invalid scaling')
        else:
            trees=artifact.get('trees')
            if not isinstance(trees,list) or not 1<=len(trees)<=256:raise ValueError('invalid trees')
            nodes=0
            for tree in trees:
                if not isinstance(tree,dict) or any(not isinstance(tree.get(k),list) for k in ('feature','left','right','threshold','value')):raise ValueError('invalid tree arrays')
                size=len(tree.get('feature',[]));nodes+=size
                if not 1<=size<=20000 or nodes>50000 or any(len(tree.get(k,[]))!=size for k in ('left','right','threshold','value')):
                    raise ValueError('invalid tree dimensions')
                for i,(feature,left,right,threshold,value) in enumerate(zip(*(tree[k] for k in ('feature','left','right','threshold','value')))):
                    number(threshold);number(value)
                    if any(type(v) is not int for v in (feature,left,right)):raise ValueError('invalid node indices')
                    if feature==-1:
                        if left!=-1 or right!=-1:raise ValueError('invalid leaf')
                        if self.kind=='random_forest' and not 0<=value<=1:raise ValueError('invalid forest probability')
                    elif not (0<=feature<len(self.names) and i<left<size and i<right<size):raise ValueError('invalid/cyclic tree')
                self.trees.append({k:tuple(tree[k]) for k in ('feature','left','right','threshold','value')})

    @classmethod
    def load(cls,path):
        import json
        with open(path,'rb') as source:data=source.read(8*1024*1024+1)
        if len(data)>8*1024*1024:raise ValueError('model exceeds loader bound')
        def reject(x):raise ValueError('invalid JSON number '+x)
        return cls(json.loads(data,parse_constant=reject))

    def predict(self,features,schema=None):
        result={'status':'invalid_input','model_version':self.version,'risk_score':None,'warning':None,'shadow':True,'probability_calibrated':False}
        try:
            if (schema is not None and schema!=self.input_schema) or not isinstance(features,dict) or set(features)!=set(self.names):
                raise ValueError('incompatible input')
            if features['ego_speed_mps'] is None:
                result['status']='insufficient_input';return result
            for name in ('lead_present','left_present','right_present','left_lane_driving','right_lane_driving'):
                if type(features[name]) is bool or features[name] not in (0,1):raise ValueError('invalid flag')
            values=[self.medians[i] if features[n] is None else number(features[n]) for i,n in enumerate(self.names)]
            if self.kind=='logistic':
                z=self.base+math.fsum(c*(v-m)/s for v,m,s,c in zip(values,self.mean,self.scale,self.coefficients))
            else:
                if self.kind=='random_forest':
                    # sklearn's forest casts its numeric input to float32 before testing double thresholds.
                    import struct
                    values=[struct.unpack('f',struct.pack('f',v))[0] for v in values]
                leaves=[]
                for tree in self.trees:
                    i=0
                    for depth in range(33):
                        if tree['feature'][i]==-1:
                            leaves.append(tree['value'][i]);break
                        i=tree['left'][i] if values[tree['feature'][i]]<=tree['threshold'][i] else tree['right'][i]
                    else:raise ValueError('tree depth exceeds bound')
                if self.kind=='random_forest':
                    score=math.fsum(leaves)/len(leaves)
                else:z=self.base+math.fsum(leaves)
            if self.kind!='random_forest':
                if not math.isfinite(z):raise ValueError('invalid logit')
                score=1/(1+math.exp(-z)) if z>=0 else math.exp(z)/(1+math.exp(z))
            if not math.isfinite(score):raise ValueError('invalid score')
            result.update(status='ok',risk_score=score,warning=score>=self.threshold)
        except (ValueError,TypeError,OverflowError):pass
        return result
