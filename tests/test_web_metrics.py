"""Edge-processing and host measurements (roadsense.web.metrics) and GET /api/system."""
import math
import subprocess

import pytest

pytest.importorskip("fastapi")  # web extra not installed
pytest.importorskip("httpx2")  # dev extra not installed (FastAPI's TestClient needs it)

from fastapi.testclient import TestClient

from roadsense.web import metrics, server
from test_web_live import sensor_frame


def test_percentiles_are_samples_by_nearest_rank():
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert (metrics.percentile(values, 50), metrics.percentile(values, 95), metrics.percentile(values, 100)) == (50, 95, 100)
    assert metrics.percentile([7.0], 95) == 7.0 and metrics.percentile([], 95) is None
    assert metrics.distribution([3.0, 1.0, 2.0]) == {"last": 2.0, "mean": 2.0, "p50": 2.0, "p95": 3.0, "max": 3.0}
    assert metrics.distribution([]) is None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def timing(process_ms):
    """read, validate, process, publish, handler in seconds."""
    return (0.0001, 0.0004, process_ms / 1000.0, 0.0003, process_ms / 1000.0 + 0.001)


def test_stage_timings_are_kept_per_frame_and_summarised_from_the_samples():
    clock = Clock()
    edge = metrics.EdgeMetrics(clock=clock)
    for sequence in range(1, 101):  # process() takes 1..100 ms
        clock.now += 0.2
        edge.accept("run-a", sequence, 0.2 * sequence, True, "edge", timing(sequence))
    summary = edge.summary()
    latency = summary["latency_ms"]
    assert (latency["samples"], latency["first_sequence"], latency["last_sequence"]) == (100, 1, 100)
    assert latency["process"] == {"last": 100.0, "mean": 50.5, "p50": 50.0, "p95": 95.0, "max": 100.0}
    assert latency["validate"]["p95"] == 0.4 and latency["read"]["max"] == 0.1 and latency["publish"]["p50"] == 0.3
    assert latency["handler"]["p95"] == 96.0  # each stage is its own measurement, the handler the whole request
    assert summary["series"]["sequence"][-1] == 100 and summary["series"]["process_ms"][-1] == 100.0
    assert len(summary["series"]["sequence"]) == 100
    assert summary["run"]["run_id"] == "run-a" and summary["run"]["frames"] == 100 and summary["mode"] == "edge"
    assert summary["rate_hz"] == 5.0 and summary["interval_ms"] == {"mean": 200.0, "max": 200.0}
    assert summary["data_age_s"] == 0.0


def test_history_is_bounded():
    edge = metrics.EdgeMetrics(history=50)
    for sequence in range(1, 501):
        edge.accept("run-a", sequence, 0.2 * sequence, True, "edge", timing(1.0))
    latency = edge.summary()["latency_ms"]
    assert (latency["samples"], latency["first_sequence"], latency["last_sequence"]) == (50, 451, 500)


def test_a_new_run_starts_its_own_timings_and_lifetime_counters_go_on():
    clock = Clock()
    edge = metrics.EdgeMetrics(clock=clock)
    for sequence in (1, 2, 3):
        clock.now += 0.2
        edge.accept("run-a", sequence, 0.2 * sequence, True, "edge", timing(50.0))
    clock.now += 30
    edge.accept("run-b", 1, 0.2, True, "edge", timing(2.0))  # the simulation restarted
    summary = edge.summary()
    assert summary["run"]["run_id"] == "run-b" and summary["run"]["frames"] == 1
    assert summary["latency_ms"]["samples"] == 1 and summary["latency_ms"]["process"]["max"] == 2.0  # none of run-a's
    assert summary["frames"] == {"accepted": 4, "rejected": 0, "rejected_by_status": {}, "missed": 0, "runs": 2}


def test_skipped_sequence_numbers_are_missed_frames_and_refusals_are_counted_by_status():
    edge = metrics.EdgeMetrics()
    for sequence in (1, 2, 5, 6, 10):  # the producer could not send 3-4 and 7-9 in time
        edge.accept("run-a", sequence, 0.2 * sequence, True, "edge", timing(1.0))
    for status in (409, 409, 400, 413):
        edge.reject(status)
    summary = edge.summary()
    assert summary["run"]["missed_frames"] == 5 and summary["run"]["frames"] == 5
    assert summary["frames"] == {"accepted": 5, "rejected": 4, "rejected_by_status": {"400": 1, "409": 2, "413": 1},
                                 "missed": 5, "runs": 1}


