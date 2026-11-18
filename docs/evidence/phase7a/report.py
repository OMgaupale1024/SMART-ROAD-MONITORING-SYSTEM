"""Render the frozen Phase 7A results without fitting or changing model/thresholds."""
import json
import math
from pathlib import Path
import sys

root = Path(sys.argv[1])
e = json.loads((root / 'evaluation.json').read_text())
cv = json.loads((root / 'training-cv.json').read_text())
f = json.loads((root / 'freeze.json').read_text())
a = json.loads((root / 'model.json').read_text())
chosen = e['chosen_variant']
order = ('always_negative', 'ttc_forward', 'unweighted', 'balanced')
splits = ('train', 'validation', 'test', 'stress')

def fmt(v, digits=3):
    return '—' if v is None else f'{v:.{digits}f}'

def percent(v):
    return '—' if v is None else f'{v * 100:.1f}%'

def table(head, rows):
    print('| ' + ' | '.join(head) + ' |')
    print('| ' + ' | '.join('---' for _ in head) + ' |')
    for row in rows:
        print('| ' + ' | '.join(map(str, row)) + ' |')
    print()

def ratio(r):
    return '%s/%s' % (r['pre_event_detected_incidents'], r['pre_event_eligible_incidents'])

def wilson(k, n):
    if not n:
        return 'unavailable'
    z = 1.959963984540054
    p = k / n
    center = (p + z*z/(2*n))/(1+z*z/n)
    radius = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return '[%s, %s]' % (percent(center-radius), percent(center+radius))

print('# Phase 7A — trained forward-risk logistic feasibility model\n')
print('**A real lightweight numerical model is trained and reproducible. Do not proceed to live Pi integration yet.** The selected unweighted logistic model improves this small test set over the forward-TTC baseline, but stress performance is worse on several important measures. Validation/test have only two positive configuration groups each; test has only one evaluable pre-onset incident. Training-only OOF recall is 36.9%, validation recall 18.0%, and stress recall 19.2%. These are material misses, not evidence of general road safety.\n')
print('## Phase 6D publication and data verification\n')
print('Published commit: `7ae80d6c9015b4bb18c6c551a2cbe213b782ce87` on origin/roadsense-v2-edge-ai, verified by remote ref after push. Reviewed source, 30-feature audit and compact evidence; no campaign data or credentials were staged. All 611 retained checksums and recording-source hashes verified. A fresh export reproduced the sample bytes; manifest fields match except the exporter source hash after fixing checksum paths for exports outside the campaign directory. The regression check covers that destination.\n')
print('Dataset SHA256: `%s`. The frozen corpus contains 52,135 recorded and 43,233 supervised samples (738 positive /42,495 negative), 70 runs, 63 configurations and 61 groups. Exclusions: 229 unusable/missing truth matches, 1,939 incomplete future windows, 6,734 samples at/after first overlap proxy =8,902. Labels were recomputed from physical Webots truth within the same execution over all 15 future 200 ms observations. No future information or simulator truth is an inference input.\n' % a['dataset_sha256'])
print('## Method and freeze\n')
print('Exact 30-feature ordering retained; median imputation (keep empty columns), StandardScaler and L2 logistic C=1, lbfgs, tol=1e-6, max_iter=2000. Four shuffled StratifiedGroupKFold splits, seed 20261117, only training groups; imputation/scaling and balanced weights fit separately inside each fold. No feature selection, oversampling, calibration fitting, hyperparameter sweep, RF, boosting or neural networks. Ten fits: four CV folds plus one full-train fit per candidate.\n')
print('Thresholds and the selected model were frozen from training OOF F2, with predetermined false-alarm/incident/lead tie-breaks, before a single held-out evaluation. Validation, test and stress did not fit preprocessing, coefficients, class weights or selection. Frozen unweighted threshold: **%.12f**; balanced: **%.12f**. The 0.5 diagnostic is saved but was not used for selection. Balanced full-train weights were negative %.6f, positive %.6f; unweighted used None.\n' % (f['thresholds']['unweighted'],f['thresholds']['balanced'],a['training']['class_weight_balanced']['0'],a['training']['class_weight_balanced']['1']))
print('Source commit: `%s` (Phase 6D base). Training code was uncommitted; exact source hashes and copies are in the artifact source/ directory. No claim is made that the training code is contained in that base commit. Fit time %.3f s; peak training-process RSS %.1f MB.\n' % (a['source_commit'], f['fit_wall_s'], f['peak_process_rss_bytes']/1e6))
table(['Fold','Fit groups','OOF groups','Fit positive frames','OOF positive frames'],
      [(i+1,len(r['train_groups']),len(r['validation_groups']),r['train_positive'],r['validation_positive']) for i,r in enumerate(cv['folds'])])
