# Randomized scenarios and simulator ground truth (Phase 6C)

Seeded Webots + SUMO scenarios, run in fast batch mode on the Mac. Each run records RoadSense's
numerical features and heuristic labels (history dataset mode, Phase 6A) and, separately, **ground
truth from the simulator's true positions**. Status: implemented and piloted (7 families × 2 runs).
No large campaign has been run, and no model has been trained. Nothing here runs on the Raspberry Pi.

```sh
PYTHONPATH=src venv/bin/python tools/scenario_batch.py pilot --campaign NAME [--seeds 1] [--traffic left|right]
PYTHONPATH=src venv/bin/python tools/scenario_batch.py run --campaign NAME --family cut_in [--family …] --seeds 1-20 [--tag=-repeat]
PYTHONPATH=src venv/bin/python tools/scenario_batch.py export --campaign NAME
```

Campaigns are stored in `~/Library/Application Support/RoadSense/campaigns/<name>/`, or under
`ROADSENSE_CAMPAIGN_ROOT`. The tool refuses a campaign directory inside the repository or `/tmp`.

```text
<campaign>/campaign.jsonl          one summary line per run (appended)
<campaign>/history/                RoadSense history database (dataset mode)
<campaign>/groups.json, export-N/  written by `export`: history_export with run → profile:family:seed groups
<campaign>/runs/<run_id>/
    scenario.json                  the drawn parameters (roadsense.scenario.v1)
    worlds/scenario.wbt            generated world; worlds/scenario_net/ SUMO net, routes, plugin.py
    ground_truth.jsonl             roadsense.groundtruth.v1: header, a frame every 200 ms, events, end
    outcomes.jsonl                 per-frame outcomes computed from the ground truth
    server.log, webots.log
```

## How a run works

