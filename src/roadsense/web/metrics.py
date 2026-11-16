"""Measurements of the edge processor and of the computer it runs on, for GET /api/system.

Everything here is measured, never estimated: stage durations come from time.perf_counter() stamps taken in the
sensor-frame path, host figures from the kernel (Linux /proc and /sys). What a platform cannot supply is None, with
the reason under "unavailable". The history is bounded and lives in memory only.

Recording a frame is one lock, a few float operations and a deque append; percentiles are computed when the
dashboard asks, in the request's worker thread, never in the sensor-frame path.
"""
from __future__ import annotations

import glob
import math
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover - Windows
    resource = None

HISTORY = 1500  # stage timings kept per run: 5 minutes of 200 ms cycles, about 150 kB
SERIES = 150  # timings handed to the dashboard's latency chart: the last 30 s of cycles
RATE_WINDOW_S = 5.0  # the frame rate is the frames accepted in this long, over this long
STAGES = ("read", "validate", "process", "publish", "handler")

# Raspberry Pi firmware "throttled" word: what is happening now, and what has happened since boot
_THROTTLE_BITS = ((0, "under_voltage"), (1, "frequency_capped"), (2, "throttled"), (3, "soft_temperature_limit"))


def percentile(ordered: Sequence[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (p in 0..100) of values in ascending order: always one of the samples."""
    if not ordered:
        return None
    return ordered[max(0, math.ceil(p / 100.0 * len(ordered)) - 1)]


def distribution(values: Sequence[float], digits: int = 3) -> Optional[Dict[str, float]]:
    """last, mean, p50, p95 and max of the samples (in their own order: last is the newest); None without any."""
    if not values:
        return None
    ordered = sorted(values)
    return {
        "last": round(values[-1], digits),
        "mean": round(sum(ordered) / len(ordered), digits),
        "p50": round(percentile(ordered, 50), digits),
        "p95": round(percentile(ordered, 95), digits),
        "max": round(ordered[-1], digits),
    }


class EdgeMetrics:
    """Counters and stage timings of the live-state hub. Timings and rates describe the current run only, so they
    stay interpretable when the simulation restarts; the counters under "frames" cover the server's lifetime."""

    def __init__(self, history: int = HISTORY, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._samples: deque = deque(maxlen=history)  # (sequence, read, validate, process, publish, handler) in ms
        self._arrivals: deque = deque(maxlen=history)  # when this run's accepted frames arrived (clock)
        self._run: Optional[Dict[str, Any]] = None
        self.accepted = self.missed = self.runs = 0
        self.rejected: Dict[int, int] = {}  # HTTP status -> frames refused with it

    def accept(self, run_id: str, sequence: int, sim_time: float, active: bool, mode: str,
               timing: Optional[Tuple[float, float, float, float, float]] = None) -> None:
        """A frame became the latest state. mode is "edge" (computed here from sensors) or "precomputed" (computed
        by the producer); timing is the STAGES' durations in seconds, known only for frames processed here."""
        now = self._clock()
        with self._lock:
            run = self._run
            if run is None or run["run_id"] != run_id:
                self.runs += 1
                self._samples.clear()
                self._arrivals.clear()
                run = self._run = {"run_id": run_id, "first_sequence": sequence, "frames": 0, "missed_frames": 0,
                                   "started_at_unix_s": round(time.time(), 3), "first_arrival": now}
            elif sequence > run["last_sequence"] + 1:  # the producer skipped frames it could not send in time
                run["missed_frames"] += sequence - run["last_sequence"] - 1
                self.missed += sequence - run["last_sequence"] - 1
            run.update(last_sequence=sequence, sim_time_s=sim_time, active=active, mode=mode)
            run["frames"] += 1
            self.accepted += 1
            self._arrivals.append(now)
            if timing is not None:
                self._samples.append((sequence,) + tuple(1000.0 * seconds for seconds in timing))

    def reject(self, status: int) -> None:
        with self._lock:
            self.rejected[status] = self.rejected.get(status, 0) + 1

    def summary(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            samples = list(self._samples)
            arrivals = list(self._arrivals)
            run = dict(self._run) if self._run else None
            rejected = dict(self.rejected)
            accepted, missed, runs = self.accepted, self.missed, self.runs
        result: Dict[str, Any] = {
            "mode": run["mode"] if run else None,
            "run": None,
            "frames": {"accepted": accepted, "rejected": sum(rejected.values()),
                       "rejected_by_status": {str(status): count for status, count in sorted(rejected.items())},
                       "missed": missed, "runs": runs},
            "rate_hz": None, "interval_ms": None, "data_age_s": None, "latency_ms": None, "series": None,
        }
        if run is None:
            return result
        first_arrival = run.pop("first_arrival")
        result["run"] = run
        result["data_age_s"] = round(now - arrivals[-1], 3)
        recent = [t for t in arrivals if now - t <= RATE_WINDOW_S]
        # frames in the window over the window (or over the run so far, while it is shorter): falls to 0 on a stall
        result["rate_hz"] = round(len(recent) / max(min(RATE_WINDOW_S, now - first_arrival), 0.2), 2)
        gaps = [1000.0 * (b - a) for a, b in zip(recent, recent[1:])]
        if gaps:
            result["interval_ms"] = {"mean": round(sum(gaps) / len(gaps), 1), "max": round(max(gaps), 1)}
        if samples:
            columns = list(zip(*samples))
            result["latency_ms"] = dict(
                {stage: distribution(columns[i + 1]) for i, stage in enumerate(STAGES)},
                samples=len(samples), first_sequence=samples[0][0], last_sequence=samples[-1][0])
            tail = samples[-SERIES:]
            result["series"] = {"sequence": [s[0] for s in tail],
                                "process_ms": [round(s[3], 3) for s in tail],
                                "handler_ms": [round(s[5], 3) for s in tail]}
        return result


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None


def decode_throttled(word: int) -> Dict[str, Any]:
    """The Raspberry Pi firmware's throttling word as lists of conditions: now, and at any time since boot."""
    return {"raw": "0x%x" % word,
            "now": [name for bit, name in _THROTTLE_BITS if word >> bit & 1],
            "since_boot": [name for bit, name in _THROTTLE_BITS if word >> (bit + 16) & 1]}


class Host:
    """Load of the computer this server runs on. sample() reads the kernel at most once per interval_s (CPU use is
    the change between two readings), so any number of dashboards share one reading. root is the filesystem root,
    replaced in tests; run executes the Raspberry Pi firmware tool where sysfs has no throttling state."""

    def __init__(self, root: str = "/", interval_s: float = 1.0, throttle_interval_s: float = 5.0,
                 run=subprocess.run, clock=time.monotonic) -> None:
        self._root, self._interval_s, self._throttle_interval_s = Path(root), interval_s, throttle_interval_s
        self._run, self._clock = run, clock
        self._lock = threading.Lock()
        self._sample: Optional[Dict[str, Any]] = None
        self._sampled_at = -math.inf
        self._cpu: Optional[Tuple[int, int]] = None  # the previous (idle, total) jiffies
        self._process: Optional[Tuple[float, float]] = None  # the previous (clock, process CPU seconds)
        self._throttle: Tuple[Optional[Dict[str, Any]], str] = (None, "not read yet")
        self._throttle_at = -math.inf
        self._vcgencmd = True  # until the tool turns out to be missing
        self._started = clock()
        model = _read(self._root / "proc/device-tree/model")
        self.identity = {
            "hostname": socket.gethostname(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "model": model.strip("\x00\n ") if model else None,  # e.g. "Raspberry Pi 5 Model B Rev 1.0"
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        }

    def sample(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            if self._sample is None or now - self._sampled_at >= self._interval_s:
                self._sample = self._measure(now)
                self._sampled_at = now
            return self._sample

    def _measure(self, now: float) -> Dict[str, Any]:
        missing: Dict[str, str] = {}
        linux_only = "needs the Linux kernel's /proc and /sys; this host is %s" % platform.system()

        cpu_percent = None
        stat = _read(self._root / "proc/stat")
        if stat is None:
            missing["cpu_percent"] = linux_only
        else:
            fields = [int(value) for value in stat.splitlines()[0].split()[1:9]]  # user .. steal
            idle, total = fields[3] + fields[4], sum(fields)
            if self._cpu is not None and total > self._cpu[1]:
                cpu_percent = round(100.0 * (1.0 - (idle - self._cpu[0]) / (total - self._cpu[1])), 1)
            else:
                missing["cpu_percent"] = "needs two readings; the next request has it"
            self._cpu = (idle, total)

        memory = None
        meminfo = _read(self._root / "proc/meminfo")
        if meminfo is None:
            missing["memory"] = linux_only
        else:
            kb = {line.split(":")[0]: int(line.split()[1]) for line in meminfo.splitlines() if ":" in line}
            total_kb, available_kb = kb.get("MemTotal"), kb.get("MemAvailable")
            if total_kb and available_kb is not None:
                memory = {"total_mb": round(total_kb / 1024.0, 1), "available_mb": round(available_kb / 1024.0, 1),
                          "used_percent": round(100.0 * (1.0 - available_kb / total_kb), 1),
                          "swap_used_mb": round((kb.get("SwapTotal", 0) - kb.get("SwapFree", 0)) / 1024.0, 1)}
            else:
                missing["memory"] = "/proc/meminfo has no MemTotal and MemAvailable"

        temperature = None
        millidegrees = _read(self._root / "sys/class/thermal/thermal_zone0/temp")
        try:
            temperature = round(int(millidegrees) / 1000.0, 1)
        except (TypeError, ValueError):
            missing["temperature_c"] = "no thermal sensor at /sys/class/thermal/thermal_zone0 on this host"

        if now - self._throttle_at >= self._throttle_interval_s:
            self._throttle, self._throttle_at = self._read_throttled(), now
        throttling, reason = self._throttle
        if throttling is None:
            missing["throttling"] = reason

        try:
            load_avg: Optional[List[float]] = [round(value, 2) for value in os.getloadavg()]
        except (AttributeError, OSError):
            load_avg = None
            missing["load_avg"] = "this platform has no load average"

        times = os.times()
        cpu_seconds = times.user + times.system
        process_cpu = None
        if self._process is not None and now > self._process[0]:
            process_cpu = round(100.0 * (cpu_seconds - self._process[1]) / (now - self._process[0]), 1)
        self._process = (now, cpu_seconds)
        rss_mb = None
        statm = _read(self._root / "proc/self/statm")
        if statm is None:
            missing["process.rss_mb"] = linux_only
        else:
            rss_mb = round(int(statm.split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576.0, 1)
        peak_rss_mb = None
        if resource is not None:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS, kilobytes elsewhere
            peak_rss_mb = round(peak / (1048576.0 if sys.platform == "darwin" else 1024.0), 1)

        return dict(self.identity, cpu_percent=cpu_percent, load_avg=load_avg, memory=memory,
                    temperature_c=temperature, throttling=throttling,
                    process={"cpu_percent": process_cpu, "rss_mb": rss_mb, "peak_rss_mb": peak_rss_mb,
                             "threads": threading.active_count(), "uptime_s": round(now - self._started, 1)},
                    unavailable=missing)

    def _read_throttled(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """The Raspberry Pi firmware's throttling state: from sysfs, else from `vcgencmd get_throttled`."""
        for path in glob.glob(str(self._root / "sys/devices/platform/soc*/*firmware*/get_throttled")):
            text = _read(Path(path))
            try:
                return decode_throttled(int(text.strip(), 16)), ""
            except (AttributeError, ValueError):
                continue
        if not self._vcgencmd:
            return None, "Raspberry Pi firmware only: no get_throttled in sysfs and no vcgencmd on this host"
        try:
            done = self._run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=1.5)
            return decode_throttled(int(done.stdout.strip().split("=")[-1], 16)), ""
        except FileNotFoundError:
            self._vcgencmd = False  # not a Raspberry Pi OS: don't look for it again
            return None, "Raspberry Pi firmware only: no get_throttled in sysfs and no vcgencmd on this host"
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
            return None, "vcgencmd get_throttled failed: %s" % (str(error) or type(error).__name__)
