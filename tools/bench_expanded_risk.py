"""Actual standard-library-only model loading and batch-1 feature/inference measurements on this host."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import sys
import time

sys.modules.update(numpy=None,scipy=None,sklearn=None,joblib=None)
from roadsense.learned_risk import FEATURES as BASE
from roadsense.expanded_risk import Predictor,TemporalFeatures


def percentile(values,p):
    values=sorted(values);return values[min(len(values)-1,round((len(values)-1)*p))]


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('model',type=Path);ap.add_argument('campaign',type=Path);ap.add_argument('out',type=Path);args=ap.parse_args()
    unit=1 if sys.platform=='darwin' else 1024
    before=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*unit;started=time.perf_counter_ns()
    predictor=Predictor.load(args.model)
    load_us=(time.perf_counter_ns()-started)/1000
    after=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*unit
    db=sqlite3.connect('file:%s?mode=ro'%(args.campaign/'history/roadsense-history.sqlite3'),uri=True)
    rows=[(rid,seq,t,{n:f[n] for n in BASE}) for rid,seq,t,encoded in db.execute('SELECT g.run_id,s.sequence,s.sim_s,s.features FROM samples s JOIN segments g USING(segment_id) ORDER BY g.run_id,s.sim_s,s.sequence LIMIT 10000') for f in [json.loads(encoded)]];db.close()
    stream=TemporalFeatures();inference=[];combined=[]
    for rid,seq,t,f in rows:
        start=time.perf_counter_ns();current=stream.observe(f,t,rid,seq);selected={n:current[n] for n in predictor.names};middle=time.perf_counter_ns()
        result=predictor.predict(selected);end=time.perf_counter_ns()
        if result['status']!='ok':raise ValueError('canonical input rejected: '+str(result))
        inference.append((end-middle)/1000);combined.append((end-start)/1000)
    report={'platform':platform.platform(),'python':platform.python_version(),'rows':len(rows),'model_sha256':hashlib.sha256(args.model.read_bytes()).hexdigest(),
            'model_bytes':args.model.stat().st_size,'model_load_us':load_us,'model_load_peak_rss_increase_bytes':max(0,after-before),
            'inference_us':{'p50':percentile(inference,.5),'p95':percentile(inference,.95),'p99':percentile(inference,.99)},
            'feature_and_inference_us':{'p50':percentile(combined,.5),'p95':percentile(combined,.95),'p99':percentile(combined,.99)},
            'third_party_ml_imports_blocked':True,'pi_measured':False,
            'note':'Peak RSS increase measured across model loading before input-row allocation; latency excludes DB I/O but combined latency includes causal feature preparation.'}
    if args.out.exists():raise FileExistsError('benchmark output exists')
    args.out.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
