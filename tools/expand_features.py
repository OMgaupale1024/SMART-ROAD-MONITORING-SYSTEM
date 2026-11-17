"""Causal v2 export: process all recorded sensor-feature frames, then retain the existing supervised mask."""
import argparse
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3

from roadsense.expanded_risk import TemporalFeatures,FEATURES,INPUT_SCHEMA
from roadsense.learned_risk import FEATURES as BASE
import scenario_dataset as ds


def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def export(base,out):
    if out.exists():raise FileExistsError("never overwrite a derived dataset")
    manifest=json.loads((base/'manifest.json').read_text())
    if tuple(manifest['features'])!=BASE or digest(base/'samples.jsonl')!=manifest['files']['samples.jsonl']['sha256']:
        raise ValueError('base feature contract/checksum mismatch')
    campaign=base.parent
    configs={p['configuration_id']:p for p in json.loads((campaign/'plan.json').read_text())['configurations']}
    conn=sqlite3.connect('file:%s?mode=ro'%(campaign/'history/roadsense-history.sqlite3'),uri=True)
    out.mkdir(parents=True)
    counts=Counter();positive=Counter();missing=Counter()
    with (base/'samples.jsonl').open() as source,(out/'samples.jsonl').open('w') as dest:
        stream=(json.loads(l) for l in source)
        for rid,group in itertools.groupby(stream,key=lambda r:r['run_id']):
            eligible={ds.tick(r['sim_s']):r for r in group};history=TemporalFeatures()
            raw=conn.execute('SELECT s.sequence,s.sim_s,s.features FROM samples s JOIN segments g USING(segment_id) WHERE g.run_id=? ORDER BY s.sim_s,s.sequence',(rid,))
            for seq,t,encoded in raw:
                f=json.loads(encoded);causal=history.observe({n:f[n] for n in BASE},t,rid,seq)
                row=eligible.pop(ds.tick(t),None)
                if row is None:continue
                assert all(row['features'][n]==causal[n] for n in BASE)
                row.update(features=causal,feature_schema=INPUT_SCHEMA,variant=configs[row['configuration_id']].get('variant','historical'),origin=str(base.resolve()))
                counts[row['split']]+=1;positive[row['split']]+=row['labels']['forward_risk_3s'];missing.update(n for n,v in causal.items() if v is None)
                dest.write(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n')
            if eligible:raise ValueError('supervised rows not found in recording')
    conn.close()
    result={'schema':'roadsense.temporal-dataset.v1','input_schema':INPUT_SCHEMA,'features':list(FEATURES),
            'target_schema':ds.SCHEMA,'target':'forward_risk_3s','horizon_s':3,'parent_dataset':str(base.resolve()),
            'parent_samples_sha256':digest(base/'samples.jsonl'),'parent_manifest_sha256':digest(base/'manifest.json'),
            'temporal_source_sha256':digest(Path(__file__).resolve().parents[1]/'src/roadsense/expanded_risk.py'),
            'samples_per_split':dict(counts),'positive_per_split':dict(positive),'missing_counts':dict(missing),
            'files':{'samples.jsonl':{'rows':sum(counts.values()),'sha256':digest(out/'samples.jsonl')}}}
    (out/'manifest.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    (out/'SHA256SUMS').write_text('\n'.join(digest(out/n)+'  '+n for n in ('samples.jsonl','manifest.json'))+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('base',type=Path);p.add_argument('out',type=Path);a=p.parse_args()
    r=export(a.base,a.out);print(json.dumps({'samples':r['samples_per_split'],'positive':r['positive_per_split']}))