print('## Split and incident coverage\n')
table(['Split','Samples','Positive /negative','Groups','Positive groups','Temporal incidents','Pre-onset evaluable','During evaluable'],
      [(s,e['splits'][s]['samples'],f"{e['splits'][s]['models'][chosen]['positive_samples']} /{e['splits'][s]['models'][chosen]['negative_samples']}",e['splits'][s]['groups'],e['splits'][s]['positive_groups'],e['splits'][s]['models'][chosen]['incident_count'],e['splits'][s]['models'][chosen]['pre_event_eligible_incidents'],e['splits'][s]['models'][chosen]['during_event_eligible_incidents']) for s in splits for r in [e['splits'][s]['models'][chosen]]])
print('Incidents are physical-truth temporal clusters with <=1 s between positive frames, cut at first overlap proxy. They are not statistically independent crashes. All repetitions remain grouped. An incident without a retained pre-event sample is unavailable for early-warning evaluation, not counted as a successful prediction or an avoidable miss. The second positive test group has developing-risk frames but no usable pre-onset window.\n')
print('## Training out-of-fold comparison\n')
reports = {**cv['baselines'], **{k:v['selected'] for k,v in cv['candidates'].items()}}
table(['Model','Threshold','Precision','Recall','F2','AP','False alarms/h','Pre-onset detected','Median lead s'],
      [(name,fmt(reports[name]['threshold'],6),percent(reports[name]['precision']),percent(reports[name]['recall']),fmt(reports[name]['f2']),fmt(reports[name]['average_precision']),fmt(reports[name]['false_alarms_per_hour'],1),ratio(reports[name]),fmt(reports[name]['median_warning_lead_s'],1)) for name in order])
print('The TTC baseline uses only the measured forward lead: clearance=max(0,lead_lon−3.9 m), closing=−lead_v_lon>0.1 m/s, ego speed>2 m/s, warning for TTC<=4.5 s or clearance<2 m. Its score is 1/(1+TTC), or 1 for a sub-2 m gap. This fixed 4.5 s threshold extrapolates the 1.5 s near-miss boundary over the 3 s horizon under constant velocity. It does not change deployed safety logic and is not a comparison to unrelated rear/hazard actions. Always negative has no positive predictions and precision is undefined.\n')
print('## Held-out test comparison — all four baselines/models\n')
reports = e['splits']['test']['models']
table(['Model','TN /FP /FN /TP','Precision','Recall','F1','F2','AP','ROC-AUC'],
      [(name,' /'.join(str(n) for pair in reports[name]['confusion_matrix_tn_fp_fn_tp'] for n in pair),percent(reports[name]['precision']),percent(reports[name]['recall']),fmt(reports[name]['f1']),fmt(reports[name]['f2']),fmt(reports[name]['average_precision']),fmt(reports[name]['roc_auc'])) for name in order])
table(['Model','FNR','FPR','Brier','10-bin ECE','False alarms/h','Warning duty','Pre-onset detected','Median lead s'],
      [(name,percent(reports[name]['false_negative_rate']),percent(reports[name]['false_positive_rate']),fmt(reports[name]['brier_score'],5),fmt(reports[name]['ece_10_bins'],5),fmt(reports[name]['false_alarms_per_hour'],1),percent(reports[name]['warning_duty_cycle']),ratio(reports[name]),fmt(reports[name]['median_warning_lead_s'],1)) for name in order])
print('The selected model improves test F2/AP and false alarms relative to this TTC baseline; it does **not** establish a reliable advantage across unseen configurations. On stress, TTC detects 4/6 evaluable incidents, selected logistic 2/6; TTC has F2 .214/AP .126 versus logistic .189/.064. No threshold or model was changed after seeing this.\n')
print('## Selected model on every partition\n')
table(['Split','TN /FP /FN /TP','Precision','Recall','FNR','FPR','F1','F2','AP','ROC-AUC','Brier'],
      [(s,' /'.join(str(n) for pair in r['confusion_matrix_tn_fp_fn_tp'] for n in pair),percent(r['precision']),percent(r['recall']),percent(r['false_negative_rate']),percent(r['false_positive_rate']),fmt(r['f1']),fmt(r['f2']),fmt(r['average_precision']),fmt(r['roc_auc']),fmt(r['brier_score'],5)) for s in splits for r in [e['splits'][s]['models'][chosen]]])