def test_frame_rate_falls_and_data_ages_when_the_stream_stalls():
    clock = Clock()
    edge = metrics.EdgeMetrics(clock=clock)
    assert edge.summary()["rate_hz"] is None and edge.summary()["data_age_s"] is None
    for sequence in range(1, 51):  # 10 s at 5 Hz
        clock.now += 0.2
        edge.accept("run-a", sequence, 0.2 * sequence, True, "edge", timing(1.0))
    assert edge.summary()["rate_hz"] == 5.0
    clock.now += 2.5  # Webots paused
    stalled = edge.summary()
    assert stalled["rate_hz"] == 2.6 and stalled["data_age_s"] == 2.5  # 13 frames in the last 5 s
    clock.now += 10
    assert edge.summary()["rate_hz"] == 0.0 and edge.summary()["data_age_s"] == 12.5


def test_precomputed_states_are_counted_but_have_no_processing_time():
    edge = metrics.EdgeMetrics()
    edge.accept("run-a", 1, 0.2, True, "precomputed")
    summary = edge.summary()
    assert summary["mode"] == "precomputed" and summary["latency_ms"] is None and summary["series"] is None
    assert summary["frames"]["accepted"] == 1


# --- the host


def linux(root, stat="cpu  100 0 100 800 0 0 0 0 0 0\n", throttled=None):
    for path, text in {
        "proc/stat": stat,
        "proc/meminfo": "MemTotal:        4144816 kB\nMemFree:  900000 kB\nMemAvailable:    3108612 kB\n"
                        "SwapTotal:        524284 kB\nSwapFree:         424284 kB\n",
        "proc/self/statm": "30000 16000 4000 1 0 9000 0\n",
        "proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0\x00",
        "sys/class/thermal/thermal_zone0/temp": "48350\n",
    }.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    if throttled is not None:
        path = root / "sys/devices/platform/soc/soc:firmware/get_throttled"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(throttled)


def no_vcgencmd(*args, **kwargs):
    raise FileNotFoundError("vcgencmd")


def test_host_figures_come_from_the_kernel(tmp_path):
    linux(tmp_path, throttled="50005\n")
    clock = Clock()
    host = metrics.Host(root=str(tmp_path), run=no_vcgencmd, clock=clock)
    first = host.sample()
    assert first["model"] == "Raspberry Pi 5 Model B Rev 1.0"
    assert first["cpu_percent"] is None and "two readings" in first["unavailable"]["cpu_percent"]
    assert first["memory"] == {"total_mb": 4047.7, "available_mb": 3035.8, "used_percent": 25.0, "swap_used_mb": 97.7}
    assert first["temperature_c"] == 48.4  # 48.35 rounds to the nearest tenth above
    assert first["throttling"] == {"raw": "0x50005", "now": ["under_voltage", "throttled"],
                                   "since_boot": ["under_voltage", "throttled"]}
    assert first["process"]["rss_mb"] > 0 and first["process"]["threads"] >= 1

    (tmp_path / "proc/stat").write_text("cpu  160 0 140 900 0 0 0 0 0 0\n")  # 200 jiffies later, 100 of them idle
    clock.now += 0.5
    assert host.sample() is first  # one reading a second, however many dashboards ask
    clock.now += 0.6
    second = host.sample()
    assert second["cpu_percent"] == 50.0 and "cpu_percent" not in second["unavailable"]


def test_throttling_falls_back_to_the_firmware_tool_and_is_read_less_often(tmp_path):
    linux(tmp_path)
    clock, calls = Clock(), []

    def vcgencmd(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="throttled=0x0\n", stderr="")

    host = metrics.Host(root=str(tmp_path), run=vcgencmd, clock=clock)
    assert host.sample()["throttling"] == {"raw": "0x0", "now": [], "since_boot": []}
    for _ in range(3):
        clock.now += 1.1
        host.sample()
    assert calls == [["vcgencmd", "get_throttled"]]  # 3.3 s later: still the reading of 5 s
    clock.now += 2.0
    host.sample()
    assert len(calls) == 2


def test_a_host_without_the_measurements_says_which_and_why(tmp_path):
    host = metrics.Host(root=str(tmp_path), run=no_vcgencmd)  # no /proc or /sys at all, like macOS
    sample = host.sample()
    assert (sample["cpu_percent"], sample["memory"], sample["temperature_c"], sample["throttling"]) == (None,) * 4
    assert set(sample["unavailable"]) >= {"cpu_percent", "memory", "temperature_c", "throttling", "process.rss_mb"}
    assert "Raspberry Pi firmware only" in sample["unavailable"]["throttling"]
    assert all(isinstance(reason, str) and reason for reason in sample["unavailable"].values())
    assert sample["hostname"] and sample["cpu_count"] >= 1 and sample["process"]["uptime_s"] >= 0
    assert not math.isnan(sample["process"]["peak_rss_mb"])


