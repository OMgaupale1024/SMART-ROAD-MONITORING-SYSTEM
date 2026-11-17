"""Phase 7A.2 fixed small model search. Fit uses development only; evaluate requires the frozen selection."""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
import warnings

import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier,HistGradientBoostingClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from roadsense.expanded_risk import FEATURES,INPUT_SCHEMA,MODEL_SCHEMA,Predictor
from roadsense.learned_risk import FEATURES as BASE,Predictor as Baseline
import train_forward_risk as old

ROOT=Path(__file__).resolve().parents[1]
BASELINE_SHA='ea94c638fd7eec8362679553704cff029a8f50a5663b4e7a65e5bf605a69f32a'
THRESHOLDS=[float(t) for t in np.geomspace(.005,.99,21)]+[1.]
CONFIGS={
 'lr30':{'algorithm':'logistic','features':'v1','weight':None},
 'lr44':{'algorithm':'logistic','features':'v2','weight':None},
 'lr44_balanced':{'algorithm':'logistic','features':'v2','weight':'balanced'},
 'rf6':{'algorithm':'random_forest','features':'v2','depth':6},
 'rf8':{'algorithm':'random_forest','features':'v2','depth':8},
 'hgb7':{'algorithm':'hist_gradient_boosting','features':'v2','leaves':7},
 'hgb15':{'algorithm':'hist_gradient_boosting','features':'v2','leaves':15},
}


def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write(p,r,compact=False):
    Path(p).write_text(json.dumps(r,sort_keys=True,allow_nan=False,**({'separators':(',',':')} if compact else {'indent':2}))+'\n')


def read_rows(path,splits):
    manifest=json.loads((path/'manifest.json').read_text())
    if tuple(manifest['features'])!=FEATURES or manifest['input_schema']!=INPUT_SCHEMA or digest(path/'samples.jsonl')!=manifest['files']['samples.jsonl']['sha256']:
        raise ValueError('dataset feature/checksum mismatch')
    rows=[];groups={}
    for line in (path/'samples.jsonl').open():
        r=json.loads(line)
        if groups.setdefault(r['group'],r['split'])!=r['split']:raise ValueError('split leakage')
        if r['split'] in splits:
            if tuple(sorted(r['features']))!=tuple(sorted(FEATURES)):raise ValueError('wrong feature keys')
            rows.append(r)
    return sorted(rows,key=lambda r:(r['run_id'],r['sim_s']))


def events_for(rows):
    by_origin=defaultdict(list)
    for r in rows:by_origin[r['origin']].append(r)
    events={}
    for origin,subset in by_origin.items():events.update(old.load_events(Path(origin),subset))
    return events


def matrix(rows,names):
    return np.asarray([[np.nan if r['features'][n] is None else r['features'][n] for n in names] for r in rows],dtype=np.float64)


def names(config):return BASE if config['features']=='v1' else FEATURES


def pipeline(config):
    impute=SimpleImputer(strategy='median',keep_empty_features=True)
    kind=config['algorithm']
    if kind=='logistic':return make_pipeline(impute,StandardScaler(),LogisticRegression(C=1.,class_weight=config['weight'],solver='lbfgs',max_iter=2000,tol=1e-6,random_state=old.SEED))
    if kind=='random_forest':return make_pipeline(impute,RandomForestClassifier(n_estimators=80,max_depth=config['depth'],min_samples_leaf=10,class_weight='balanced_subsample',n_jobs=1,random_state=old.SEED))
    return make_pipeline(impute,HistGradientBoostingClassifier(max_iter=60,max_leaf_nodes=config['leaves'],min_samples_leaf=15,learning_rate=.1,class_weight='balanced',early_stopping=False,categorical_features=None,random_state=old.SEED))