print('The train row is fitted-model performance, not an unbiased estimate; use the earlier OOF table for training generalization. Full per-family/per-model metrics, default-threshold diagnostics and both confusion matrices are in evaluation.json.\n')
print('## Warning timing and false alarms\n')
table(['Split','Model','Pre-onset detection','During detection','Median pre-onset lead s','False alarms/h','FP frames/h'],
      [(s,name,ratio(r),f"{r['during_event_detection_rate'] and fmt(r['during_event_detection_rate']) or ('0' if r['during_event_detection_rate']==0 else '—')} ({r['during_event_eligible_incidents']} eligible)",fmt(r['median_warning_lead_s'],1),fmt(r['false_alarms_per_hour'],1),fmt(r['false_positive_frames_per_hour'],1)) for s in splits for name in order for r in [e['splits'][s]['models'][name]]])
print('Lead time is the earliest warning sample within the 3 s target window, outside developing risk and after the previous incident; it is capped by the horizon and is not first alarm onset or time before measured contact. False alarms are consecutive warning episodes with no target-positive frame, divided by valid sample exposure (n×0.2 s). Gaps and wall time are excluded. Sustained mixed episodes can mask nuisance alarms, so frame FPR, FP frames/h and duty cycle are also reported. Even the selected test model produces about 52 false alarm episodes/hour; validation ~56 and stress ~94. This is unsuitable as an automatic action trigger.\n')
table(['Split','Future-positive frames before developing','Recall before developing','Future-positive frames already developing','Recall already developing'],
      [(s,sum(r['before_developing']['confusion_matrix_tn_fp_fn_tp'][1]),percent(r['before_developing']['recall']),sum(r['already_developing']['confusion_matrix_tn_fp_fn_tp'][1]),percent(r['already_developing']['recall'])) for s in splits for r in [e['splits'][s]['models'][chosen]]])
print('The selected model catches many developing situations better than early ones. Validation pre-developing recall is only 8.9%, stress 14.4%; test is 48.8%. This distinction materially weakens the early-prediction claim.\n')
print('## Selected model by scenario family\n')
table(['Split','Family','Positive frames','Precision','Recall','F2','Pre-onset incidents detected'],
      [(s,family,r['positive_samples'],percent(r['precision']),percent(r['recall']),fmt(r['f2']),ratio(r)) for s in splits for family,r in e['splits'][s]['models'][chosen]['families'].items()])
print('Zero positive observations means recall is undefined, not perfect. The selected model misses all four evaluable training cut-in incidents and all stress combined/mixed incidents. Family labels never entered the inference vector; these are failure analyses, not extra feature tuning.\n')
print('## Uncertainty and calibration\n')
table(['Split','Positive groups','Recall 95% group bootstrap','Precision 95% group bootstrap','Pre-onset detected','Illustrative Wilson interval'],
      [(s,e['splits'][s]['positive_groups'],str(r['group_bootstrap']['recall']['interval_95']),str(r['group_bootstrap']['precision']['interval_95']),ratio(r),wilson(r['pre_event_detected_incidents'],r['pre_event_eligible_incidents'])) for s in splits for r in [e['splits'][s]['models'][chosen]]])
print('Bootstrap: 1,000 whole-group resamples, seed 20261117. Zero-denominator replicates are omitted and counted in JSON. For the single detectable test incident, the conditional bootstrap incident interval degenerates to [1,1] and omits 323/1,000 replicates; **that is not useful uncertainty evidence**. The illustrative 1/1 Wilson interval is about [21%,100%], and even it assumes independent Bernoulli events, which repeated/configuration-correlated incidents do not guarantee. Frame intervals with two positive groups mostly reweight those same known configurations and cannot establish population generalization.\n')
print('Scores remain **uncalibrated risk scores**, not reliable collision/near-miss probabilities. Balanced training shifts the class prior: test Brier .124/ECE .281 are much worse than unweighted .00551/.00871. Unweighted stress Brier .02486 is worse than always negative .02372. Small ECE/Brier can be dominated by negatives. No probability calibrator was fit with these few independent events.\n')
table(['Test score bin','Samples','Mean selected score','Observed positive fraction'],
      [(f"[{r['lower']:.1f},{r['upper']:.1f}{']' if r['upper']==1 else ')'}",r['count'],fmt(r['mean_score'],5),fmt(r['observed_fraction'],5)) for r in e['splits']['test']['models'][chosen]['calibration']])
