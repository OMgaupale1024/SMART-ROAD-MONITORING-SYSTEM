# Phase 6C plan: randomized Indian-road scenario generation (proposed, not started)

Goal: generate many different, reproducible driving scenarios from a seed with Webots and SUMO, record
**independent simulation ground truth** for each, and collect bulk training datasets on the Mac, so
that Phase 7 can train a numerical risk model whose labels are not just the rule's output.

Out of scope: training a model, large batches, changes to the authoritative safety algorithms
(`roadsense.autonomy`), real vehicles, paid infrastructure, real GPS.

## What exists and can be reused

| Piece | Where | Use in 6C |
| ----- | ----- | --------- |
| SUMO traffic driven through Webots' `SumoInterface` / `sumo_supervisor` | `simulation/webots/worlds/roadsense_highway.wbt`, `roadsense_highway_net/` | the traffic engine |
| `plugin.py` hook: `sumo_supervisor` imports `<world>_net/plugin.py` and calls `SumoSupervisorPlugin(sup, traci, net).run(ms)` every step | stock Webots | **the scenario director and ground-truth logger**: it has both the Webots supervisor and TraCI, without editing the world or the controllers |
| vehicle types `car`, `bus`, `truck`, `trailer`, `motorcycle` | `sumo.rou.xml` | mixed traffic; Webots already renders SUMO motorcycles |
| `RoadSensePothole` PROTO | `simulation/webots/protos/` | randomized potholes, placed by the supervisor at run start |
| Fast batch runs: `--batch --mode=fast --no-rendering`, about 200 s of simulation in 25 s | | bulk generation |
| History dataset mode + `history_export --group-map` | Phase 6A | per-frame features, heuristic labels, grouped splits |

## Design

```text
scenario_batch.py (Mac) ── for each (family, seed):
   1. draw parameters with random.Random(seed); write scenario.json
   2. generate a run directory outside the repo: a copy of the world + _net/ with plugin.py,
      its own SUMO port, sumocfg with --seed=<seed> (and --lefthand net, see 6C.1)
   3. launch Webots in fast mode, env ROADSENSE_RUN_ID=<family>-<seed>, ROADSENSE_EDGE_URL → local
      RoadSense server (dataset mode, campaign history directory)
   4. plugin.py: places potholes, sets traffic mix, triggers the family's event via TraCI at the
      drawn time/position, logs ground truth at 5 Hz, quits at the drawn duration
   5. append run_id → family/seed to the campaign's group map
export: history_export --dir <campaign> --group-map groups.json export …  (+ ground_truth/*.jsonl)
```

- **One run id for both sides.** Today the ego controller invents its run id. A small controller change
  (not the safety code): use `ROADSENSE_RUN_ID` when set. The ground-truth file and the history
  segments then join on `(run_id, sim_s)`.
- **Ground truth is recorded by the supervisor, never by the RoadSense pipeline**: every vehicle's
  true position, speed, lane, type and size; the ego's true pose; pothole positions and severities;
  the scheduled and the actual event (who braked, when, how hard; who cut in, gap at the cut);
  derived outcomes computed from true geometry: collision (footprint overlap), near miss (true TTC
  below 1.5 s or gap below 2 m), unsafe headway (below 1 s), pothole strike (ego wheel path over a
  pothole). These are the labels a model can be evaluated against.
- **Storage on the Mac**, not in the repo, not in `/tmp`: one directory per campaign, for example
  `~/Library/Application Support/RoadSense/campaigns/<name>/` with `scenarios.jsonl`,
  `ground_truth/<run_id>.jsonl`, `groups.json`, the history database and exports. Estimated
  2–5 MB per 200 s run, so 100 runs fit in about 0.5 GB. The Pi stays events-only.

## Scenario families

Each family draws its parameters from the seed; all also draw traffic density and mix.

| Family | Randomized | Ground-truth outcome of interest |
| ------ | ---------- | -------------------------------- |
| `normal` | density, speeds, mix | no conflicts (the negatives a model needs) |
| `sudden_brake` | lead gap 10–40 m, deceleration 4–8 m/s², time | headway, true TTC minimum, collision or not |
| `cut_in` | side, gap at cut 3–12 m, cutter speed difference | true TTC minimum after the cut |
| `tailgating` | follower time gap 0.3–1.0 s, or ego closing on a slow lead | unsafe headway duration |
| `potholes` | count 2–10, position, lane, severity, size | pothole strike or avoided |
| `mixed` | motorcycle / autorickshaw / truck / bus share | interactions with small, slow vehicles |
| `combined` | sudden brake or cut-in within 50 m of a pothole | conflict and strike together |

**Mixed vehicles:** motorcycles are native. SUMO and Webots have no autorickshaw: define a SUMO
`vType` (about 2.8 m × 1.4 m, 50 km/h, low acceleration, more erratic `sigma`), rendered in Webots as
the nearest stand-in model. The ground truth carries the true type and size; the radar sees the
stand-in's shape, which must be stated in the dataset manifest.

## Steps

**6C.1 Feasibility (short, nothing generated at scale)**

1. Left-hand traffic: rebuild a copy of the network with `netconvert --lefthand`, check that the
   Webots roads, the SUMO lanes, the ego's direction and RoadSense's lane relations still line up.
2. Check the radar and tracking on motorcycles and the autorickshaw stand-in.
3. Prove the `plugin.py` director in a copy: place potholes, trigger one sudden brake and one cut-in
   by TraCI, log ground truth, quit at a set time.
4. Add `ROADSENSE_RUN_ID` to the controller (keeping Python 3.9 compatibility) with a test.

**6C.2 Generator and ground truth**: `tools/scenario_batch.py`, the families above,
`scenario.json` + ground-truth schema `roadsense.groundtruth.v1`, tests on the parameter draws and
the outcome computations (pure functions, testable without Webots).

**6C.3 Pilot**: one run per family (7 runs, a few minutes on the Mac). Check: same seed gives the
same parameters and the same event; the realized event matches the scheduled one; ground truth
and history samples join on `(run_id, sim_s)`; the export's group map keeps a family and seed in one
split. Then stop for review. Large batches only after approval.

## Decisions needed before or during 6C

1. **Overtaking side.** `safety._free_lane` checks the left lane before the right, so with both free
   it recommends the left: the right-hand-traffic convention. In Indian left-hand traffic,
   overtaking is on the right. Options: (a) keep the rule unchanged and record it as a known
   mismatch in every manifest (the recommendation is advisory), or (b) a separate, approved change
   that makes the preferred side a policy parameter. 6C does not change it.
2. **The ego's own driving** is the Webots `highway_overtake` sample logic; it overtakes on its own
   convention, independently of RoadSense's recommendation. Fine for data generation, but it must be
   in the manifest.
3. **Autorickshaw stand-in model**: accept a stand-in, or limit 6C to cars, two-wheelers, trucks, buses.
4. **Lane discipline**: SUMO's sublane model (two-wheelers sharing lanes) is closer to Indian traffic
   but slower and outside RoadSense's lane-based perception. Proposed: not in 6C.

## Known risks

- Run-to-run SUMO start timing changes the traffic even with the same seed (seen in earlier phases).
  Events are triggered relative to the ego's position, and the ground truth records what actually
  happened; the seed fixes the parameters, not every vehicle's path.
- The 5 km road ends at about 256 s: runs are 60–240 s.
- The Webots world must never be saved after a run (that broke it once); the generator works on copies.
- One Webots with SUMO at a time on the default port; generated worlds use their own port.