def export_model(model,config,key,threshold,provenance):
    estimator=model.steps[-1][1]
    a=dict(provenance,schema=MODEL_SCHEMA,algorithm=config['algorithm'],target='forward_risk_3s',horizon_s=3,
           features=list(names(config)),input_schema='roadsense.forward-inputs.v1' if config['features']=='v1' else INPUT_SCHEMA,
           model_version='roadsense-7a2-%s-v1'%key,output='uncalibrated_risk_score',threshold=threshold,
           impute_median=model.steps[0][1].statistics_.tolist(),configuration=config)
    if config['algorithm']=='logistic':
        scaler=model.steps[1][1]
        a.update(scale_mean=scaler.mean_.tolist(),scale_scale=scaler.scale_.tolist(),coefficients=estimator.coef_[0].tolist(),intercept=float(estimator.intercept_[0]))
    elif config['algorithm']=='random_forest':
        a['trees']=[]
        for item in estimator.estimators_:
            t=item.tree_;leaf=t.children_left==-1;v=t.value[:,0,:];total=v.sum(axis=1)
            a['trees'].append({'feature':np.where(leaf,-1,t.feature).tolist(),'left':t.children_left.tolist(),'right':t.children_right.tolist(),
                             'threshold':np.where(leaf,0.,t.threshold).tolist(),'value':np.where(leaf,v[:,1]/total,0.).tolist()})
    else:
        a['intercept']=float(estimator._baseline_prediction[0,0]);a['trees']=[]
        for stage in estimator._predictors:
            if len(stage)!=1:raise ValueError('only binary numerical boosted trees supported')
            t=stage[0].nodes
            if np.any(t['is_categorical']):raise ValueError('categorical tree unsupported')
            leaf=t['is_leaf'].astype(bool)
            a['trees'].append({'feature':np.where(leaf,-1,t['feature_idx'].astype(int)).tolist(),
                             'left':np.where(leaf,-1,t['left'].astype(int)).tolist(),'right':np.where(leaf,-1,t['right'].astype(int)).tolist(),
                             'threshold':np.where(leaf,0.,t['num_threshold']).tolist(),'value':np.where(leaf,t['value'],0.).tolist()})
    return a


def portable_scores(artifact,rows):
    p=Predictor(artifact);scores=[]
    for r in rows:
        answer=p.predict({n:r['features'][n] for n in p.names})
        if answer['status']!='ok':raise ValueError('portable predictor rejected canonical input')
        scores.append(answer['risk_score'])
    return np.asarray(scores)


def short(report):
    return {k:v for k,v in report.items() if k not in ('incidents','group_counts','families','calibration')}


def select_threshold(rows,scores,events,thresholds):
    ordinary=[i for i,r in enumerate(rows) if r['variant'] in ('ordinary','dense_mixed')]
    points=[]
    for threshold in thresholds:
        r=old.score_report(rows,scores,threshold,events)
        o=old.score_report([rows[i] for i in ordinary],scores[ordinary],threshold,events) if ordinary else None
        feasible=r['false_positive_rate'] is not None and r['false_positive_rate']<=.05 and o is not None and o['false_alarms_per_hour']<=20
        points.append({'metrics':short(r),'ordinary_metrics':short(o) if o else None,'feasible':feasible})
    feasible=[p for p in points if p['feasible']]
    def rank(p):
        r=p['metrics']
        return (r['pre_event_detection_rate'] or 0,r['f2'] or 0,r['median_warning_lead_s'] or 0,-(r['false_alarms_per_hour'] or 0),r['threshold']) if feasible else (r['f2'] or 0,r['pre_event_detection_rate'] or 0,r['threshold'])
    return max(feasible or points,key=rank),points


