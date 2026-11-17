"""Readable quality report from the frozen Phase 6D supervised export (no training)."""
import collections
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.extend([str(ROOT / "tools"), str(ROOT / "src")])
import scenario_dataset as ds
campaign, exported = map(Path, sys.argv[1:3])
m = json.loads((exported / 'manifest.json').read_text())
runs = [json.loads(l) for l in (campaign / 'campaign.jsonl').read_text().splitlines()]
by_split = collections.defaultdict(collections.Counter)
by_family = collections.defaultdict(collections.Counter)
positive_groups = collections.defaultdict(set)
positive_runs = collections.defaultdict(set)
seen_groups = collections.defaultdict(set)
seen_runs = collections.defaultdict(set)
for line in (exported / 'samples.jsonl').open():
    r = json.loads(line)
    positive = r['labels']['forward_risk_3s']
    for key, target in ((r['split'], by_split), (r['family'], by_family)):
        target[key]['positive' if positive else 'negative'] += 1
    ttc, gap = r['labels']['future_min_lead_ttc_s'], r['labels']['future_min_lead_gap_m']
    if not positive and ((ttc is not None and 1.5 <= ttc < 3) or (gap is not None and 2 <= gap < 8)):
        by_family[r['family']]['near_boundary_negative'] += 1
    seen_groups[r['split']].add(r['group'])
    seen_runs[r['split']].add(r['run_id'])
    if positive:
        positive_groups[r['split']].add(r['group'])
        positive_runs[r['split']].add(r['run_id'])

def table(head, rows):
    print('| ' + ' | '.join(map(str, head)) + ' |')
    print('| ' + ' | '.join('---' for _ in head) + ' |')
    for row in rows:
        print('| ' + ' | '.join(map(str, row)) + ' |')

print('# Phase 6D dataset validation — 2026-11-17\n')
print('These are newly executed local runs, distinct from Claude\'s Phase 6C pilot. No model was trained, no Pi action was taken, and the original Webots and development server were left running.\n')
print('Campaign: `%s`\n\nExport: `%s`\n' % (campaign, exported))
positive = sum(v['positive'] for v in by_split.values())
valid = m['supervised_samples']
wall, sim = m['wall_s'], m['sim_s']
resources = [r['resources'] for r in runs if r.get('resources')]
cpu = sum(r['child_user_cpu_s'] + r['child_system_cpu_s'] for r in resources)
size = sum(p.stat().st_size for p in campaign.rglob('*') if p.is_file() and not p.is_symlink())
table(['Measurement', 'Verified result'], [
    ('Attempted / successful runs', '%s / %s' % (m['attempted_runs'], m['successful_runs'])),
    ('Configurations / groups', '%s / %s' % (m['configurations'], sum(m['split_groups'].values()))),
    ('Seeds', ', '.join(map(str, m['seeds']))),
    ('Recorded / supervised samples', '%s / %s' % (m['samples']['recorded'], valid)),
    ('Positive / negative samples', '%s / %s (%.2f%% positive)' % (positive, valid-positive, 100*positive/valid)),
    ('Exclusions', json.dumps(m['exclusions'], sort_keys=True)),
    ('Near-boundary negative samples', sum(v['near_boundary_negative'] for v in by_family.values())),
    ('Recording acceptance', json.dumps(m['recording_quality'], sort_keys=True)),
    ('Raw / usable physical truth frames', '%s / %s' % (sum(r.get('truth_frames',0) for r in m['runs']), sum(r.get('valid_truth_frames',0) for r in m['runs']))),
    ('Forward near-miss episodes / overlap-proxy episodes', '%s / %s' % (m['episodes']['forward_near_miss'], m['episodes']['collision_proxy'])),
    ('Forward near-miss / overlap-proxy frames', '%s / %s' % (m['frames_with']['forward_near_miss'], m['frames_with']['collision_proxy'])),
    ('Tailgating episodes / frames', '%s / %s' % (m['episodes']['tailgated'], m['frames_with']['tailgated'])),
    ('Simulation / measured Webots wall seconds', '%s / %s (%.2f×)' % (sim, wall, sim/wall)),
    ('Campaign size at report time', '%.1f MB' % (size/1e6)),
    ('Child CPU time', '%.1f s (%.1f%% of measured Webots wall time)' % (cpu, 100*cpu/wall)),
    ('Peak child RSS', '%.1f MB' % (max(r['cumulative_peak_child_rss_bytes'] for r in resources)/1e6)),
])
print('\nNear-boundary negatives have future minimum lead TTC in [1.5,3.0) s or gap in [2,8) m, while remaining negative under the unchanged target definition. This is a descriptive difficulty measure, not a balancing rule.\n')
print('\nCPU/RSS are OS child-process accounting, with cumulative maximum RSS (a largest-child measure); they are not a simultaneous aggregate of every simulator process. Wall time excludes per-run startup/flush overhead.\n')
print('Geometric pothole encounters: %s distinct run/hazard pairs (intended boxes; not wheel impacts).\n' % sum(len(r['ground_truth']['potholes_under_ego']) for r in runs if r.get('ground_truth')))
prefix_episodes = 0
for run in runs:
    _, frames, _, _ = ds.sc.read_ground_truth(campaign / 'runs' / run['run_id'] / 'ground_truth.jsonl')
    observed = []
    for frame in frames:
        if frame.get('missing_physical'):
            continue
        outcome = ds.outcomes(frame)
        observed.append(outcome)
        if outcome['collision_proxy']:
            break
    prefix_episodes += ds.episodes(observed, 'forward_near_miss')
