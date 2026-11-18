"""Phase 6C: generate, run and record randomized Webots/SUMO scenarios on the Mac (docs/scenarios.md).

    PYTHONPATH=src venv/bin/python tools/scenario_batch.py pilot --campaign pilot-20261117
    PYTHONPATH=src venv/bin/python tools/scenario_batch.py run --campaign NAME --family cut_in --seeds 1-20
    PYTHONPATH=src venv/bin/python tools/scenario_batch.py export --campaign NAME

Each run: scenario.json from (family, seed); a generated world and SUMO network in the campaign directory (never in
the repository, never /tmp); a local RoadSense server of its own (dataset mode, no Ollama, the run's road profile;
never the Raspberry Pi); Webots in fast batch mode until the scenario's plugin quits; then the ground truth is
summarised and one line appended to campaign.jsonl.
"""
import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import hashlib
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WEBOTS = os.environ.get("ROADSENSE_WEBOTS", "/Applications/Webots.app/Contents/MacOS/webots")
WORLD = ROOT / "simulation/webots/worlds/roadsense_highway.wbt"
NET_DIR = ROOT / "simulation/webots/worlds/roadsense_highway_net"
SERVER_PORT = 8011  # never 8000 (the development server) or 8001 (the Pi)
SUMO_PORT = 8901  # never 8873, the port of an open Webots with the repository world
PROFILE_OF_TRAFFIC = {"left": "highway_lht", "right": "highway"}


def campaign_root():
    configured = os.environ.get("ROADSENSE_CAMPAIGN_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/RoadSense/campaigns"
    else:
        root = Path.home() / ".local/share/roadsense/campaigns"
    resolved = root.resolve()
    if ROOT in resolved.parents or resolved == ROOT or str(resolved).startswith(("/tmp", "/private/tmp")):
        sys.exit("campaign data must stay outside the repository and /tmp: %s" % resolved)
    return resolved


def free(port):
    """Nothing listens on the port (a closed socket left in TIME_WAIT by the previous run does not count)."""
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def prepare(run_dir, params, sumo_port):
    """Write the run's world, network, plugin and scenario; returns the world's path."""
    worlds = run_dir / "worlds"
    net = worlds / "scenario_net"
    net.mkdir(parents=True)
    (worlds / "scenario.wbt").write_text(scenarios.world(WORLD.read_text(), params, sumo_port))
    (net / "sumo.net.xml").write_text(scenarios.network((NET_DIR / "sumo.net.xml").read_text(), params))
    (net / "sumo.rou.xml").write_text(scenarios.routes(params))
    (net / "sumo.sumocfg").write_text((NET_DIR / "sumo.sumocfg").read_text())
    (net / "plugin.py").write_text("import sys\nsys.path.insert(0, %r)\nfrom scenario_plugin import SumoSupervisorPlugin"
                                   "  # noqa\n" % str(ROOT / "tools"))
    for name, target in (("forest", WORLD.parent / "forest"),):
        (worlds / name).symlink_to(target)
    (run_dir / "protos").symlink_to(ROOT / "simulation/webots/protos")
    # the world's controllers: RoadSense's ego, and the sample's radar display that Webots only finds in a project
    controllers = run_dir / "controllers"
    controllers.mkdir()
    (controllers / "roadsense_ego").symlink_to(ROOT / "simulation/webots/controllers/roadsense_ego")
    (controllers / "radar_target_tracker").symlink_to(Path(WEBOTS).parents[1] / "projects/vehicles/controllers"
                                                       / "radar_target_tracker")
    (run_dir / "scenario.json").write_text(json.dumps(params, indent=1) + "\n")
    return worlds / "scenario.wbt"


def get(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=5) as r:
        return json.loads(r.read())


def start_server(port, history_dir, profile, log):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")))), PYTHONDONTWRITEBYTECODE="1",
               ROADSENSE_HISTORY_ENABLE="1", ROADSENSE_HISTORY_DATASET="1", ROADSENSE_HISTORY_DIR=str(history_dir),
               ROADSENSE_HISTORY_MAX_MB="2048", ROADSENSE_HISTORY_RETENTION_DAYS="3650",
               ROADSENSE_ROAD_PROFILE=profile)
    env.pop("ROADSENSE_AI_EXPLAIN", None)  # no Ollama while generating data: explanations are not data
    process = subprocess.Popen([sys.executable, "-m", "roadsense.web", "--host", "127.0.0.1", "--port", str(port),
                                "--no-browser"], env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if get("http://127.0.0.1:%d/api/system" % port)["history"]["state"] == "recording":
                return process
        except (OSError, ValueError, KeyError):
            pass
        if process.poll() is not None:
            break
        time.sleep(0.2)
    process.kill()
    process.wait()
    raise RuntimeError("the RoadSense server did not start recording; see server.log")


def stop(process, timeout=15):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def samples_of(history_dir, run_id):
    conn = sqlite3.connect("file:%s?mode=ro" % (history_dir / "roadsense-history.sqlite3"), uri=True)
    try:
        rows = conn.execute("SELECT s.sim_s FROM samples s JOIN segments g ON g.segment_id = s.segment_id "
                            "WHERE g.run_id = ?", (run_id,)).fetchall()
        labels = conn.execute("SELECT json_extract(labels, '$.recommended_action'), count(*) FROM samples s JOIN "
                              "segments g ON g.segment_id = s.segment_id WHERE g.run_id = ? GROUP BY 1",
                              (run_id,)).fetchall()
        tracks = conn.execute("SELECT avg(json_extract(features, '$.track_count')), "
                              "sum(json_extract(features, '$.track_count') > 0) FROM samples s JOIN segments g "
                              "ON g.segment_id = s.segment_id WHERE g.run_id = ?", (run_id,)).fetchone()
        gaps = conn.execute("SELECT kind, coalesce(sum(missing_sequences), 0), count(*) FROM discontinuities "
                            "WHERE run_id = ? AND kind IN ('sequence_gap', 'history_loss') GROUP BY kind",
                            (run_id,)).fetchall()
    finally:
        conn.close()
    return ([r[0] for r in rows], dict(labels), {k: {"missing": m, "records": n} for k, m, n in gaps},
            {"mean_tracks": round(tracks[0] or 0, 2), "samples_with_tracks": tracks[1] or 0})


def size(path):
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file() and not p.is_symlink())