def fit(old_data,development,campaign,out,require_blind=False):
    if out.exists() or ROOT in out.resolve().parents:raise ValueError('new external model directory required')
    plan_path=campaign/'plan.json';plan=json.loads(plan_path.read_text())
    heldout_present=any(r.get('planned_split') in ('test','stress','ordinary') for r in map(json.loads,(campaign/'campaign.jsonl').read_text().splitlines()))
    if require_blind and heldout_present:
        raise ValueError('fresh held-out collection already started before fit')
    train=read_rows(old_data,{'train'})+read_rows(development,{'train'})
    validation=read_rows(development,{'validation'})
    groups=np.asarray([r['group'] for r in train]);y=np.asarray([r['labels']['forward_risk_3s'] for r in train])
    if not validation or len(set(groups[y==1]))<3:raise ValueError('insufficient development groups')
    if set(groups)&{r['group'] for r in validation}:raise ValueError('train/validation leakage')
    folds=list(StratifiedGroupKFold(3,shuffle=True,random_state=old.SEED).split(np.zeros(len(y)),y,groups))
    assert all(not set(groups[a])&set(groups[b]) and len(set(y[a]))==len(set(y[b]))==2 for a,b in folds)
    train_events=events_for(train);validation_events=events_for(validation)
    files=[ROOT/'src/roadsense/expanded_risk.py',Path(__file__),ROOT/'tools/expand_features.py',ROOT/'tools/forward_risk_metrics.py',ROOT/'docs/phase7a2-plan.md',ROOT/'requirements-ml.txt']
    provenance={'source_revision':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
                'source_dirty':bool(subprocess.check_output(['git','status','--porcelain'])),
                'source_sha256':{str(p.relative_to(ROOT)):digest(p) for p in files},'sklearn_version':sklearn.__version__,
                'campaign_plan_sha256':digest(plan_path),'historical_dataset_sha256':digest(old_data/'samples.jsonl'),
                'development_dataset_sha256':digest(development/'samples.jsonl'),
                'seed':old.SEED,'training_groups':len(set(groups)),'validation_groups':len({r['group'] for r in validation}),
                'selection_splits':['validation'],'heldout_present_at_fit':heldout_present,'threshold_grid':THRESHOLDS,'early_stopping':False,'class_prior_note':'enriched simulation scores are not real-world probabilities'}
    out.mkdir(parents=True)
    for p in files:
        q=out/'source'/p.relative_to(ROOT);q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
    comparisons={};artifacts={};references={};start=time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter('error',ConvergenceWarning)
        for key,config in CONFIGS.items():
            x=matrix(train,names(config));xv=matrix(validation,names(config));oof=np.full(len(y),np.nan)
            for a,b in folds:
                model=pipeline(config).fit(x[a],y[a]);oof[b]=model.predict_proba(x[b])[:,1]
            assert np.isfinite(oof).all()
            cv=old.score_report(train,oof,.5,train_events)
            model=pipeline(config).fit(x,y)
            references[key]=model
            score=model.predict_proba(xv)[:,1];selected,search=select_threshold(validation,score,validation_events,THRESHOLDS)
            artifact=export_model(model,config,key,selected['metrics']['threshold'],provenance)
            reference=np.concatenate([model.predict_proba(x)[:,1],score]);portable=portable_scores(artifact,train+validation)
            error=float(np.max(np.abs(reference-portable)));mismatch=int(np.count_nonzero((reference>=artifact['threshold'])!=(portable>=artifact['threshold'])))
            if error>1e-6 or mismatch:raise ValueError('portable mismatch '+key+': '+str((error,mismatch)))
            artifact['parity_development']={'rows':len(portable),'max_abs_error':error,'threshold_mismatches':mismatch}
            write(out/(key+'.json'),artifact,True);artifacts[key]=artifact
            comparisons[key]={'oof_at_0_5':short(cv),'validation_selection':selected,'threshold_search':search,
                              'artifact_bytes':(out/(key+'.json')).stat().st_size,'parity':artifact['parity_development']}
            print(json.dumps({'candidate':key,'oof_ap':cv['average_precision'],'val_pre_recall':selected['metrics']['pre_event_detection_rate'],
                              'threshold':artifact['threshold'],'feasible':selected['feasible'],'bytes':comparisons[key]['artifact_bytes']}),flush=True)
    feasible=[key for key,c in comparisons.items() if c['validation_selection']['feasible']]
    def rank(key):
        r=comparisons[key]['validation_selection']['metrics']
        return (r['pre_event_detection_rate'] or 0,r['f2'] or 0,r['median_warning_lead_s'] or 0,-(r['false_alarms_per_hour'] or 0),-comparisons[key]['artifact_bytes']) if feasible else (r['f2'] or 0,r['pre_event_detection_rate'] or 0,-comparisons[key]['artifact_bytes'])
    chosen=max(feasible or list(comparisons),key=rank)
    matched,_=select_threshold(validation,old.ttc_scores(validation),validation_events,sorted(set(THRESHOLDS+[1/5.5])))
    write(out/'development.json',comparisons)
    shutil.copyfile(out/(chosen+'.json'),out/'model.json')
    freeze=dict(provenance,schema='roadsense.expanded-freeze.v1',selected=chosen,selected_artifact_sha256=digest(out/'model.json'),
                candidate_sha256={k:digest(out/(k+'.json')) for k in CONFIGS},thresholds={k:a['threshold'] for k,a in artifacts.items()},
                validation_selection_feasible=bool(feasible),ttc_matched_threshold=matched['metrics']['threshold'],
                fit_count=28,fit_wall_s=time.perf_counter()-start,peak_training_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                fold_groups=[{'train':sorted(set(groups[a])),'oof':sorted(set(groups[b]))} for a,b in folds],fresh_holdout_evaluated=False)
    write(out/'freeze.json',freeze)
    print(json.dumps({'selected':chosen,'threshold':artifacts[chosen]['threshold'],'freeze_sha256':digest(out/'freeze.json'),'fit_count':28}),flush=True)
    return references


