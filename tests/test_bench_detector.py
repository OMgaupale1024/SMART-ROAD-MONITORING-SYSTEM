"""tools/bench_detector.py's timing maths; the detector loop itself needs the vision extra and a video."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "bench_detector", Path(__file__).resolve().parents[1] / "tools/bench_detector.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_roi_crops_the_part_above_the_road():
    assert bench.roi_top_px(720, 0.0) == 0
    assert bench.roi_top_px(720, 0.5) == 360
    with pytest.raises(ValueError):
        bench.roi_top_px(720, 1.0)


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))  # 1..100
    assert (bench.percentile(values, 50), bench.percentile(values, 95), bench.percentile(values, 100)) == (50, 95, 100)
    assert bench.percentile([7.0], 95) == 7.0


def test_summary_compares_throughput_with_the_video():
    s = bench.summarize([40.0, 50.0, 60.0, 50.0], wall_s=0.25, source_fps=30)
    assert s["latency_ms"] == {"mean": 50.0, "p50": 50.0, "p95": 60.0, "max": 60.0}
    assert (s["detector_fps"], s["pipeline_fps"], s["keeps_up_with_video"]) == (20.0, 16.0, False)
    assert bench.summarize([10.0] * 4, wall_s=0.1, source_fps=30)["keeps_up_with_video"] is True
    assert bench.summarize([], 0.0, 30) == {"frames": 0}