def run_one(campaign, family, seed, profile, tag="", timeout_s=600, repetition=0, parameters=None, capture=False):
    """One run; capture=True also records every raw sensor frame the ego sends (sensor_frames.jsonl, Phase 7A.3)."""
    params = dict(parameters) if parameters is not None else scenarios.draw(family, seed, profile)
    expected_id = params.pop("configuration_id", None)
    if (params["family"], params["seed"], params["profile"]) != (family, seed, profile):
        raise ValueError("scenario identity mismatch")
    params["configuration_id"] = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
    if expected_id is not None and expected_id != params["configuration_id"]:
        raise ValueError("configuration checksum mismatch")
    params["repetition"] = repetition
    params["execution_tag"] = tag
    params["run_id"] = scenarios.run_id(params) + "-" + uuid.uuid4().hex[:12]
    params["execution_id"] = params["run_id"]
    params["source_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    params["source_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    source_paths = sorted((ROOT / "tools").glob("scenario*.py")) + sorted((ROOT / "src/roadsense").rglob("*.py"))
    source_paths += [ROOT / "simulation/webots/controllers/roadsense_ego/roadsense_ego.py"]
    contents = {str(p.relative_to(ROOT)): p.read_bytes() for p in source_paths}
    params["source_sha256"] = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    source_id = hashlib.sha256(json.dumps(params["source_sha256"], sort_keys=True).encode()).hexdigest()[:20]
    snapshot = campaign / "source" / source_id
    if not snapshot.exists():
        for name, data in contents.items():
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    params["source_snapshot"] = str(snapshot.relative_to(campaign))
    params["simulator_versions"] = {"webots": (Path(WEBOTS).parents[1] / "resources/version.txt").read_text().strip(),
                                    "sumo": subprocess.check_output(["sumo", "--version"], text=True).splitlines()[0]}
    params["feature_schema"] = "roadsense.features.v1"
    params["ground_truth_definition"] = "roadsense.forward-risk.v1"
    run_dir = campaign / "runs" / params["run_id"]
    if run_dir.exists():
        raise RuntimeError("%s exists: a run is never overwritten" % run_dir)
    history_dir = campaign / "history"
    port, sumo_port = SERVER_PORT, SUMO_PORT
    if not free(port) or not free(sumo_port):
        raise RuntimeError("port %d or %d is in use" % (port, sumo_port))
    world = prepare(run_dir, params, sumo_port)
    gt_path = run_dir / "ground_truth.jsonl"
    record = {"configuration_id": params["configuration_id"], "repetition": repetition, "run_id": params["run_id"], "family": family, "seed": seed, "profile": profile, "ok": False}
    with open(run_dir / "server.log", "w") as server_log, open(run_dir / "webots.log", "w") as webots_log:
        server = start_server(port, history_dir, profile, server_log)
        try:
            env = dict(os.environ, ROADSENSE_EDGE_URL="http://127.0.0.1:%d/api/sensor-frame" % port,
                       ROADSENSE_RUN_ID=params["run_id"], ROADSENSE_ROAD_PROFILE=profile,
                       ROADSENSE_SCENARIO=str(run_dir / "scenario.json"), ROADSENSE_GROUND_TRUTH=str(gt_path),
                       PYTHONDONTWRITEBYTECODE="1")
            env["ROADSENSE_EGO_SPEED_KMH"] = str(params.get("ego_speed_kmh", 80))
            env.pop("ROADSENSE_FRAME_CAPTURE", None)
            if capture:
                env["ROADSENSE_FRAME_CAPTURE"] = str(run_dir / "sensor_frames.jsonl")
            env.pop("ROADSENSE_LIVE_PUBLISH", None)
            started = time.monotonic()
            webots = subprocess.Popen([WEBOTS, "--batch", "--mode=fast", "--no-rendering", "--stdout", "--stderr",
                                       "--minimize", str(world)], env=env, stdout=webots_log,
                                      stderr=subprocess.STDOUT)
            try:
                webots.wait(timeout_s)
            except subprocess.TimeoutExpired:
                webots.terminate()
                try:
                    webots.wait(10)
                except subprocess.TimeoutExpired:
                    webots.kill()
                    webots.wait()
                record["error"] = "Webots did not finish within %d s" % timeout_s
            record["wall_s"] = round(time.monotonic() - started, 1)
            time.sleep(1.5)  # the controller's end-of-run frame, then a history flush
            edge = get("http://127.0.0.1:%d/api/system" % port)["edge"]
            record["history_status"] = get("http://127.0.0.1:%d/api/system" % port)["history"]
            record["server"] = {"accepted": edge["frames"]["accepted"], "missed": edge["frames"]["missed"],
                                "rejected": edge["frames"]["rejected"], "process_p95_ms": edge["latency_ms"]
                                ["process"]["p95"], "handler_p95_ms": edge["latency_ms"]["handler"]["p95"]}
        finally:
            stop(server)
    errors = [line.strip() for line in open(run_dir / "webots.log", errors="replace")
              if line.startswith(("ERROR", "Traceback")) or "Error" in line[:40]]
    record["webots_errors"] = errors[:10]
    if not gt_path.exists():
        record["error"] = record.get("error") or "no ground truth written"
        return record
    header, frames, events, end = scenarios.read_ground_truth(gt_path)
    summary, outcomes = scenarios.summarize(frames, params)
    with open(run_dir / "outcomes.jsonl", "w") as f:
        for o in outcomes:
            o = dict(o, schema="roadsense.outcomes.descriptive.v2", collision_proxy=o["collision"],
                     near_miss_any_direction=o["near_miss"])
            del o["collision"], o["near_miss"]
            f.write(json.dumps(o, separators=(",", ":")) + "\n")
    sims, labels, gaps, tracks = samples_of(history_dir, params["run_id"])
    if capture:
        frames_path = run_dir / "sensor_frames.jsonl"
        record["sensor_frames"] = {"lines": sum(1 for _ in open(frames_path, "rb")),
                                   "sha256": hashlib.sha256(frames_path.read_bytes()).hexdigest()} \
            if frames_path.exists() else None
    gt_times = {round(f["t"], 1) for f in frames}
    joined = sum(1 for s in sims if round(s, 1) in gt_times)
    record.update(ok=end is not None and not record.get("error") and not errors, sim_s=end["t"] if end else None,
                  end=end and end["reason"], event=end and end["event"], events=[e["what"] for e in events],
                  speed_x=round(end["t"] / record["wall_s"], 2) if end else None,
                  ground_truth=summary, samples=len(sims), samples_joined_to_ground_truth=joined,
                  label_actions=labels, gaps=gaps, radar=tracks, run_bytes=size(run_dir))
    return record


def append(campaign, record):
    with open(campaign / "campaign.jsonl", "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def export(campaign):
    runs = [json.loads(line) for line in open(campaign / "campaign.jsonl")]
    groups = {r["run_id"]: "%s:%s:%d" % (r["profile"], r["family"], r["seed"]) for r in runs}
    (campaign / "groups.json").write_text(json.dumps(groups, indent=1, sort_keys=True) + "\n")
    out = campaign / ("export-%d" % len(list(campaign.glob("export-*"))))
    subprocess.run([sys.executable, "-m", "roadsense.web.history_export", "--dir", str(campaign / "history"), "export",
                    str(out), "--group-map", str(campaign / "groups.json")], check=True,
                   env=dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, (str(ROOT / "src"), os.environ.get("PYTHONPATH", ""))))))
    return out


