"""W10 verification, offline emulation of SUMO->Webots display rules on a logged SUMO run (causal: state k used from step k+1)."""
import json, math, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sensor_profile as sp

def emulate(seq, rule, step):
    """seq: SUMO centre positions (x, y) per step for one vehicle (state k published at Webots time (k+1)*step).
    Returns displayed positions at Webots times (k+2)*step for each k (one value per step)."""
    out, now, vbar = [], None, None
    for k in range(1, len(seq)):
        s, p = seq[k], seq[k - 1]
        move = (s[0] - p[0], s[1] - p[1])
        aim = (s[0] + move[0], s[1] + move[1])
        if now is None:
            now = aim
        if rule == "smooth":  # current plugin: reach S_k + 2 move at the next step
            goal = (aim[0] + move[0], aim[1] + move[1])
        elif rule.startswith("ema"):  # velocity smoothed over steps (alpha), then two-step extrapolation
            a = float(rule[3:])
            vbar = move if vbar is None else (a * move[0] + (1 - a) * vbar[0], a * move[1] + (1 - a) * vbar[1])
            goal = (s[0] + 2 * vbar[0], s[1] + 2 * vbar[1])
        elif rule.startswith("partial"):  # SUMO's velocity plus a partial correction of the pose error
            g = float(rule[7:])
            goal = (now[0] + move[0] + g * (aim[0] + move[0] - now[0] - move[0]),
                    now[1] + move[1] + g * (aim[1] + move[1] - now[1] - move[1]))
        elif rule == "stock":
            goal = s  # moves from S_{k-1} to S_k over the next step (shown at (k+2) step, two steps old)
        now = goal
        out.append(goal)
    return out

def evaluate(run_dir):
    tracks = sp._sumo_tracks(Path(run_dir))
    info = json.loads((Path(run_dir) / "run.json").read_text())
    step = info["sumo_ms"] / 1000.0
    res = {}
    for rule in ("stock", "smooth", "ema0.7", "ema0.5", "ema0.3"):
        err, acc = [], []
        for node, tr in tracks.items():
            segs, cur = [], []
            for (t, v), nxt in zip(tr, tr[1:] + [(None, None)]):
                cur.append((t, v))
                if nxt[1] is None or nxt[1]["id"] != v["id"]:
                    segs.append(cur); cur = []
            for seg in segs:
                if len(seg) < 6:
                    continue
                cen = [(v["x"] - v["length"] / 2 * math.cos(v["yaw"]), v["y"] - v["length"] / 2 * math.sin(v["yaw"])) for _, v in seg]
                yaw = [v["yaw"] for _, v in seg]
                disp = emulate(cen, rule, step)  # disp[j] shown at the time of state j+2
                for j in range(len(disp) - 1):
                    k = j + 3
                    if k >= len(cen) or j < 3:
                        continue
                    e = (disp[j][0] - cen[k][0]) * math.cos(yaw[k]) + (disp[j][1] - cen[k][1]) * math.sin(yaw[k])
                    err.append(abs(e))
                for j in range(4, len(disp) - 1):
                    a = [(disp[j + 1][i] - 2 * disp[j][i] + disp[j - 1][i]) / step ** 2 for i in (0, 1)]
                    acc.append(a[0] * math.cos(yaw[min(j + 2, len(yaw) - 1)]) + a[1] * math.sin(yaw[min(j + 2, len(yaw) - 1)]))
        res[rule] = {"along_err_p50": sp._pct(err, 0.5), "along_err_p95": sp._pct(err, 0.95), "along_err_p99": sp._pct(err, 0.99),
                     "accel_p01": sp._pct(acc, 0.01), "accel_p99": sp._pct(acc, 0.99)}
    return res

for run in sys.argv[1:]:
    print(Path(run).name, json.dumps(evaluate(run)))
