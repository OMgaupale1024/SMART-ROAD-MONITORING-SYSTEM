"""Phase 7A.3 Stage 15: the corrected campaign's plan (tools/phase7a3_stage15.py), without SUMO or Webots."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import phase7a3_stage15 as s15  # noqa: E402


@pytest.fixture(scope="module")
def plan():
    return s15.plan()


def test_the_plan_is_fixed_by_its_seeds(plan):
    assert plan == s15.plan() and plan["attempt_limit"] == len(plan["configurations"])
    assert len({r["params"]["configuration_id"] for r in plan["configurations"]}) == plan["attempt_limit"]
    assert plan["adequacy"] == {"frames_25": 2000, "vehicles_25": 100, "runs_25": 10, "frames_30": 200}  # Stage 9's


def test_traffic_from_behind_draws_level_with_the_planned_ego_during_the_run(plan):
    for row in plan["configurations"]:
        p = row["params"]
        cruise, by_lane = p["ego_speed_kmh"] / 3.6, {}
        for v in p["planned_traffic"]:
            assert v["lane"] in s15.LANES
            by_lane.setdefault(v["lane"], []).append(v["depart_s"])
            if v["pos_m"] == 0.0:  # entering behind the ego: it catches the ego while the run lasts
                assert v["speed_mps"] > cruise
                catch = next((k / 10 for k in range(int(p["duration_s"] * 10))
                              if v["speed_mps"] * max(0.0, k / 10 - v["depart_s"]) >= s15.ego_at(k / 10, cruise)), None)
                assert catch is not None and catch <= p["duration_s"] - 20
            else:
                assert v["depart_s"] == 0.0 and v["pos_m"] >= 110 and v["speed_mps"] < cruise
        for departs in by_lane.values():  # one insertion at a time at the edge start
            behind = sorted(d for d in departs if d > 0)
            assert all(b - a >= 1.2 - 1e-9 for a, b in zip(behind, behind[1:]))


def test_recording_refuses_without_the_approved_plan(tmp_path, plan):
    import json
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")
    with pytest.raises(SystemExit, match="not approved"):
        s15.run("feasibility", path, approved="0" * 64)


def test_the_count_keeps_present_totals_apart_from_its_speed_bands():
    out = s15.count({})
    assert out["present_all_speeds"] == {"frames": 0, "vehicles": 0, "runs": 0} and out["present"] == {}
