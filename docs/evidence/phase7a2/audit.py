import collections, hashlib, json, sqlite3, sys
from pathlib import Path
sys.path[:0]=[str(Path.cwd()/'tools'),str(Path.cwd()/'src')]
import scenario_dataset as ds
import scenario_expansion as ex
from roadsense.expanded_risk import FEATURES, TemporalFeatures
from roadsense.learned_risk import FEATURES as BASE

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def load(p):return json.loads(p.read_text())
c=Path.home()/'Library/Application Support/RoadSense/campaigns/phase7a2-20261117'
m=Path.home()/'Library/Application Support/RoadSense/ml/phase7a2-20261117'
plan=load(c/'plan.json');records=[json.loads(l) for l in (c/'campaign.jsonl').read_text().splitlines()]
assert len(records)==len({r['run_id'] for r in records})==len({r['configuration_id'] for r in records})==140
old=load(c.parent/'phase6d-20261117/plan.json')['configurations']
assert plan==ex.build_plan(old)
for i,p in enumerate(plan['configurations']):assert not any(ex.related(p,q) for q in old+plan['configurations'][:i])
checked=0
for root,sums in [(c,c/'dataset-final/SHA256SUMS'),(c/'features-final',c/'features-final/SHA256SUMS')]:
    for line in sums.read_text().splitlines():
        h,name=line.split('  ',1);assert sha(root/name)==h,name;checked+=1
frozen=load(m/'freeze.json');gate=load(c/'heldout-gate.json');assert gate['sha256']==sha(m/'freeze.json')
assert not frozen['heldout_present_at_fit'] and frozen['campaign_plan_sha256']==sha(c/'plan.json')
for k,h in frozen['source_sha256'].items():assert sha(m/'source'/k)==h
for k,h in frozen['candidate_sha256'].items():assert sha(m/(k+'.json'))==h
assert sha(m/'model.json')==frozen['selected_artifact_sha256']
assert sha(Path('docs/evidence/phase7a/model.json'))=='ea94c638fd7eec8362679553704cff029a8f50a5663b4e7a65e5bf605a69f32a'
byrun=collections.defaultdict(dict);positive=collections.defaultdict(set);groups=collections.defaultdict(set);counts=collections.Counter();sources=collections.Counter();stages=collections.Counter();unmatched=collections.Counter();presence=collections.Counter();missing=collections.Counter();terminations=collections.Counter()
for line in (c/'features-final/samples.jsonl').open():
    r=json.loads(line);assert set(r['features'])==set(FEATURES)
    split=plan['assignments'][r['configuration_id']];assert (r['split'],r['group'])==(split['split'],split['group'])
    byrun[r['run_id']][r['sequence']]=r;counts[r['split']]+=1;groups[r['split']].add(r['group'])
    if r['labels']['forward_risk_3s']:positive[r['split']].add(r['group'])
    missing.update(n for n,v in r['features'].items() if v is None)
db=sqlite3.connect('file:%s?mode=ro'%(c/'history/roadsense-history.sqlite3'),uri=True)
validated=0;raw_count=0;versions=set();truth_count=missing_physical=0;retained_incidents=collections.Counter()
for record in records:
    rid=record['run_id'];d=c/'runs'/rid;p=load(d/'scenario.json');sources[p['source_snapshot']]+=1
    for name,h in p['source_sha256'].items():assert sha(c/p['source_snapshot']/name)==h
    assert record['ok'] and not record['gaps'] and record['server']['missed']==record['server']['rejected']==0
    assert not any(record['history_status']['lost'].values())
    header,frames,events,end=ds.sc.read_ground_truth(d/'ground_truth.jsonl');assert header['run_id']==rid and header['scenario']==p
    stages.update(e['what'] for e in events);terminations[end['reason']]+=1;versions.add(json.dumps(p['simulator_versions'],sort_keys=True))
    truth_count+=len(frames);missing_physical+=sum(bool(f.get('missing_physical')) for f in frames)
    all_frames={ds.tick(f['t']):f for f in frames};truth={t:ds.outcomes(f) for t,f in all_frames.items() if not f.get('missing_physical')}
    first_proxy=min((t for t,o in truth.items() if o['collision_proxy']),default=float('inf'))
    temporal=TemporalFeatures()
    for seq,t,encoded in db.execute('SELECT s.sequence,s.sim_s,s.features FROM samples s JOIN segments g USING(segment_id) WHERE g.run_id=? ORDER BY s.sim_s,s.sequence',(rid,)):
        raw_count+=1;tick=ds.tick(t);raw=json.loads(encoded);features=temporal.observe({n:raw[n] for n in BASE},t,rid,seq)
        if tick not in truth:unmatched['missing_physical_pose' if tick in all_frames else 'no_truth_frame']+=1
        r=byrun[rid].pop(seq,None)
        eligible=tick in truth and tick<first_proxy and all(tick+j in truth for j in range(1,16))
        assert eligible==(r is not None),(rid,seq,'eligibility')
        if r is None:continue
        assert r['features']==features and r['sim_s']==t and r['current_forward_risk']==truth[tick]['forward_near_miss']
        future=[truth[tick+j] for j in range(1,16)]
        assert r['labels']['forward_risk_3s']==int(any(o['forward_near_miss'] for o in future))
        assert r['labels']['future_forward_overlap_proxy']==any(o['forward_overlap_proxy'] for o in future)
        for source,target in [('lead_gap_m','future_min_lead_gap_m'),('lead_ttc_s','future_min_lead_ttc_s')]:
            values=[o[source] for o in future if o[source] is not None];assert r['labels'][target]==(min(values) if values else None)
        validated+=1
    assert not byrun[rid]
db.close()
for s,g in groups.items():assert not any(g&h for t,h in groups.items() if s!=t)
assert raw_count==load(c/'dataset-final/manifest.json')['samples']['recorded']
report={'checksums_verified':checked,'runs':len(records),'successful_runs':sum(r['ok'] for r in records),'unique_configurations':len(plan['configurations']),
 'split_samples':dict(counts),'split_groups':{s:len(g) for s,g in groups.items()},'positive_groups':{s:len(positive[s]) for s in groups},
 'raw_samples':raw_count,'supervised_labels_and_causal_features_recomputed':validated,'unmatched_truth_reasons':dict(unmatched),'truth_frames':truth_count,'missing_physical_truth_frames':missing_physical,
 'staged_log_counts':dict(stages),'termination_reasons':dict(terminations),'source_snapshot_run_counts':dict(sources),'simulator_versions':[json.loads(v) for v in sorted(versions)],
 'missing_feature_counts':dict(missing),'plan_sha256':sha(c/'plan.json'),'dataset_sha256':sha(c/'features-final/samples.jsonl'),'freeze_sha256':sha(m/'freeze.json'),
 'all_new_pairs_and_old_pairs_non_near':True,'privileged_inference_features':False,'baseline_unchanged':True,'native_parity':load(m/'evaluation.json')['native_parity']}
Path('docs/evidence/phase7a2/quality-audit.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in ('missing_feature_counts','native_parity')}))
