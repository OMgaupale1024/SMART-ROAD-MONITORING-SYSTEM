import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from roadsense.learned_risk import FEATURES as BASE
from roadsense.expanded_risk import FEATURES,TemporalFeatures

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import scenario_expansion as expansion
import scenarios as sc
from scenario_plugin import SumoSupervisorPlugin,EGO


def observation(i):
    f={k:0. for k in BASE}
    for side in ('lead','left','right'):
        f.update({side+'_present':1,side+'_lon_m':30-i,side+'_lat_m':3.-i*.1,
                  side+'_v_lon_mps':-5.,side+'_v_lat_mps':-.5,side+'_age_s':2+i*.2,side+'_unseen_s':0.})
    f['ego_speed_mps']=20.
    return f


def test_causal_trends_order_and_future_prefix_invariance():
    a=TemporalFeatures(); rows=[a.observe(observation(i),i*.2,'run',i+1) for i in range(7)]
    assert tuple(rows[0])==FEATURES and FEATURES[:30]==BASE and len(FEATURES)==44
    assert rows[0]['lead_lon_trend_mps'] is None
    assert rows[5]['lead_lon_trend_mps']==pytest.approx(-5)
    assert rows[5]['left_lat_trend_mps']==pytest.approx(-.5)
    saved=dict(rows[5]);a.observe(observation(100),1.4,'run',8)
    assert rows[5]==saved
    assert not any(k in rows[5] for k in ('run_id','sequence','timestamp','future_ttc'))


@pytest.mark.parametrize('change',[{'run':'new'},{'sequence':20},{'time':2.},{'age':0.},{'present':0},{'unseen':.6}])
def test_temporal_state_resets(change):
    a=TemporalFeatures()
    for i in range(5):a.observe(observation(i),i*.2,'run',i+1)
    f=observation(5)
    for key,suffix in [('age','age_s'),('present','present'),('unseen','unseen_s')]:
        if key in change:f['lead_'+suffix]=change[key]
    r=a.observe(f,change.get('time',1.),change.get('run','run'),change.get('sequence',6))
    assert r['lead_lon_trend_mps'] is None and r['lead_continuity_s']==0


def test_prospective_plan_is_bounded_disjoint_and_deterministic():
    old=[sc.draw(f,1) for f in sc.FAMILIES];p=expansion.build_plan(old)
    assert p==expansion.build_plan(old) and len(p['configurations'])==140
    assert p['allocations']=={'train':80,'validation':20,'test':20,'stress':10,'ordinary':10}
    for i,a in enumerate(p['configurations']):
        assert not any(expansion.related(a,b) for b in old+p['configurations'][:i])
    assert {p['variant'] for p in p['configurations']}==set(expansion.VARIANTS)


@pytest.mark.parametrize('vtype,length',[('motorcycle',2.),('autorickshaw',2.8),('car',4.5)])
def test_event_spawn_uses_intended_vehicle_length(vtype,length):
    assert sc.vehicle_length(vtype)==length
    p=SumoSupervisorPlugin.__new__(SumoSupervisorPlugin)
    p.profile=sc.PROFILES['highway_lht'];p.p={'traffic':'left'};p.traci=SimpleNamespace(vehicle=SimpleNamespace())
    p.event={'phase':'waiting','kind':'cut_in','at_m':0,'gap_m':4.,'side':'left','speed_delta_kmh':-10,'vehicle_type':vtype}
    calls=[]
    def spawn(*args):calls.append(args);p.event['phase']='spawned';return True
    p._spawn=spawn;p._log_event=lambda *args,**kwargs:None
    p._step_event(1.,{'x':-4800.,'y':7.03,'speed':20.,'travelled_m':160},[EGO])
    assert calls[0][1]==vtype and calls[0][3]==pytest.approx(203.9+4+length)


@pytest.mark.parametrize('mode',[0,512])
def test_cutin_mode_is_explicit_and_default_is_safe(mode):
    calls=[]
    v=SimpleNamespace(getIDList=lambda:['car'],setSpeedMode=lambda *a:None,
                      setLaneChangeMode=lambda *a:calls.append(a),changeLane=lambda *a:None,
                      getSpeed=lambda _:10,getLaneIndex=lambda _:2)
    p=SumoSupervisorPlugin.__new__(SumoSupervisorPlugin);p.profile=sc.PROFILES['highway_lht'];p.p={'traffic':'left'};p.traci=SimpleNamespace(vehicle=v)
    p.event={'kind':'cut_in','phase':'settling','vehicle':'car','since':0,'to_lane':2,'lane_change_mode':mode}
    if mode==512:p.event.pop('lane_change_mode')
    p._log_event=lambda *a,**k:None
    p._step_event(2,{'x':-4800.,'y':7.03,'speed':20.},[EGO])
    assert calls==[('car',mode)]


@pytest.mark.parametrize('value,expected',[('80',80),('40',40),('nan',None),('91',None)])
def test_experiment_ego_speed_boundaries(value,expected):
    path=Path(__file__).resolve().parents[1]/'simulation/webots/controllers/roadsense_ego/roadsense_ego.py'
    tree=ast.parse(path.read_text());index=next(i for i,n in enumerate(tree.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='maxSpeed' for t in n.targets))
    code=compile(ast.Module(body=tree.body[index:index+2],type_ignores=[]),str(path),'exec')
    env={'os':SimpleNamespace(environ={'ROADSENSE_EGO_SPEED_KMH':value})}
    if expected is None:
        with pytest.raises(ValueError):exec(code,env)
    else:exec(code,env);assert env['maxSpeed']==expected


