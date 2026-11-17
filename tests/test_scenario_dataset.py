"""Independent labels, censoring and split/input leakage checks for Phase 6D."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import scenario_campaign as campaign
import scenario_dataset as ds
from test_scenarios import car, ego


def frame(vehicles, speed=20):
    return {"t": 1.0, "ego": ego(speed=speed), "vehicles": vehicles}


def test_front_and_rear_targets_are_separate():
    lead = ds.outcomes(frame([car(10, speed=10)]))
    rear = ds.outcomes(frame([car(-3, speed=25)]))
    assert lead["forward_near_miss"] and lead["lead_ttc_s"] == pytest.approx(0.16)
    assert rear["tailgated"] and not rear["forward_near_miss"]
    assert not rear["collision_proxy"]


def test_overlap_is_a_proxy_and_direction_is_explicit():
    front = ds.outcomes(frame([car(7)]))
    rear = ds.outcomes(frame([car(0)]))
    assert front["forward_overlap_proxy"] and front["collision_proxy"]
    assert front["lead_gap_m"] == front["lead_ttc_s"] == 0
    assert rear["collision_proxy"] and not rear["forward_overlap_proxy"]


def test_stationary_close_vehicles_are_not_a_moving_near_miss():
    out = ds.outcomes(frame([car(10, speed=0)], speed=0))
    assert not out["forward_near_miss"] and out["lead_gap_m"] == pytest.approx(1.6)
    assert out["lead_ttc_s"] is None


def test_adjacent_traffic_and_rotated_boxes():
    out = ds.outcomes(frame([car(7, y=12)]))
    assert not out["collision_proxy"] and out["lead_gap_m"] is None
    assert ds.outcomes(frame([car(5, yaw=0.5)]))["collision_proxy"]


def truth(at, positive=False):
    return {"t": at * 0.2, "forward_near_miss": positive, "lead_gap_m": 5,
            "lead_ttc_s": 2, "forward_overlap_proxy": False}


def test_future_window_excludes_present_and_requires_all_15_future_frames():
    future = {i: truth(i) for i in range(16)}
    future[0]["forward_near_miss"] = True
    assert ds.future_label(future, 0)["forward_risk_3s"] == 0
    future[15]["forward_near_miss"] = True
    assert ds.future_label(future, 0)["forward_risk_3s"] == 1
    assert ds.future_label(future, 1) is None
    del future[7]
    assert ds.future_label(future, 0) is None


def test_join_grid_rejects_loose_rounding():
    assert ds.tick(0.400000001) == 2
    with pytest.raises(ValueError):
        ds.tick(0.45)


def test_feature_audit_covers_all_51_and_excludes_rule_and_privileged_inputs():
    audit = ds.audit_features()
    assert len(audit) == 51 and len(ds.INPUTS) == 30
    assert all(audit[k]["selected"] for k in ds.INPUTS)
    assert not {"ego_lane", "ego_lane_change", "derived_min_ttc_s", "derived_conflicts", "history_dropped_before"} & set(ds.INPUTS)
    assert not any(k.startswith(("follower_", "hazard_")) for k in ds.INPUTS)
    assert not any("future" in k or "truth" in k or "risk" in k for k in ds.INPUTS)


def test_plan_is_bounded_reproducible_and_repetitions_share_split():
    p = campaign.plan()
    assert p == campaign.plan() and len(p["tasks"]) == 70
    assert len(p["configurations"]) == 63
    assert set(v["split"] for v in p["assignments"].values()) == {"train", "validation", "test", "stress"}
    for a, b in zip(p["tasks"][:7], p["tasks"][-7:]):
        assert a["configuration_id"] == b["configuration_id"] and a["repetition"] != b["repetition"]
    for a in p["configurations"]:
        for b in p["configurations"]:
            if ds.related(a, b):
                assert p["assignments"][a["configuration_id"]] == p["assignments"][b["configuration_id"]]


def test_export_separates_features_labels_and_baseline_and_reproduces(tmp_path):
    import json
    import sqlite3
    c = tmp_path / "campaign"
    (c / "runs/run-a").mkdir(parents=True)
    (c / "history").mkdir()
    params = campaign.config("normal", 11)
    params["run_id"] = "run-a"
    (c / "runs/run-a/scenario.json").write_text(json.dumps(params))
    plan = {"configurations": [params], "assignments": {
        params["configuration_id"]: {"group": "group-a", "split": "train"}}}
    (c / "plan.json").write_text(json.dumps(plan))
    records = [{"record": "header", "schema": "roadsense.groundtruth.v2", "run_id": "run-a", "scenario": params}]
    records += [dict(frame([car(7 if i == 4 else 10 if i == 20 else 100, speed=10)]), record="frame", t=i * 0.2,
                     missing_physical=[]) for i in range(2, 22)]
    records += [{"record": "end", "t": 4.2, "reason": "duration"}]
    (c / "runs/run-a/ground_truth.jsonl").write_text("\n".join(map(json.dumps, records)) + "\n")
    (c / "campaign.jsonl").write_text(json.dumps({"run_id": "run-a", "ok": True, "gaps": {},
        "server": {"accepted": 22, "missed": 0, "rejected": 0}, "history_status": {"lost": {"queue_full": 0}}}) + "\n")
    conn = sqlite3.connect(c / "history/roadsense-history.sqlite3")
    conn.executescript("CREATE TABLE segments(segment_id TEXT, run_id TEXT);"
                       "CREATE TABLE samples(segment_id TEXT, sequence INTEGER, sim_s REAL, features TEXT, labels TEXT);"
                       "INSERT INTO segments VALUES ('s', 'run-a');")
    features = {n: 0 for n, _, _ in ds.history.FEATURE_COLUMNS}
    features["derived_min_ttc_s"] = 999
    features["ego_lane"] = 123
    conn.executemany("INSERT INTO samples VALUES ('s', ?, ?, ?, ?)",
                     [(i, i * 0.2, json.dumps(features), json.dumps({"recommended_action": "MAINTAIN"})) for i in range(1, 22)])
    # Identical timestamps in another execution must never supply future labels to run-a.
    second = c / "runs/run-b"
    second.mkdir()
    other_params = dict(params, run_id="run-b")
    (second / "scenario.json").write_text(json.dumps(other_params))
    other_truth = [{"record": "header", "schema": "roadsense.groundtruth.v2", "run_id": "run-b", "scenario": other_params}]
    other_truth += [dict(frame([car(100, speed=20)]), record="frame", t=i * 0.2, missing_physical=[]) for i in range(2, 22)]
    other_truth += [{"record": "end", "t": 4.2, "reason": "duration"}]
    (second / "ground_truth.jsonl").write_text("\n".join(map(json.dumps, other_truth)) + "\n")
    campaign_record = json.loads((c / "campaign.jsonl").read_text())
    with (c / "campaign.jsonl").open("a") as output:
        output.write(json.dumps(dict(campaign_record, run_id="run-b")) + "\n")
    conn.execute("INSERT INTO segments VALUES ('other', 'run-b')")
    conn.executemany("INSERT INTO samples VALUES ('other', ?, ?, ?, ?)",
                     [(i, i * 0.2, json.dumps(features), json.dumps({"recommended_action": "MAINTAIN"})) for i in range(1, 22)])
    conn.commit()
    conn.close()
    a, b = c / "out-a", c / "out-b"
    result = ds.export(c, a)
    ds.export(c, b)
    assert result["supervised_samples"] == 7
    assert result["exclusions"] == {"unmatched_truth": 2, "at_or_after_first_overlap_proxy": 18, "incomplete_future_window": 15}
    assert (a / "samples.jsonl").read_bytes() == (b / "samples.jsonl").read_bytes()
    assert (a / "manifest.json").read_bytes() == (b / "manifest.json").read_bytes()
    external = tmp_path / "external-export"
    ds.export(c, external)
    assert (external / "samples.jsonl").read_bytes() == (a / "samples.jsonl").read_bytes()
    import hashlib
    for line in (external / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((c / name).read_bytes()).hexdigest() == digest
    rows = [json.loads(l) for l in (a / "samples.jsonl").read_text().splitlines()]
    assert all(set(row["features"]) == set(ds.INPUTS) for row in rows)
    assert any(row["labels"]["forward_risk_3s"] == 1 for row in rows if row["run_id"] == "run-a")
    assert not any(row["labels"]["forward_risk_3s"] for row in rows if row["run_id"] == "run-b")
    with pytest.raises(FileExistsError):
        ds.export(c, a)


def test_episode_counts_merge_short_threshold_flicker():
    values = [{"t": t, "risk": flag} for t, flag in [(0, True), (.2, False), (.4, True), (1.6, True)]]
    assert ds.episodes(values, "risk") == 2
