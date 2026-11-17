# Phase 7A.2 prospective protocol (frozen before new outcomes)

The published experimental Phase 7A baseline is `fac5857`; preserve its JSON SHA256 `ea94c638fd7eec8362679553704cff029a8f50a5663b4e7a65e5bf605a69f32a` and every historical split. No live deployment is approved.

## Diagnosis and justified changes

Historical re-evaluation reproduced the frozen metrics. Of 434 missed positive frames, 121 had no confirmed lead and only 9 had an immature lead; 387 had a side track and 167 had lateral speed above 0.3 m/s. Two missed stress incidents had a mature lead throughout all 15 warning samples; another had none throughout. Missing observability and inadequate feature/model structure both matter. Of 588 false-positive frames, 185 lacked a lead; 271 had lateral motion. This does not prove the threshold alone is wrong. The fixed score threshold is not changed retroactively.

The original 30-feature logistic model has weak early warning, misses all training cut-in incidents, and uses correlated range-rate/longitudinal-speed coefficients. There are only 16 training-positive configuration groups. Labels are still kinematic near-miss and intended-box proxies; no actual-contact or real-road claim follows. SUMO lane-change mode 512 **enforces collision avoidance and safety gaps**, despite a misleading old comment ([official documentation](https://sumo.dlr.de/docs/TraCI/Change_Vehicle_State.html#lane-change-mode-0xb6)). New scenarios explicitly record safe mode 512 or deliberate gap-bypassing mode 0, request duration, physical Webots lane-change delay and intended vehicle type. This changes scenario inputs, never risk-label thresholds.

Add a versioned causal 44-feature contract: preserve the 30 in order, plus measured forward TTC, nominal left/right corridor-crossing times, steering rate, one-second lead range trend/relative acceleration/lead-speed acceleration, side range/lateral trends and continuity durations. Instantaneous kinematic cues may reproduce TTC; an updated 30-feature LR and matched TTC are controls. Temporal continuity is only an age/motion consistency proxy because the recorded slot features lack object IDs. It resets on run/sequence/time gaps, absence, stale observations, age resets and implausible slot jumps. No simulator identity or future observation is substituted. Compute features over **all** recorded causal frames before filtering supervised rows, so ground-truth censorship does not create fictitious sensor gaps.

## Allocation and collection gate

At most **140 additional attempts**, including six smoke attempts. Freeze the complete external plan and its hash before Webots launches: **80 train, 20 validation, 20 fresh test, 10 fresh stress, 10 prospective ordinary-cohort runs**. Every configuration is unique and pairwise non-near to every new and old configuration under the existing relation, conservatively ignoring extra type/mode/secondary-event differences. Reject/re-draw close candidate inputs before observing outcomes; never move a collected group. All original holdouts remain historical-only and never enter training.

Variants include severe/moderate braking, motorcycle/autorickshaw cut-ins, slow entry, dual threats plus potholes, dense mixed traffic, safe overtaking, close-safe following and resolving threats. Vary ego limits 40/55/70/80 km/h, SUMO road limits 40/60/80/100, traffic mix/density, event distances/gaps/deceleration/settling, and physical lane-change delay 1–4 s. Ego uses the existing sample controller, with only an optional experiment speed cap (default remains 80). Original world/session and safety decisions stay untouched. Autorickshaws still use car visuals and intended dimensions; motorcycles remain lane-structured.

Train/validation collection completes first. Freeze all fitted candidates, the selected model and threshold **before collecting** fresh test/stress/ordinary runs. The runner requires a candidate freeze bound to the plan. This is stronger than merely hiding their performance. Smoke is part of train, not an extra campaign. Failed/interrupted attempts consume budget and are preserved; no automatic retries or extra campaign. Storage budget 4 GB, 50 MB headroom between runs, 2 GB minimum free space. Serial local ports 8011/8901, no Ollama, no Pi traffic. Bulk data outside Git.

Ordinary cohort: no staged event; seeded uniform simulator density/mix/speed variations, with occasional nuisance potholes. It represents this declared highway simulation distribution, **not Indian real-world accident prevalence**. Enriched dangerous scenarios and ordinary-cohort metrics are reported separately. Targets (not guaranteed): >=10 positive fresh-test groups; >=6 validation and >=5 stress positive groups. Record shortfalls without changing allocations, target definitions or budget.

## Model search, threshold selection and controls

Reuse original target `roadsense.forward-risk.v1`: any physical forward near-miss observation in (t,t+3 s], requiring all 15 future frames. Preserve all Phase 6D quality exclusions and independently log source/recording checksums. Old train groups may augment new train; old validation/test/stress may not.

Small fixed search: LR C=1 with original 30 features; LR with causal v2 features (unweighted and balanced); RF 80 trees, depth 6 or 8, minimum leaf 10, balanced_subsample; HistGradientBoosting 60 iterations, 7 or 15 leaves, minimum leaf 15, learning rate 0.1, balanced classes. No additional training dependency beyond existing scikit-learn. Three shuffled group folds, seed 20261117, preprocessing fitted inside each fold; no oversampling. Fit full train candidates after CV, never fit on validation. Compare all candidates, frozen Phase 7A and forward TTC.

Use validation only to select the candidate and threshold. Fixed score threshold grid: 21 geometric points from .005 to .99 plus 1.0; include fixed TTC 4.5 s as a separate baseline. Also choose a TTC operating threshold on the same validation constraints. Feasible thresholds require overall validation FPR <=5% and <=20 false episodes/hour on unstaged validation ordinary runs. Rank feasible points by pre-onset incident recall, F2, median lead, fewer false alarms and smaller artifact. If no feasible point exists, rank by F2/incident recall and explicitly mark selection infeasible. Never open fresh held-out performance until the freeze exists. No retuning after final evaluation.

## Numerical acceptance criteria (research only)

Approval for further live-integration research requires **all** of:

- Fresh test >=10 positive groups and >=10 evaluable pre-onset incidents; stress >=5 positive groups and >=5 evaluable incidents; ordinary exposure >=0.25 simulated hours.
- Test pre-onset incident recall >=70%; stress >=60%; both at least the frozen baseline, and stress at least the validation-matched TTC baseline.
- Pre-developing frame recall >=50% in both test and stress; median detected warning lead >=1 s.
- Ordinary false alarm episodes/hour <=10 and at most half the frozen baseline rate (if baseline rate is zero, candidate must also be zero).
- Portable JSON <=2 MiB; Mac batch-1 p95 <=2 ms; measured model-loading RSS increase <=64 MiB; parity error <=1e-6 (tree float32 input semantics documented) and zero threshold disagreements on retained samples.

Use whole-configuration bootstrap intervals (1,000 draws, fixed seed), incident counts and lead-time quantiles; treat small counts and repeated time samples honestly. Scores trained on enrichment remain uncalibrated, not real accident probabilities. Even passing these engineering targets is not automotive safety certification or permission to deploy. No further campaign is automatic; no Pi connection/restart/reboot/deployment, live-server changes, authoritative action changes, dashboard redesign, paid infrastructure or fabricated GPS.
