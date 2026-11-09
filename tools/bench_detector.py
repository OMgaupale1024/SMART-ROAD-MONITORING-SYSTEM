"""Edge-vision benchmark — run an object/pothole detector over a recorded road video and measure its speed.

    python tools/bench_detector.py drive.mp4                                  # yolo11n.pt at 320 px
    python tools/bench_detector.py drive.mp4 --model yolo11n_ncnn_model       # NCNN export: fastest on the Pi 5 CPU
    python tools/bench_detector.py drive.mp4 --roi-top 0.5 --out annotated.mp4 --json result.json

Run it on the Raspberry Pi to see whether a model keeps up with the camera before building on it. Export a model to
NCNN once with: yolo export model=yolo11n.pt format=ncnn imgsz=320
--roi-top crops the frame to the road (e.g. 0.5 = lower half) so small potholes get more of the model's pixels.
The first --warmup frames are timed separately (model load, caches) and left out of the numbers.
Needs the vision extra: pip install -e ".[vision]". Ctrl+C stops early and still prints the result.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


def roi_top_px(height: int, roi_top: float) -> int:
    """First image row kept when cropping the frame to the road: roi_top is the fraction of the height cut off above."""
    if not 0.0 <= roi_top < 1.0:
        raise ValueError("roi_top must be in [0, 1)")
    return int(height * roi_top)


def percentile(values, p):
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def summarize(latencies_ms, wall_s, source_fps):
    """Timing numbers for the measured frames: per-frame detector latency and whole-loop throughput (decode +
    detect + optional annotation), and whether that throughput keeps up with the video's own frame rate."""
    frames = len(latencies_ms)
    if not frames:
        return {"frames": 0}
    pipeline_fps = frames / wall_s if wall_s > 0 else None
    return {
        "frames": frames,
        "latency_ms": {"mean": round(sum(latencies_ms) / frames, 2), "p50": round(percentile(latencies_ms, 50), 2),
                       "p95": round(percentile(latencies_ms, 95), 2), "max": round(max(latencies_ms), 2)},
        "detector_fps": round(1000 * frames / sum(latencies_ms), 1),
        "pipeline_fps": round(pipeline_fps, 1) if pipeline_fps else None,
        "source_fps": round(source_fps, 1) if source_fps else None,
        "keeps_up_with_video": bool(pipeline_fps and source_fps and pipeline_fps >= source_fps),
    }


def pi_health():
    """CPU temperature (°C) and the Pi firmware's throttle flags; None where this machine has neither."""
    temp = Path("/sys/class/thermal/thermal_zone0/temp")
    throttled = None
    if shutil.which("vcgencmd"):
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout.strip()
        throttled = out.partition("=")[2] or None  # "0x0" = never throttled since boot
    return {"cpu_temp_c": round(int(temp.read_text()) / 1000, 1) if temp.exists() else None, "throttled": throttled}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/bench_detector.py", description=__doc__.split("\n")[0])
    parser.add_argument("video", help="recorded road video (mp4, mov, ...)")
    parser.add_argument("--model", default="yolo11n.pt", help="Ultralytics model: .pt, an *_ncnn_model dir, .onnx")
    parser.add_argument("--imgsz", type=int, default=320, help="detector input size in px (default 320)")
    parser.add_argument("--conf", type=float, default=0.25, help="minimum detection confidence (default 0.25)")
    parser.add_argument("--roi-top", type=float, default=0.0, help="fraction of the frame height cut off above")
    parser.add_argument("--frames", type=int, default=0, help="stop after this many measured frames (0 = all)")
    parser.add_argument("--warmup", type=int, default=10, help="frames run before measuring (default 10)")
    parser.add_argument("--out", help="write an annotated video here (slows the loop: compare without it)")
    parser.add_argument("--json", help="also write the result as JSON here")
    args = parser.parse_args(argv)

    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as error:
        print("missing %s: pip install -e \".[vision]\"" % error.name, file=sys.stderr)
        return 1

    capture = cv2.VideoCapture(args.video)
    if not capture.isOpened():
        print("cannot open video %s" % args.video, file=sys.stderr)
        return 1
    source_fps = capture.get(cv2.CAP_PROP_FPS) or None
    model = YOLO(args.model, task="detect")
    writer, latencies, counts, seen = None, [], collections.Counter(), 0
    warmup_s = wall_start = None
    health_start = pi_health()
    started = time.perf_counter()
    try:
        while not args.frames or len(latencies) < args.frames:
            ok, frame = capture.read()
            if not ok:
                break
            if seen == args.warmup:  # measuring starts with this frame
                warmup_s, wall_start = time.perf_counter() - started, time.perf_counter()
            top = roi_top_px(frame.shape[0], args.roi_top)
            t0 = time.perf_counter()
            result = model.predict(frame[top:], imgsz=args.imgsz, conf=args.conf, verbose=False)[0]
            latency_ms = (time.perf_counter() - t0) * 1000
            if seen >= args.warmup:
                latencies.append(latency_ms)
                counts.update(result.names[int(c)] for c in result.boxes.cls)
            if args.out:
                frame[top:] = result.plot()
                if top:
                    cv2.line(frame, (0, top), (frame.shape[1], top), (0, 255, 255), 2)
                if writer is None:
                    writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), source_fps or 30,
                                             (frame.shape[1], frame.shape[0]))
                writer.write(frame)
            seen += 1
    except KeyboardInterrupt:
        pass
    finally:
        capture.release()
        if writer is not None:
            writer.release()

    wall_s = time.perf_counter() - wall_start if wall_start is not None else 0.0
    report = {
        "video": args.video, "model": args.model, "imgsz": args.imgsz, "conf": args.conf, "roi_top": args.roi_top,
        "annotating": bool(args.out), "warmup_frames": min(seen, args.warmup),
        "warmup_s": round(warmup_s, 2) if warmup_s is not None else None,
        **summarize(latencies, wall_s, source_fps),
        "detections": dict(counts.most_common()),
        "machine": "%s %s, Python %s" % (platform.machine(), platform.platform(), platform.python_version()),
        "pi_health": {"start": health_start, "end": pi_health()},
    }
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
    if not latencies:
        print("no frames measured: the video has %d frame(s), --warmup is %d" % (seen, args.warmup), file=sys.stderr)
        return 1
    lat = report["latency_ms"]
    print("%s @ %dpx, %d frames: detector %.1f ms mean / %.1f p95 (%.1f FPS), pipeline %s FPS, video %s FPS -> %s"
          % (args.model, args.imgsz, report["frames"], lat["mean"], lat["p95"], report["detector_fps"],
             report["pipeline_fps"], report["source_fps"],
             "keeps up" if report["keeps_up_with_video"] else "does NOT keep up (frames would be dropped)"))
    print("detections: %s" % (", ".join("%s=%d" % kv for kv in counts.most_common()) or "none"))
    end = report["pi_health"]["end"]
    if end["cpu_temp_c"] is not None or end["throttled"] is not None:
        print("pi: cpu %s°C -> %s°C, throttled=%s" % (health_start["cpu_temp_c"], end["cpu_temp_c"], end["throttled"]))
        if end["throttled"] not in (None, "0x0"):
            print("warning: the Pi throttled (power or heat); numbers are lower than it can do", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
