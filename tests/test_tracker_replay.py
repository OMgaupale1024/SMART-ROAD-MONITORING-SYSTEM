"""The evaluation-only TruthOracle of tools/tracker_replay.py (Phase 7A.3 Stage 8)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import tracker_replay  # noqa: E402
from roadsense.autonomy.perception import Detection  # noqa: E402


def test_the_oracle_reports_detected_vehicles_at_their_truth_and_coasts_like_a_tracker():
    truth = {round(0.2 * k, 1): ({}, [{"id": "a", "centre": (10.0 + k, 2.0), "v": (5.0, 0.0)},
                                      {"id": "never_detected", "centre": (40.0, 2.0), "v": (5.0, 0.0)}])
             for k in range(30)}
    oracle, reported = tracker_replay.TruthOracle(truth), []
    for k in range(30):
        t = round(0.2 * k, 1)
        detections = [Detection(t, "radar_front", 0, 0, 0, 0, None, 11.0 + k, 2.5)] if k < 10 else []
        oracle.update(t, detections)
        reported.append([(track.track_id, track.position_at(t), track.velocity) for track in oracle.confirmed])
    assert reported[5] == [("TRUTH_a", (15.0, 2.0), (5.0, 0.0))]  # the truth, not the detection
    # seen over 1.8 s, so kept for 1.8 s after its last detection (t 3.6 s), as a tracker coasts it, then dropped
    assert all(len(r) == 1 for r in reported[:19]) and all(r == [] for r in reported[19:])
