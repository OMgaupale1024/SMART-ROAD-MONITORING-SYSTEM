"""Phase 7A.3 P2: the opt-in Kalman estimator, Tracker(initiation="range_rate", estimator="kalman")."""
import math

import pytest

import test_tracking_p1 as t1
from roadsense.autonomy import perception, tracking
from roadsense.autonomy.perception import Detection


@pytest.fixture(autouse=True)
def lht_road():
    perception.use_road("highway_lht")  # carriageway heading +x
    yield
    perception.use_road("highway")


@pytest.fixture
def kalman(monkeypatch):
    original = tracking.Tracker
    monkeypatch.setattr(tracking, "Tracker", lambda initiation: original(initiation, "kalman"))


def test_the_default_and_the_pipeline_stay_on_the_window_estimator():
    assert tracking.Tracker().estimator == "window"
    with pytest.raises(ValueError):
        tracking.Tracker("legacy", "kalman")  # P2 starts from P1's estimate; never on its own
    with pytest.raises(ValueError):
        tracking.Tracker("range_rate", "particle")


@pytest.mark.parametrize("noisy", [False, True])
@pytest.mark.parametrize("ego", [11.0, 22.0])
@pytest.mark.parametrize("speed", t1.SPEEDS)
def test_t1_every_speed_confirms_within_0_6_s(kalman, noisy, ego, speed):
    confirmed, _ = t1.drive("range_rate", ego, [("car", (40.0, 6.88), (speed, 0.0))], noisy=noisy)
    assert confirmed.get("car", math.inf) <= 0.6 + 1e-9


def test_a_lane_change_speed_is_followed(kalman):
    confirmed, tracker = t1.drive("range_rate", 22.0, [("car", (30.0, 3.13), (30.0, 1.0))], noisy=True, seconds=3.0)
    (track,) = tracker.confirmed
    assert confirmed["car"] <= 0.6 + 1e-9 and abs(track.velocity[1] - 1.0) < 0.3


def test_irregular_timestamps_gaps_and_repeats_keep_a_finite_consistent_filter():
    def at(t):
        return Detection(t, "radar_front", 0.0, 0.0, 0.0, 0.0, None, 20.0 * t, 3.0 + 0.5 * t)
    times = [0.0, 0.17, 0.41, 0.6, 1.4, 1.58, 1.58, 1.83, 2.0, 2.19, 2.4]  # jitter, a 0.8 s gap, a repeat
    track = tracking.KalmanTrack(at(0.0), None, None)
    for t in times[1:]:
        track.update(at(t))
    state, cov = track.predicted(1.0)  # never predicts backwards
    assert state == track.state
    assert all(math.isfinite(v) for row in cov for v in row)
    assert all(cov[i][i] > 0 and abs(cov[i][j] - cov[j][i]) < 1e-12 for i in range(4) for j in range(4))
    assert abs(track.velocity[0] - 20.0) < 0.5 and abs(track.velocity[1] - 0.5) < 0.2
