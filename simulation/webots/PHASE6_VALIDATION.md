# Phase 6 validation — Unified Safety Decision Layer

Validated 2026-10-09 on macOS / Apple Silicon, Webots R2025b Nightly Build 7/10/2026
(`838bb6f`), SUMO 1.27.1. Phase 6 recommendations only; Phase 7 is not started.

## Starting state and scope

Verified clean `main`, HEAD `2ad9551472f511e0aa7b75711ccf3c463f10696e`, matching
`origin/main` and the live remote (`git ls-remote`). Version 0.2.1. Baseline: **186 passed**.
The first sandboxed test attempt had three localhost-bind permission failures; the full suite
passed with socket access. No baseline code fixes were required.

Added `controllers/roadsense_ego/safety.py`, `tests/test_ego_safety.py`, and this report.
Changed `controllers/roadsense_ego/roadsense_ego.py`, this directory's README and
THIRD_PARTY_NOTICES, and the root README. No sensor, tracking, prediction, risk, hazard-map,
world, SUMO, package, version, backend or UI implementation changed. No dependencies added.
The Apache-derived controller modification header and notice table now identify recommendations.
New safety logic and tests are original RoadSense code.

Architecture, policy formulas, action enum, lane gating, input contract and the full plain-dict
UnifiedSafetyState schema are in [README.md](README.md#unified-safety-recommendations-phase-6).

## Actual Webots run

Launched the **repository-owned** `simulation/webots/worlds/roadsense_highway.wbt`, with
SUMO_HOME, PATH and WEBOTS_EXTRA_PROJECT_PATH exactly as documented in the README. Used
`--mode=realtime --batch --stdout --stderr --log-performance=<file>,12000`; stopped gracefully
after the decision at 120 simulated seconds. No simulator files under `/Applications/Webots.app`
were modified. The highway and ego were also visually verified in the native Webots window;
the visible repository demo was left paused.

Observed successful SUMO and roadsense_ego startup, moving ego telemetry, front/rear radar
track reports, trajectory-derived conflicts and TTC, all four pothole discoveries, persistent
hazard-map records and unified decisions. Exactly **120 decision lines**, at simulated seconds
1 through 120, one action per line, all speeds bounded 0–80 km/h. Levels: **102 SAFE,
12 CAUTION, 4 HIGH, 2 CRITICAL**. No traceback or controller error. Known startup warnings:
macOS OpenGL texture warning and SUMO's deprecated `lane` argument.

The existing sample driver still controls the vehicle. At t=21,22,23 s the actual ego speed
remained 80 km/h despite targets of 52.5,36.9,16.0 km/h. An AST regression test additionally
compares all driving statements against `2ad9551` and verifies they are unchanged; the decision
print is inside the existing one-second schedule. There is no safety-to-actuator connection.

## Demo observations

- Safe baseline at t=1 and t=20: MAINTAIN, target 80, no primary threat.
- PH_001 discovered at t=20.4, HIGH severity, 60.0 m ahead. By t=21 it is 46.7 m ahead:
  HIGH/BRAKE, target 52.5. At t=22: 24.5 m, target 36.9. At t=23: 2.2 m, target 16.0.
  At t=24 the threat is SAFE while PH_001 remains ACTIVE in the map, 20.0 m behind.
  The camera's ~60 m range means this pothole is already inside the strong-deceleration
  distance when first seen. A fabricated CAUTION prelude was not added. The unit test with
  a longer-range map view verifies CAUTION → HIGH → SAFE for the same persistent hazard.
- PH_002, MEDIUM in the right lane: CAUTION/MONITOR at t=31–33, target 80; clears at t=34.
- PH_003, LOW in the ego lane: CAUTION/SLOW_DOWN at t=42–44, target 78.2 → 67.9 → 56.0;
  clears at t=45. PH_004 in a further lane is mapped without an active warning.
- Vehicle TRACK_040: SAFE at t=67; CAUTION TTC 4.64 s at t=68, target 69.2;
  CAUTION TTC 3.49 at t=69; HIGH TTC 2.42 at t=70, BRAKE target 58.4;
  CAUTION TTC 3.12 at t=71, target 56.6; SAFE at t=72.
- Natural CRITICAL: TRACK_004 at t=4 (TTC 1.14 s) and t=5 (0.70 s), EMERGENCY_BRAKE
  target zero. That conflict clears at t=6.
- At t=120 all four hazards still exist behind the ego, and the decision is SAFE.
- No simultaneous *active* road and vehicle conflict occurred in this run. Combined threats
  and free/blocked lane suggestions are demonstrated deterministically in unit tests below.

## Combined threats (synthetic, using the real prediction/risk pipeline)

HIGH pothole 25 m ahead plus HIGH TRACK_020 TTC 2.4 s chooses the vehicle and BRAKE.
The pothole also caps the target at 37.3 km/h. CRITICAL TRACK_020 TTC 0.8 s plus a HIGH
pothole 40 m ahead chooses the vehicle and EMERGENCY_BRAKE, target zero, no lane suggestion.
These are test scenarios, not claims about simultaneous threats in the recorded Webots run.

## Regression and performance

`venv/bin/python -m pytest -q`: **264 passed in 7.72 s** (186 existing + 78 new).
The new tests cover all requested hazard, vehicle and combined cases; speed dependence and
bounds; map persistence and non-mutation; free/occupied/fast-closing/stale traffic and swept
lane corridor; deterministic strict JSON; malformed inputs; and unchanged actuation code.
Unit tests require no Webots installation or imports.

Realtime validation: **120 simulated seconds / 129.710 wall seconds including startup**.
Webots's own 12,000-step measurement: **0.950815×**, compared with the supplied Phase 5
baseline ~0.94×. Total ego-controller reported average: 1.339 ms/step (includes controller
scheduling; not an isolated Python decision timer). Traffic timing/noise makes this an
observational comparison, not proof of a speedup.

Isolated decision benchmark on this Mac (Python 3.12): 20 track reports + their assessments,
4 potholes, 10,000 calls repeated five times: **27.70–27.85 microseconds/call** (median 27.82).
At 5 Hz that is about **0.014% of one CPU second**. No material decision overhead observed.
An earlier fast-mode smoke run also succeeded, but its 7.22× factor is not comparable to the
realtime baseline. Raw local logs and timings: `/private/tmp/roadsense-phase6/`; selected
verbatim evidence is retained below so the report does not depend on temporary files.

## Remaining limits

The crossing speeds, reaction/deceleration assumptions and lane clearance require real-world
calibration. Constant-velocity tracks can be noisy; absent tracks do not establish a clear
blind spot. No trajectory planner, sensor-health state, hysteresis, full lane-change safety
proof, rear-threat-specific braking optimization, disk persistence or control is implemented.
Malformed required input raises ValueError rather than manufacturing a SAFE state. Future
sensor adapters must supply current consistent records and handle unavailable sensors.

## Captured console evidence

```text
[RoadSense:DECISION] t=1.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:DECISION] t=4.0s risk=CRITICAL action=EMERGENCY_BRAKE target=0.0km/h threat=TRACK_004 reason=TRACK_004 predicted collision in 1.14 s
[RoadSense:DECISION] t=5.0s risk=CRITICAL action=EMERGENCY_BRAKE target=0.0km/h threat=TRACK_004 reason=TRACK_004 predicted collision in 0.70 s
[RoadSense:DECISION] t=6.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:DECISION] t=20.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:HAZARD_EVENT] t=20.4s discovered PH_001 type=pothole severity=HIGH distance=60.0m lane=ego_lane conf=0.90
[RoadSense:DECISION] t=21.0s risk=HIGH action=BRAKE target=52.5km/h threat=PH_001 reason=PH_001 HIGH pothole 46.7 m ahead in ego_lane
[RoadSense:DECISION] t=22.0s risk=HIGH action=BRAKE target=36.9km/h threat=PH_001 reason=PH_001 HIGH pothole 24.5 m ahead in ego_lane
[RoadSense:DECISION] t=23.0s risk=HIGH action=BRAKE target=16.0km/h threat=PH_001 reason=PH_001 HIGH pothole 2.2 m ahead in ego_lane
[RoadSense:HAZARD] t=24.0s map=1 ahead=0 nearest_ahead=none
  PH_001 pothole behind  20.0m ego_lane   HIGH   conf=0.97 obs=13
[RoadSense:DECISION] t=24.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:HAZARD_EVENT] t=30.8s discovered PH_002 type=pothole severity=MEDIUM distance=59.1m lane=right_lane conf=0.90
[RoadSense:DECISION] t=31.0s risk=CAUTION action=MONITOR target=80.0km/h threat=PH_002 reason=PH_002 MEDIUM pothole 54.5 m ahead in right_lane; monitor only
[RoadSense:DECISION] t=34.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:HAZARD_EVENT] t=42.0s discovered PH_003 type=pothole severity=LOW distance=60.0m lane=ego_lane conf=0.90
[RoadSense:DECISION] t=42.0s risk=CAUTION action=SLOW_DOWN target=78.2km/h threat=PH_003 reason=PH_003 LOW pothole 60.0 m ahead in ego_lane
[RoadSense:DECISION] t=43.0s risk=CAUTION action=SLOW_DOWN target=67.9km/h threat=PH_003 reason=PH_003 LOW pothole 37.8 m ahead in ego_lane
[RoadSense:DECISION] t=44.0s risk=CAUTION action=SLOW_DOWN target=56.0km/h threat=PH_003 reason=PH_003 LOW pothole 15.6 m ahead in ego_lane
[RoadSense:DECISION] t=45.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:HAZARD_EVENT] t=53.2s discovered PH_004 type=pothole severity=MEDIUM distance=61.7m lane=other_lane conf=0.90
[RoadSense:DECISION] t=67.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:DECISION] t=68.0s risk=CAUTION action=SLOW_DOWN target=69.2km/h threat=TRACK_040 reason=TRACK_040 predicted collision in 4.64 s
[RoadSense:DECISION] t=69.0s risk=CAUTION action=SLOW_DOWN target=69.2km/h threat=TRACK_040 reason=TRACK_040 predicted collision in 3.49 s
[RoadSense:DECISION] t=70.0s risk=HIGH action=BRAKE target=58.4km/h threat=TRACK_040 reason=TRACK_040 predicted collision in 2.42 s
[RoadSense:DECISION] t=71.0s risk=CAUTION action=SLOW_DOWN target=56.6km/h threat=TRACK_040 reason=TRACK_040 predicted collision in 3.12 s
[RoadSense:DECISION] t=72.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
[RoadSense:HAZARD] t=120.0s map=4 ahead=0 nearest_ahead=none
  PH_001 pothole behind 2048.7m ego_lane   HIGH   conf=0.97 obs=13
  PH_002 pothole behind 1818.7m right_lane MEDIUM conf=0.96 obs=12
  PH_003 pothole behind 1568.7m ego_lane   LOW    conf=0.97 obs=13
  PH_004 pothole behind 1318.6m other_lane MEDIUM conf=0.96 obs=11
[RoadSense:DECISION] t=120.0s risk=SAFE action=MAINTAIN target=80.0km/h threat=none reason=No current vehicle conflict or relevant road hazard
```

Synthetic combined UnifiedSafetyState (full JSON):

```json
{
  "timestamp": 42.0,
  "overall_risk": "HIGH",
  "primary_threat": {
    "type": "vehicle",
    "id": "TRACK_020",
    "reason": "TRACK_020 predicted collision in 2.40 s; target also bounded by current threats"
  },
  "recommended_action": "BRAKE",
  "recommended_speed_kmh": 37.3,
  "recommended_lane": null,
  "reason": "TRACK_020 predicted collision in 2.40 s; target also bounded by current threats",
  "vehicle_safety": {
    "timestamp": 42.0,
    "overall_risk": "HIGH",
    "most_critical_track": "TRACK_020",
    "minimum_ttc_s": 2.4,
    "active_conflicts": 1
  },
  "road_safety": {
    "relevant_hazards": 1,
    "nearest_hazard": "PH_001",
    "hazards": [
      {
        "hazard_id": "PH_001",
        "type": "pothole",
        "severity": "HIGH",
        "confidence": 0.97,
        "distance_m": 25.0,
        "longitudinal_m": 25,
        "lateral_m": 0,
        "lane_relation": "ego_lane",
        "urgency": "HIGH",
        "time_to_hazard_s": 1.125,
        "recommended_action": "BRAKE",
        "recommended_speed_kmh": 37.3,
        "reason": "PH_001 HIGH pothole 25.0 m ahead in ego_lane"
      }
    ]
  }
}
```