def test_autorickshaw_insertion_respects_type_speed():
    calls=[]
    v=SimpleNamespace(add=lambda *a,**kw:calls.append(kw),moveToXY=lambda *a,**kw:None,setSpeed=lambda *a:None)
    p=SumoSupervisorPlugin.__new__(SumoSupervisorPlugin);p.profile=sc.PROFILES['highway_lht'];p.traci=SimpleNamespace(vehicle=v);p.event={}
    assert p._spawn('lead','autorickshaw',2,100,22.,1.,[EGO])
    assert float(calls[0]['departSpeed'])==pytest.approx(13.9)
    assert p.event['start_speed']==pytest.approx(13.9)


def tree_artifact(kind='random_forest'):
    from roadsense.expanded_risk import MODEL_SCHEMA,INPUT_SCHEMA
    return {'schema':MODEL_SCHEMA,'input_schema':INPUT_SCHEMA,'features':list(FEATURES),'target':'forward_risk_3s','horizon_s':3,
            'model_version':'tree-test-v1','output':'uncalibrated_risk_score','algorithm':kind,'threshold':.5,
            'impute_median':[0.]*44,'intercept':0.,'trees':[{'feature':[0,-1,-1],'left':[1,-1,-1],'right':[2,-1,-1],
            'threshold':[10.,0.,0.],'value':[0.,.1,.9]}]}


def test_portable_trees_and_missing_inputs():
    from roadsense.expanded_risk import Predictor
    f={n:0. for n in FEATURES};f['ego_speed_mps']=20.
    p=Predictor(tree_artifact());assert p.predict(f)['risk_score']==pytest.approx(.9)
    f['ego_speed_mps']=5.;assert not p.predict(f)['warning']
    assert p.predict(dict(f,ego_speed_mps=None))['status']=='insufficient_input'
    assert p.predict(dict(f,future_ttc=0))['status']=='invalid_input'
    h=Predictor(tree_artifact('hist_gradient_boosting'));assert h.predict(f)['risk_score']==pytest.approx(1/(1+__import__('math').exp(-.1)))


@pytest.mark.parametrize('change',[{'schema':'wrong'},{'features':list(reversed(FEATURES))},{'threshold':0.},{'algorithm':'unknown'}])
def test_portable_rejects_bad_artifacts(change):
    from roadsense.expanded_risk import Predictor
    with pytest.raises(ValueError):Predictor(dict(tree_artifact(),**change))


def test_portable_tree_cycles_and_bad_scales_are_rejected():
    from roadsense.expanded_risk import Predictor
    a=tree_artifact();a['trees'][0]['left'][0]=0
    with pytest.raises(ValueError):Predictor(a)
    a=tree_artifact();a.update(algorithm='logistic',coefficients=[0.]*44,scale_mean=[0.]*44,scale_scale=[0.]*44)
    with pytest.raises(ValueError):Predictor(a)


@pytest.mark.parametrize('value',[None,[],{'features':None}])
def test_bad_model_container_is_rejected(value):
    from roadsense.expanded_risk import Predictor
    with pytest.raises(ValueError):Predictor(value)


def test_temporal_export_uses_unlabelled_past_observations(tmp_path):
    import hashlib,json,sqlite3
    import expand_features
    c=tmp_path/'campaign';base=c/'dataset';base.mkdir(parents=True);(c/'history').mkdir()
    (c/'plan.json').write_text(json.dumps({'configurations':[{'configuration_id':'cfg','variant':'test'}]}))
    f=observation(4)
    row={'run_id':'run','configuration_id':'cfg','group':'cfg','sim_s':1.,'sequence':5,'split':'train','features':f,'labels':{'forward_risk_3s':0}}
    (base/'samples.jsonl').write_text(json.dumps(row)+'\n')
    h=hashlib.sha256((base/'samples.jsonl').read_bytes()).hexdigest()
    (base/'manifest.json').write_text(json.dumps({'features':list(BASE),'files':{'samples.jsonl':{'sha256':h}}}))
    db=sqlite3.connect(c/'history/roadsense-history.sqlite3')
    db.executescript("CREATE TABLE segments(segment_id TEXT,run_id TEXT); CREATE TABLE samples(segment_id TEXT,sequence INTEGER,sim_s REAL,features TEXT); INSERT INTO segments VALUES ('s','run');")
    db.executemany("INSERT INTO samples VALUES ('s',?,?,?)",[(i+1,(i+1)*.2,json.dumps(observation(i))) for i in range(6)]);db.commit();db.close()
    out=tmp_path/'derived';expand_features.export(base,out)
    r=json.loads((out/'samples.jsonl').read_text())
    assert r['features']['lead_lon_trend_mps']==pytest.approx(-5.)
    assert r['features']['lead_continuity_s']==pytest.approx(.8)
    assert r['features']['left_lat_trend_mps']==pytest.approx(-.5)
    assert set(r['features'])==set(FEATURES)