1. `scenarios.draw(family, seed, profile)` derives every parameter from one
   `random.Random("profile:family:seed")`. The generated world, network and routes are
   byte-identical for the same inputs (see the pilot's SHA256SUMS).
2. A world copy is generated:
   - the ego is placed for the road profile;
   - the viewpoint is kept at the same offset from the ego, because `SumoInterface` shows SUMO
     vehicles only within its radius of the viewpoint;
   - the fixed potholes and the parked `vehicle(1)` are removed;
   - SUMO gets its own port (8901) and the drawn seed.
3. A local RoadSense server is started on 127.0.0.1:8011 with history dataset mode and the run's
   `ROADSENSE_ROAD_PROFILE`, and with `ROADSENSE_AI_EXPLAIN` unset (no Ollama). Webots runs with
   `--batch --mode=fast --no-rendering`, and `ROADSENSE_EDGE_URL` points at that local server, never
   at the Pi. `ROADSENSE_RUN_ID` gives the controller and the ground truth the same run id.
4. `tools/scenario_plugin.py` (a `sumo_supervisor` plugin) does the in-simulation work:
   - imports the drawn potholes;
   - stages the event when the ego has travelled `at_m`;
   - writes a ground-truth frame every SUMO step;
   - quits Webots at the drawn duration.
5. The batch tool joins history samples to ground-truth frames on `(run_id, round(sim_s, 1))`,
   summarizes the outcomes, and appends the run to `campaign.jsonl`.

## Traffic handedness

`highway` (the default) is the original right-hand world, with the ego heading toward −x.
`highway_lht` (Indian roads) uses the same carriageway driven the other way:

- the ego heads toward +x;
- both SUMO edges are reversed and the network has `lefthand="true"`;
- the lane shapes are reversed and lane indices still count from the kerb, so no lane moves.

RoadSense derives the physical left and right from the heading (`perception.right_step`). The
controller swaps its left and right distance sensors and steering sign so that the sample's
overtaking logic works in either direction.

The safety rule changes only its **preference** when both sides are free and safe: left in
right-hand traffic, right in left-hand traffic. Collision, blind-spot, lane-availability and
emergency-brake checks are unchanged and decide alone when only one side is possible
(`tests/test_traffic_side.py`).

**Verified in a short Webots run of each profile:**
- SUMO traffic on the ego's edge moves with the ego, and the opposite edge moves against it.
- Lanes are at the same Webots y in both profiles.
- The perceived left, right and lead slots agree with the true side of the nearest vehicle in
  95% of samples (LHT 815/857, RHT 1946/2018). Most disagreements are tracks beyond 50 m.

## Families

| Family | Drawn | Staged by the plugin |
| ------ | ----- | -------------------- |
| `normal` | density 4–12 /km/lane, mix, speed limit 60/70/80 | nothing |
| `sudden_brake` | gap 12–40 m, 4–8 m/s², to 0/10/20 km/h, hold 3–8 s | a lead placed in the ego's lane at ego speed, 3 s steady, then braking at the drawn deceleration, held |
| `cut_in` | side, gap 3–12 m, speed −10…+5 km/h | a car placed in the adjacent lane on the drawn side (the other side if none), forced change into the ego's lane over 3 s |
| `tailgating` | time gap 0.3–1.0 s, 15–30 s | a car placed 15 m behind, then a gap controller holding the drawn time gap |
| `potholes` | 4–10 potholes: lane, offset, severity, size | potholes imported at start |
| `mixed` | motorcycles 30–45%, autorickshaws 15–25%, trucks, buses | nothing |
| `combined` | sudden brake or cut-in, and a pothole 25–60 m beyond it | both |

Event vehicles are placed as `sumo_supervisor` places the ego: with `vehicle.add` and then
`moveToXY`, because SUMO's insertion checks refused the slots. A slot must be clear of other traffic
for 8 m (the ego excepted, since its distance to the slot is the drawn parameter), and the plugin
retries for up to 10 s. If the slot never clears, the event is recorded as `slot_never_clear`, the
run continues as unstaged traffic, and the summary shows it in `events`.

## Ground truth and outcomes

Each frame records:
- **the ego:** true Webots pose and speed;
- **every SUMO vehicle within 150 m:** id, true type, Webots position (front bumper, as SUMO
  reports it), yaw, speed, length, width, edge and lane.

The header records the scenario, the pothole positions and the ego footprint. The ground truth is
computed without importing RoadSense, so it is independent of the pipeline it evaluates.

`scenarios.frame_outcomes` derives these outcomes from true geometry alone:

| Outcome | Definition |
| ------- | ---------- |
| `collision` | footprint overlap: the ego box (1.0 m behind to 3.9 m ahead of its origin, ±0.95 m) against each vehicle's box |
| `lead_*`, `follow_*` | gap, TTC and headway to the nearest vehicle overlapping the ego's lateral extent, ahead and behind |
| `near_miss` | collision, TTC < 1.5 s or gap < 2 m |
| `unsafe_headway` | lead headway < 1 s |
| `tailgated` | follower headway < 1 s |
| `pothole_under_ego` | the pothole's extent overlaps the ego box |

## Approximations (stated in every report and to be stated in the Phase 7 dataset card)

- **Autorickshaw.** Webots draws SUMO vehicles only as car, bus, truck, trailer or motorcycle
  models. The autorickshaw is a SUMO `passenger` vType (2.8 × 1.4 m, max 50 km/h, accel 1.2 m/s²,
  `sigma` 0.8) and appears in Webots, and to the radar, as a car model. Ground truth carries the
  true type and size.
- **Motorcycles** are genuine SUMO `motorcycle`s drawn as Webots motorcycle models. They keep to
  lanes: SUMO's sublane model, where two-wheelers share lanes, is not used.
- **Collision is footprint overlap of boxes**, not Webots contact. In the side-swipe examined it
  began about 1.6 s before the physical impact, which showed as the ego dropping from 17 to 9 m/s
  in 0.2 s. A pothole strike is also an overlap, not a wheel path.
- **The ego drives with the Webots `highway_overtake` sample logic**, not with RoadSense:
  RoadSense's decisions are advisory and are recorded as labels. Ego collisions are therefore
  outcomes of the sample controller.
- **Labels in the export are heuristic** (the safety rule's output, `label_provenance.ground_truth
  = false`). The independent outcomes are in each run's `outcomes.jsonl`, and joining them into a
  training table is Phase 7 work.
- **Event triggers are positional.** The plugin acts when the ego reaches `at_m`, so the staged
  situation depends on the traffic present at that moment (see reproducibility below).

## Pilot, 2026-11-17 (Mac, left-hand traffic, seed 1)

Evidence: `docs/evidence/phase6c/`.
- `pilot-campaign.jsonl` and `pilot-report.txt` hold every run's numbers.
- `pilot-SHA256SUMS` covers the inputs and the ground truth.
- `pilot-export-manifest.json` is the export's manifest.
- The analysis scripts are alongside.

| Measure | Result |
| ------- | ------ |
| Runs | 14 (7 families, each run twice), **14 completed**: every run ended at its drawn duration with an `end` record and no Webots errors |
| Staged events | 8 of 8 event runs: spawn, insertion, action, release |
| Speed | 333 s wall time for 2,105 s simulated (6.3× real time); 17–30 s per run |
| Samples | 10,524 (624–893 per run); RoadSense server 0 missed, 0 rejected, no sequence gaps |
| Ground truth | 10,510 frames; 10,510 of 10,524 samples joined to a frame (all but 1 per run: the sample at 0.2 s, before the plugin's first frame at 0.4 s) |
| Heuristic labels | MAINTAIN 9,651 · SLOW_DOWN 325 · MONITOR 188 · BRAKE 181 · EMERGENCY_BRAKE 179 · CONSIDER_LANE_CHANGE 0 |
| Ground-truth frames | unsafe headway 2,019 · tailgated 312 · near miss 177 · collision 5 |
| Radar lead tracking (true lead 3–40 m ahead) | motorcycle 99.8%, autorickshaw 100%, car 99.2%, truck 97.8%; median range error 0.2 m (motorcycle), 0.9 m (car), 2.2 m (truck) |
| Storage | 41 MB for the campaign: 15 MB database, 26 MB run files (ground truth dominates), 17 MB export |
| Export | grouped by profile:family:seed, so a run and its repeat share a split; 7 groups are too few for a validation split (warned) |

**Reproducibility.**
- **Inputs reproduce exactly.** Every repeat had equal parameters and byte-identical world,
  network and routes.
- **Trajectories reproduce bit-for-bit only for `tailgating`.** Elsewhere the Webots ego first
  diverges at 11–49 s of simulated time (a speed 0.007 m/s apart, nothing else differing).
  Through the sample controller's lane-change and braking thresholds the difference grows:
  - **`normal`, `mixed`, `potholes`:** small effect, with the maximum ego deviation 0.2–4.9 m and
    similar outcomes.
  - **`sudden_brake`:** one run struck the braking lead (5 collision frames, after which the ego
    crossed the median) and the repeat stopped 2.1 m short.
  - **`cut_in` / `combined`:** minimum TTC 4.1 s vs 2.2 s, and 2.3 s vs 0.24 s.
- **The cause remains unknown.** Fixed SUMO seeds and disabled radar noise did not eliminate divergence: a pair of runs with all radar noise
  set to zero diverged the same way, and the distance sensors have none. The responsible subsystem has not been established.
- **Consequence:** a seed defines a scenario's parameters, not its outcome. Labels stay correct
  because each sample's ground truth comes from its own run. Regression tests should not expect
  bit-identical replays.

**Observations for Phase 7.**
- Heuristic labels are 92% MAINTAIN, and there is no rear-threat label. The 137 tailgated
  ground-truth frames of the tailgating run came with 2 SLOW_DOWN and no other reaction.
- Severe outcomes arise only from staged events, and not every staged event produces one. The
  pilot's `cut_in` (11 m drawn gap) stayed at a 4.1 s TTC, its repeat reached 2.2 s.

**Hundreds of runs.**
- At about 24 s per run, serially: 100 runs take about 40 minutes and 500 runs about 3.3 hours.
- About 3 MB per run plus about 1.2 MB of export per run: 500 runs come to about 2 GB on the Mac.
- Runs are serial, since one fixed server port and one SUMO port are used. Parallel runs would need
  per-run ports, which is not needed at this scale.
- The 5 km road limits runs to roughly 250 s.

## Review correction (Codex, 2026-11-17)

Restored vehicle x/y fields accidentally swallowed by an inline comment after the pilot. The pilot raw files contain those fields. SUMO positions are not identical to physical Webots positions during smoothed lane changes; Phase 6D must record physical poses before using them for supervised labels. The pilot tracking percentages measure lead-slot presence for true leads 3–40 m ahead, not object-level detection accuracy or identity matching. `collision` in v1 means a box-overlap proxy, never measured contact. Repeated executions must receive unique IDs in Phase 6D.