def evaluate(old_data,final,campaign,out,references=None):
    if (out/'evaluation.json').exists():raise FileExistsError('final evaluation already exists; no retuning')
    freeze=json.loads((out/'freeze.json').read_text());freeze_sha=digest(out/'freeze.json')
    if digest(campaign/'plan.json')!=freeze['campaign_plan_sha256'] or digest(out/'model.json')!=freeze['selected_artifact_sha256']:
        raise ValueError('selection/plan changed')
    baseline_path=ROOT/'docs/evidence/phase7a/model.json'
    if digest(baseline_path)!=BASELINE_SHA:raise ValueError('historical baseline changed')
    fresh=read_rows(final,{'train','validation','test','stress','ordinary'})
    old_rows=read_rows(old_data,{'validation','test','stress'})
    historical=[]
    for r in old_rows:
        r=dict(r);r['split']='historical_'+r['split'];historical.append(r)
    rows=fresh+historical;events=events_for(rows)
    scores={};thresholds=dict(freeze['thresholds'])
    for key,sha in freeze['candidate_sha256'].items():
        if digest(out/(key+'.json'))!=sha:raise ValueError('candidate changed')
        a=json.loads((out/(key+'.json')).read_text());scores[key]=portable_scores(a,rows)
    base=Baseline.load(baseline_path)
    scores['phase7a_frozen']=np.asarray([base.predict({n:r['features'][n] for n in BASE})['risk_score'] for r in rows]);thresholds['phase7a_frozen']=base.threshold
    scores['ttc_fixed']=old.ttc_scores(rows);scores['ttc_matched']=scores['ttc_fixed'];thresholds.update(ttc_fixed=1/5.5,ttc_matched=freeze['ttc_matched_threshold'])
    native_parity={}
    if references is not None:
        for key,model in references.items():
            expected=model.predict_proba(matrix(rows,names(CONFIGS[key])))[:,1]
            actual=scores[key];threshold=thresholds[key]
            native_parity[key]={'rows':len(rows),'max_abs_error':float(np.max(np.abs(expected-actual))),'threshold_mismatches':int(np.count_nonzero((expected>=threshold)!=(actual>=threshold)))}
    results={'schema':'roadsense.expanded-evaluation.v1','freeze_sha256':freeze_sha,'selected':freeze['selected'],'native_parity':native_parity,'splits':{}}
    for split in sorted({r['split'] for r in rows}):
        ix=[i for i,r in enumerate(rows) if r['split']==split];subset=[rows[i] for i in ix]
        models={}
        for key,s in scores.items():
            r=old.score_report(subset,s[ix],thresholds[key],events);r['group_bootstrap']=old.bootstrap(r)
            r['families']={}
            for family in sorted({r['family'] for r in subset}):
                fi=[i for i in ix if rows[i]['family']==family]
                r['families'][family]=short(old.score_report([rows[i] for i in fi],s[fi],thresholds[key],events))
            models[key]=r
        results['splits'][split]={'samples':len(subset),'positive_samples':sum(r['labels']['forward_risk_3s'] for r in subset),
            'groups':len({r['group'] for r in subset}),'positive_groups':len({r['group'] for r in subset if r['labels']['forward_risk_3s']}),'models':models}
    selected=freeze['selected'];test=results['splits']['test'];stress=results['splits']['stress'];ordinary=results['splits']['ordinary']
    t,s,o=[p['models'][selected] for p in (test,stress,ordinary)]
    baseline_o=ordinary['models']['phase7a_frozen']['false_alarms_per_hour']
    checks={
      'coverage':test['positive_groups']>=10 and t['pre_event_eligible_incidents']>=10 and stress['positive_groups']>=5 and s['pre_event_eligible_incidents']>=5 and o['exposure_hours']>=.25,
      'test_incident_recall':(t['pre_event_detection_rate'] or 0)>=.7,
      'stress_incident_recall':(s['pre_event_detection_rate'] or 0)>=.6 and (s['pre_event_detection_rate'] or 0)>=(stress['models']['ttc_matched']['pre_event_detection_rate'] or 0),
      'beats_frozen_incident_recall':all((p['models'][selected]['pre_event_detection_rate'] or 0)>=(p['models']['phase7a_frozen']['pre_event_detection_rate'] or 0) for p in (test,stress)),
      'early_frame_recall':all((r['before_developing']['recall'] or 0)>=.5 for r in (t,s)),
      'lead':all((r['median_warning_lead_s'] or 0)>=1 for r in (t,s)),
      'ordinary_false_alarms':o['false_alarms_per_hour']<=10 and o['false_alarms_per_hour']<=(.5*baseline_o if baseline_o else 0),
      'model_size':(out/'model.json').stat().st_size<=2*1024*1024,
      'native_parity':bool(native_parity) and native_parity[selected]['max_abs_error']<=1e-6 and native_parity[selected]['threshold_mismatches']==0}
    results['numerical_acceptance_before_runtime']=checks
    results['acceptance_requires_runtime_check']=True
    assert digest(out/'freeze.json')==freeze_sha
    write(out/'evaluation.json',results)
    print(json.dumps({'selected':selected,'checks':checks,'test_incidents':(t['pre_event_detected_incidents'],t['pre_event_eligible_incidents']),'stress_incidents':(s['pre_event_detected_incidents'],s['pre_event_eligible_incidents'])}))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('command',choices=('fit','evaluate','run'));ap.add_argument('historical',type=Path);ap.add_argument('data',type=Path);ap.add_argument('campaign',type=Path);ap.add_argument('out',type=Path);args=ap.parse_args()
    with threadpool_limits(limits=1):
        if args.command=='run':
            # Retain native fitted models across collection for parity without serializing/loading pickle.
            references=fit(args.historical,args.data,args.campaign,args.out,require_blind=True)
            env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(Path.home()/'.local/share/roadsense/python312'),str(ROOT/'src'))),ROADSENSE_CAMPAIGN_ROOT=str(args.campaign.parent))
            subprocess.run([sys.executable,str(ROOT/'tools/scenario_expansion.py'),'heldout','--campaign',args.campaign.name,'--freeze',str(args.out/'freeze.json')],check=True,env=env)
            import scenario_dataset,expand_features
            base=args.campaign/'dataset-final';final=args.campaign/'features-final'
            scenario_dataset.export(args.campaign,base);expand_features.export(base,final)
            before={(r['run_id'],r['sequence']):(r['features'],r['labels']) for r in read_rows(args.data,{'train','validation'})}
            after={(r['run_id'],r['sequence']):(r['features'],r['labels']) for r in read_rows(final,{'train','validation'})}
            if before!=after:raise ValueError('development rows changed during held-out collection')
            evaluate(args.historical,final,args.campaign,args.out,references)
        else:(fit if args.command=='fit' else evaluate)(args.historical,args.data,args.campaign,args.out)
