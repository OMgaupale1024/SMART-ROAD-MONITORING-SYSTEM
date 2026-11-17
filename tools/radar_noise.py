"""Phase 7A.3 P2: the radar's measurement noise in recorded frames, to set the Kalman filter's noise (offline).

    PYTHONPATH=src python3 tools/radar_noise.py CAMPAIGN OUT.json

Each detection (from the captured sensor frames, as the tracker sees them) is paired with the nearest true vehicle
centre within 3 m at the same time. Simulator truth is used here only to measure the sensor, never as a tracker
input. Position residuals are split along and across the line of sight from the radar; their slowly varying part
(where on the body the radar places a vehicle, which depends on the aspect) is separated from the cycle-to-cycle
noise by first differences of consecutive detections of the same vehicle (std / sqrt 2). The range rate is turned
into the target's world speed along the line of sight with the ego velocity, with and without the radar's lever
arm (yaw rate from consecutive poses), and compared with the true velocity along it.
"""
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perception_audit as pa  # noqa: E402
from roadsense.autonomy import perception  # noqa: E402

PAIR_M = 3.0
RANGES = ((0, 25), (25, 50), (50, 100), (100, 150))


def robust(values):
    """median, 1.4826 MAD (a standard deviation that ignores outliers), p95 of |x|, n."""
    if not values:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    ordered = sorted(abs(v) for v in values)
    return {"median": round(med, 3), "robust_sigma": round(1.4826 * mad, 3),
            "p95_abs": round(ordered[int(0.95 * (len(ordered) - 1))], 3), "n": len(values)}


def main(campaign, out):
    pos = defaultdict(list)  # (band, "along"/"across") -> residuals
    step = defaultdict(list)  # (band, axis) -> first differences of consecutive residuals of one vehicle
    rate = defaultdict(list)  # "lever"/"no_lever"/"first_refresh" -> world radial speed residuals
    for line in open(campaign / "campaign.jsonl"):
        record = json.loads(line)
        if not record.get("ok"):
            continue
        run = campaign / "runs" / record["run_id"]
        perception.use_road(record["profile"])
        truth = pa.truth_frames(run / "ground_truth.jsonl")
        last, previous_pose = {}, None
        for frame in map(json.loads, open(run / "sensor_frames.jsonl")):
            t, ego = round(frame["timestamp"], 3), frame["ego"]
            pose = (ego["position"][0], ego["position"][1], ego["yaw_rad"])
            omega = 0.0
            if previous_pose is not None and t > previous_pose[0]:
                omega = math.remainder(pose[2] - previous_pose[1], math.tau) / (t - previous_pose[0])
            previous_pose = (t, pose[2])
            if round(t, 1) not in truth:
                continue
            vehicles = truth[round(t, 1)][1]
            if not vehicles:
                continue
            seen = {}
            for name, data in frame["radars"].items():
                for d in perception.radar_detections(name, data["targets"], data["limits"], t, pose):
                    v = min(vehicles, key=lambda v: math.hypot(v["centre"][0] - d.world_x, v["centre"][1] - d.world_y))
                    if math.hypot(v["centre"][0] - d.world_x, v["centre"][1] - d.world_y) > PAIR_M:
                        continue
                    mx, my = perception.ego_to_world(*perception.RADARS[name][1:3], pose)
                    dx, dy = d.world_x - mx, d.world_y - my
                    r = math.hypot(dx, dy)
                    ux, uy = dx / r, dy / r
                    ex, ey = d.world_x - v["centre"][0], d.world_y - v["centre"][1]
                    band = next("%d-%d m" % b for b in RANGES if b[0] <= r < b[1] + 1e-9)
                    res = (ex * ux + ey * uy, -ex * uy + ey * ux)
                    pos[(band, "along")].append(res[0])
                    pos[(band, "across")].append(res[1])
                    key = (v["id"], name)
                    if key in last and abs(last[key][0] - (t - 0.2)) < 1e-6:
                        step[(band, "along")].append(res[0] - last[key][1][0])
                        step[(band, "across")].append(res[1] - last[key][1][1])
                    first_refresh = key not in last or abs(last[key][0] - (t - 0.2)) > 1e-6
                    seen[key] = (t, res)
                    if d.range_rate_mps is None:
                        continue
                    true_radial = v["v"][0] * ux + v["v"][1] * uy
                    lx, ly = mx - pose[0], my - pose[1]  # the mount relative to the ego origin, world axes
                    lever = (-omega * ly) * ux + (omega * lx) * uy
                    plain = d.range_rate_mps + ego["velocity"][0] * ux + ego["velocity"][1] * uy - true_radial
                    rate["first_refresh" if first_refresh else "no_lever"].append(plain)
                    if not first_refresh:
                        rate["lever"].append(plain + lever)
            last.update(seen)
    result = {"schema": "roadsense.radar-noise.v1", "campaign": campaign.name, "pair_within_m": PAIR_M,
              "position_residual_m": {"%s %s" % k: robust(v) for k, v in sorted(pos.items())},
              "position_noise_from_first_differences_m": {
                  "%s %s" % k: round(statistics.pstdev(v) / math.sqrt(2), 3) for k, v in sorted(step.items()) if len(v) > 10},
              "radial_speed_residual_mps": {k: robust(v) for k, v in sorted(rate.items())},
              "radial_speed_outliers_over_3_mps": {k: round(sum(abs(x) > 3 for x in v) / len(v), 4)
                                                   for k, v in sorted(rate.items())}}
    Path(out).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(json.dumps(result, indent=1, sort_keys=True))


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