print('There are %s forward-risk temporal episodes before or at the first overlap proxy in each run. The larger raw episode/frame totals above include later observations; they are not independent training accidents. Usable positives span %s configuration groups, with 16 in train, 2 in validation, 2 in test and 3 in stress.\n' % (prefix_episodes, sum(len(v) for v in positive_groups.values())))
print('## Family distribution\n')
table(['Family','Runs','Positive','Negative','Forward-risk episodes','Proxy episodes'], [
    (family, sum(r['family']==family for r in runs), stats['positive'], stats['negative'],
     sum(r['episodes'].get('forward_near_miss',0) for r in m['runs'] if next(x['family'] for x in runs if x['run_id']==r['run_id'])==family),
     sum(r['episodes'].get('collision_proxy',0) for r in m['runs'] if next(x['family'] for x in runs if x['run_id']==r['run_id'])==family))
    for family, stats in sorted(by_family.items())])
print('\n## Staged-event execution\n')
event_counts = collections.Counter((r.get('event') or {}).get('phase', 'none') for r in runs)
print('Staged actions executed in %s/40 event runs; %s released before termination.\n' % (sum('act' in r.get('events',[]) for r in runs), sum('released' in r.get('events',[]) for r in runs)))
print('Final event phases: `%s`. A completed simulation is not necessarily a successfully staged event. Blocked/unreached events remain valid observed traffic runs; they do not count as executed interventions.\n' % dict(event_counts))
print('\n## Frozen grouped splits\n')
table(['Split','Groups','Runs with samples','Positive groups','Positive runs','Positive samples','Negative samples'], [
    (name,m['split_groups'].get(name,0),len(seen_runs[name]),len(positive_groups[name]),len(positive_runs[name]),by_split[name]['positive'],by_split[name]['negative'])
    for name in ('train','validation','test','stress')])
print('\nAssignments were frozen before simulation. Exact repetitions and near configurations share a group. No frame-level random splitting or outcome-based reassignment was used. Stress reserves unseen configurations, not an unseen road environment.\n')
print('## Missing features and schema\n')
print('The exact %s inference keys and the audit of all 51 source features are in manifest.json. Missing track-slot fields are structural when no track exists; missing values remain null.\n' % len(m['features']))
table(['Feature','Missing rate'],[(name,'%.3f%%' % (100*value)) for name,value in m['missing_feature_rates'].items() if value])
print('\nInference rows contain only the explicit observable feature whitelist; no simulator truth, future minima, risk indicators, rule decisions or controller target lane/manoeuvre are inside features. Labels, baseline and metadata remain separate. The current-risk field is evaluation-only privileged information.\n')
print('## Reproduction\n')
print('Use tools/scenario_dataset.py on the retained campaign and a new output directory. See docs/dataset.md for commands, target definitions, leakage audit, grouping policy and the Phase 7 plan. SHA256SUMS covers retained source data and export files; manifest.json records the sample checksum and the frozen plan checksum.\n')
print('Sample SHA256: `%s`.\n' % m['files']['samples.jsonl']['sha256'])
print('## Limits and training recommendation\n')
print('These are 5 Hz synthetic observations from one highway layout and an existing controller. Physical-pose truth improves alignment; intended vehicle dimensions, kinematic near misses and oriented-box overlap are still proxies. Contact is not independently established. Motorcycles remain lane-structured and autorickshaws use car visuals. Runs can have variable physical outcomes under identical scenario inputs. Episode counts are temporal clusters within runs, not statistically independent crashes.\n')
print('**Readiness: suitable for an initial Mac-only logistic-regression feasibility experiment after review. Insufficient for reliable model selection or broad generalization claims:** validation and test each have only two positive configuration groups. Keep RF/XGBoost comparison provisional until episode-level results and uncertainty justify it. No labels or groups were moved to improve this distribution. Do not tune on test or stress, train a final model, deploy to Pi, or launch a larger campaign without review.\n')

print('## Verification and handoff\n')
print('479 Python tests passed, including 49 scenario/handedness/dataset tests. The Phase 6C review also passed 65 JavaScript and 60 archived Arduino tests; UI code was unchanged. All 70 execution IDs, canonical parameter/configuration hashes, recorded source hashes and checksums were verified. SQLite quick_check passed. All seven original/repetition pairs have byte-identical world/network/route inputs. No unexpected missing selected feature was found when its track slot was present. A real resume invocation skipped all 70 completed tasks, and a second export reproduced samples.jsonl and manifest.json byte-for-byte. The original Webots and development-server PIDs remained 36894 and 36233. Vivaldi automation was not rerun; its earlier limitation remains. Phase 6B manual verification is documented, not newly performed here.\n')
print('Phase 6C was published as f4d40477db5ab11f92b7f3ef48fa048007a4f1d2; Phase 6B was already published as 0b48c59. Phase 6D publication and a separate bounded Phase 7A training experiment were approved on 2026-11-17. No credentials, databases, sample rows or bulk simulation data are included in the Git working tree.\n')