def seeds(text):
    lo, _, hi = text.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pilot", "run", "export"):
        p = sub.add_parser(name)
        p.add_argument("--campaign", required=True)
        if name != "export":
            p.add_argument("--traffic", choices=("left", "right"), default="left")
            p.add_argument("--seeds", default="1")
        if name == "run":
            p.add_argument("--family", choices=scenarios.FAMILIES, action="append", required=True)
            p.add_argument("--tag", default="", help="execution tag (run identities are always unique)")
    args = parser.parse_args(argv)
    if Path(args.campaign).name != args.campaign or args.campaign in (".", ".."):
        parser.error("campaign must be a simple directory name")
    campaign = campaign_root() / args.campaign
    campaign.mkdir(parents=True, exist_ok=True)
    if args.command == "export":
        print(export(campaign))
        return
    families = scenarios.FAMILIES if args.command == "pilot" else args.family
    for seed in seeds(args.seeds):
        for family in families:
            record = run_one(campaign, family, seed, PROFILE_OF_TRAFFIC[args.traffic], getattr(args, "tag", ""))
            append(campaign, record)
            print(json.dumps({k: record.get(k) for k in ("run_id", "ok", "wall_s", "sim_s", "speed_x", "samples",
                                                         "end", "error")}), flush=True)


if __name__ == "__main__":
    main()