print('## Features, artifact and numerical parity\n')
print('Inputs, in exact order:\n\n```text\n'+'\n'.join(f"{i+1:02d} {n}" for i,n in enumerate(a['features']))+'\n```\n')
print('No derived pipeline TTC/conflict scores, rule decisions, future minima, simulator poses/types, controller intent, family, run ID or time is in that vector. Relative positions/velocities are ingredients of TTC, so a learned linear boundary can approximate existing kinematics. Large correlated velocity coefficients do not prove novel reasoning or causal importance. Steering/acceleration can reflect this simulated controller\'s response to a developing situation, limiting transfer to other controllers.\n')
table(['Largest standardized coefficient','Value'],[(n,fmt(v,6)) for n,v in sorted(zip(a['features'],a['coefficients']),key=lambda x:abs(x[1]),reverse=True)[:8]])
lat=e['mac_inference']
print('Selected inspectable JSON: **%s bytes**, 30 coefficients plus intercept, median/scaler arrays, threshold, model/input versions, dataset/source checksums, pinned training configuration. No pickle was written or loaded. Training-time end-to-end scikit-learn pipeline parity over 27,457 train rows: maximum absolute score error %.3g. Across all 43,233 rows, independent NumPy/scipy-expit reconstruction versus the portable predictor: %.3g. The actual JSON artifact also predicted all rows successfully with numpy/scipy/sklearn/joblib imports explicitly blocked.\n' % ((root/'model.json').stat().st_size,a['training_parity_max_abs_error'],e['all_sample_parity'][chosen]['max_abs_error']))
print('Mac batch-1 inference (10,000 calls, Python %s, %s): p50 **%.3f µs**, p95 **%.3f µs**, p99 **%.3f µs**. Includes input validation, missing-value substitution, scaling, dot product, sigmoid and result status; excludes feature extraction, I/O and application scheduling. This is not a Pi latency measurement.\n' % (lat['python'],lat['platform'],lat['p50_us'],lat['p95_us'],lat['p99_us']))
print('A separate zero-fit serialization check restored scikit-learn LogisticRegression from the frozen learned coefficients on all 43,233 samples: maximum score error 6.66e-16 and zero threshold mismatches. See parity-validation.json.\n')
print('## Reproduction and proposed Pi approach\n')
print('Artifacts: `%s`. source/ preserves the exact fitting/evaluation source and protocol; SHA256SUMS covers it and all model/evaluation JSON. The freeze checksum is unchanged after evaluation. Scikit-learn and numerical packages are isolated in ~/.local/share/roadsense/ml-python312; requirements-ml.txt is separate from Pi/web dependencies.\n' % root)
print('From the repository root, with the existing dataset and a new external output directory:\n\n```sh\npython3.12 -m pip install --only-binary=:all: --target "$HOME/.local/share/roadsense/ml-python312" -r requirements-ml.txt\nexport PYTHONPATH="$HOME/.local/share/roadsense/ml-python312:$PWD/src"\nDATASET="$HOME/Library/Application Support/RoadSense/campaigns/phase6d-20261117/dataset-v1"\nMODEL_OUT="$HOME/Library/Application Support/RoadSense/ml/phase7a-reproduction"\npython3.12 tools/train_forward_risk.py fit "$DATASET" "$MODEL_OUT"\npython3.12 tools/train_forward_risk.py evaluate "$DATASET" "$MODEL_OUT"\n```\n')
print('Future Pi inference can use only learned_risk.py, this ~6 KB JSON and Python standard library. Load once outside the safety loop; incompatible artifacts raise a load error, which a future observer adapter must catch and expose as unavailable. Current-input validation failures return invalid_input/insufficient_input with null score/warning, model version and shadow status. The predictor never supplies a braking or steering action. No live hook was added; shadow adapter design is documented in docs/learned-risk.md. Pi inference latency and live coexistence remain unmeasured.\n')
print('## Recommendation and restrictions\n')
print('**Expand independent dataset coverage before Phase 7B live/Pi integration.** The experiment demonstrates genuinely trained, tiny, fast numerical inference and some test predictive signal, but high misses, nuisance warnings, failed cut-ins and stress degradation prevent a deployment recommendation. Any next campaign needs separate approval: target independent positive cut-in/combined/mixed configurations and matched difficult negatives; reserve more positive groups in new evaluation sets prospectively, without moving the current frozen groups or lowering truth thresholds. Revisit event/false-alarm utility with a new preregistered experiment, not this held-out test. Do not train larger model families just to improve an impressive table.\n')
print('Phase 6D is published. Phase 7A source/artifacts are local for review. 501 Python regressions, 65 JavaScript tests and 60 archived Arduino tests pass, including 22 portable-model/incident cases. No new simulation, Pi connection/restart/reboot/deployment, live-server modification, authoritative-decision change or dashboard redesign was performed. All safety claims remain limited to synthetic proxy observations and this existing controller.\n')
print('Implementation references: [scikit-learn LogisticRegression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html), [SimpleImputer / preserving empty features](https://scikit-learn.org/stable/modules/impute.html#keeping-the-number-of-features-constant). Training statistics are fit only on the training partition/fold; these references describe the APIs, not validation of RoadSense performance.')