def test_a_failing_firmware_tool_is_reported_not_raised(tmp_path):
    linux(tmp_path)

    def broken(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1.5)

    sample = metrics.Host(root=str(tmp_path), run=broken).sample()
    assert sample["throttling"] is None and "vcgencmd get_throttled failed" in sample["unavailable"]["throttling"]


# --- GET /api/system


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ROADSENSE_AI_EXPLAIN", raising=False)
    with TestClient(server.app) as c:
        yield c


def test_system_endpoint_before_any_frame(client):
    system = client.get("/api/system").json()
    assert system["schema"] == "roadsense.system.v1"
    assert system["app"]["name"] == "RoadSense" and system["app"]["version"] == server.__version__
    assert system["app"]["uptime_s"] >= 0 and system["server_time_unix_s"] > 1_700_000_000
    assert system["edge"]["run"] is None and system["edge"]["latency_ms"] is None and system["edge"]["rate_hz"] is None
    assert system["edge"]["frames"]["accepted"] == 0
    assert system["edge"]["clients"] == {"websocket": 0, "dropped_messages": 0}
    assert system["ai"] == {"enabled": False, "model": "qwen3:0.6b", "availability": "disabled", "status": "disabled",
                            "latency_ms": None}
    assert isinstance(system["host"]["unavailable"], dict) and system["host"]["hostname"]
    # the safety processor's own levels, for the dashboard's reference lines
    assert system["safety"] == {"critical_ttc_s": 1.5, "high_ttc_s": 3.0, "horizon_s": 5.0, "nominal_speed_kmh": 80.0}


def test_system_endpoint_reports_the_measured_stages_of_each_sensor_frame(client):
    for sequence in range(1, 31):
        assert client.post("/api/sensor-frame", json=sensor_frame(sequence, 0.2 * sequence)).status_code == 200
    stale = client.post("/api/sensor-frame", json=sensor_frame(30, 6.0))
    malformed = client.post("/api/sensor-frame", content=b"{}", headers={"Content-Type": "application/json"})
    assert (stale.status_code, malformed.status_code) == (409, 400)

    edge = client.get("/api/system").json()["edge"]
    assert edge["mode"] == "edge"
    assert edge["run"]["run_id"] == "edge-run" and edge["run"]["frames"] == 30 and edge["run"]["last_sequence"] == 30
    assert edge["run"]["missed_frames"] == 0 and edge["run"]["active"] is True and edge["run"]["sim_time_s"] == 6.0
    assert edge["frames"] == {"accepted": 30, "rejected": 2, "rejected_by_status": {"400": 1, "409": 1}, "missed": 0, "runs": 1}
    latency = edge["latency_ms"]
    assert latency["samples"] == 30 and set(latency) >= {"read", "validate", "process", "publish", "handler"}
    for stage in ("validate", "process", "publish", "handler"):
        stats = latency[stage]
        assert 0 < stats["p50"] <= stats["p95"] <= stats["max"], stage
    # the whole request is at least its parts; process() is measured on its own, not derived from the request
    assert latency["handler"]["mean"] >= latency["process"]["mean"] + latency["validate"]["mean"] + latency["publish"]["mean"]
    assert edge["series"]["sequence"] == list(range(1, 31)) and len(edge["series"]["process_ms"]) == 30
    assert edge["data_age_s"] >= 0 and edge["rate_hz"] > 0


def test_the_end_marker_of_a_run_is_counted_without_a_processing_time(client):
    frame = sensor_frame(1, 0.2)
    assert client.post("/api/sensor-frame", json=frame).status_code == 200
    final = dict(frame, simulation={"run_id": "edge-run", "sequence": 2, "active": False})
    assert client.post("/api/sensor-frame", json=final).status_code == 200
    edge = client.get("/api/system").json()["edge"]
    assert edge["run"]["active"] is False and edge["run"]["frames"] == 2 and edge["latency_ms"]["samples"] == 1


def test_oversized_input_is_counted_as_refused(client, monkeypatch):
    monkeypatch.setattr(server.live, "MAX_BYTES", 100)
    assert client.post("/api/sensor-frame", json=sensor_frame()).status_code == 413
    assert client.get("/api/system").json()["edge"]["frames"]["rejected_by_status"] == {"413": 1}
