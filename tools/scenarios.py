"""Phase 6C randomized scenarios (docs/scenarios.md): parameters from a seed, the generated Webots/SUMO files, and
ground-truth outcomes from the simulator's true positions.

Pure Python 3.9 (sumo_supervisor imports it inside Webots); no RoadSense imports, so the ground truth stays
independent of the pipeline it is used to evaluate.
"""
import json
import math
import random
import re
import xml.etree.ElementTree as ET

SCHEMA = "roadsense.scenario.v1"
GT_SCHEMA = "roadsense.groundtruth.v1"
FAMILIES = ("normal", "sudden_brake", "cut_in", "tailgating", "potholes", "mixed", "combined")

# roadsense_highway.wbt: Webots (x, y) = SUMO (x, y) - NET_OFFSET. The ego carriageway is SUMO edge "0", lanes
# 1-3 (lane 0, the kerb lane, is pedestrian-only), centred at these Webots y; lane 1 is the kerb-side driving lane.
NET_OFFSET = (5000.0, 8.75)
LANE_Y = {1: 10.87, 2: 7.03, 3: 3.18}
ROAD_X = (-5000.0, 0.0)
# Per road profile: the ego's start (Webots x, heading) and its direction of travel along x.
PROFILES = {"highway": {"start_x": -40.0, "yaw": math.pi, "direction": -1, "traffic": "right"},
            "highway_lht": {"start_x": -4960.0, "yaw": 0.0, "direction": 1, "traffic": "left"}}
EGO_START_Y = 6.71
EGO_REAR_M, EGO_FRONT_M, EGO_HALF_WIDTH_M = 1.0, 3.9, 0.95  # Lincoln MKZ footprint about its rear-axle origin

# SUMO vehicle types. Webots draws SUMO classes as car, bus, truck, trailer or motorcycle models; there is no
# autorickshaw model, so an autorickshaw is a small slow SUMO "passenger" drawn as a Webots car (see docs).
VTYPES = {
    "car": 'vClass="passenger" length="4.5" width="1.8" accel="2.6" decel="4.5" sigma="0.5" maxSpeed="27"'
           ' speedFactor="normc(1,0.1,0.8,1.2)"',
    "motorcycle": 'vClass="motorcycle" length="2.0" width="0.8" accel="3.5" decel="6" sigma="0.7" maxSpeed="25"'
                  ' minGap="1.5" speedFactor="normc(1.05,0.15,0.8,1.3)" lcAssertive="2"',
    "autorickshaw": 'vClass="passenger" length="2.8" width="1.4" accel="1.2" decel="4" sigma="0.8" maxSpeed="13.9"'
                    ' minGap="1.5" color="0.2,0.6,0.1" speedFactor="normc(1,0.1,0.8,1.1)"',
    "truck": 'vClass="truck" length="8" width="2.5" accel="0.8" decel="4.5" sigma="0.5" maxSpeed="22"'
             ' minGap="6" guiShape="truck"',
    "bus": 'vClass="bus" length="11" width="2.5" accel="0.8" decel="4.5" sigma="0.5" maxSpeed="20" minGap="6"'
           ' guiShape="bus"',
}
INDIAN_MIX = {"car": 0.5, "motorcycle": 0.25, "autorickshaw": 0.12, "truck": 0.09, "bus": 0.04}
SEVERITIES = ("LOW", "MEDIUM", "HIGH")
EVENT_FAMILIES = {"sudden_brake": "sudden_brake", "cut_in": "cut_in", "tailgating": "tailgating"}


def _pothole(rng, at_m, lane=None):
    return {"at_m": round(at_m, 1), "lane": lane or rng.choice((1, 2, 3)), "offset_m": round(rng.uniform(-0.6, 0.6), 2),
            "severity": rng.choice(SEVERITIES), "length_m": round(rng.uniform(0.6, 1.6), 2),
            "width_m": round(rng.uniform(0.5, 1.2), 2), "depth_m": round(rng.uniform(0.03, 0.12), 3),
            "yaw_rad": round(rng.uniform(-0.5, 0.5), 2)}


def _event(rng, kind, at_m):
    if kind == "sudden_brake":
        return {"kind": kind, "at_m": round(at_m, 1), "gap_m": round(rng.uniform(12, 40), 1),
                "decel_mps2": round(rng.uniform(4, 8), 2), "to_kmh": rng.choice((0, 10, 20)),
                "hold_s": round(rng.uniform(3, 8), 1)}
    if kind == "cut_in":
        return {"kind": kind, "at_m": round(at_m, 1), "side": rng.choice(("left", "right")),
                "gap_m": round(rng.uniform(3, 12), 1), "speed_delta_kmh": round(rng.uniform(-10, 5), 1)}
    return {"kind": kind, "at_m": round(at_m, 1), "time_gap_s": round(rng.uniform(0.3, 1.0), 2),
            "duration_s": round(rng.uniform(15, 30), 1)}


def draw(family, seed, profile="highway_lht"):
    """The scenario of (family, seed): every random choice comes from this one seeded generator."""
    if family not in FAMILIES:
        raise ValueError("unknown family %r" % family)
    if profile not in PROFILES:
        raise ValueError("unknown profile %r" % profile)
    rng = random.Random("%s:%s:%d" % (profile, family, seed))  # str seeds are hashed with SHA-512: stable
    duration = round(rng.uniform(120, 180), 1)
    mix = dict(INDIAN_MIX)
    if family == "mixed":
        mix = {"motorcycle": rng.uniform(0.3, 0.45), "autorickshaw": rng.uniform(0.15, 0.25),
               "truck": rng.uniform(0.06, 0.12), "bus": rng.uniform(0.02, 0.06)}
        mix["car"] = 1.0 - sum(mix.values())
    reach = duration * 18.0  # metres the ego can be expected to cover (it drives up to 80 km/h)
    event = None
    potholes = []
    if family in EVENT_FAMILIES:
        event = _event(rng, family, rng.uniform(500, min(1800, reach - 600)))
    elif family == "combined":
        event = _event(rng, rng.choice(("sudden_brake", "cut_in")), rng.uniform(500, min(1800, reach - 600)))
        potholes.append(_pothole(rng, event["at_m"] + rng.uniform(25, 60), lane=None))
    count = {"potholes": rng.randint(4, 10), "combined": rng.randint(1, 3)}.get(family, rng.randint(0, 2))
    potholes += [_pothole(rng, rng.uniform(150, reach - 100)) for _ in range(count)]
    potholes.sort(key=lambda p: p["at_m"])
    return {
        "schema": SCHEMA, "family": family, "seed": seed, "profile": profile,
        "traffic": PROFILES[profile]["traffic"], "duration_s": duration,
        "speed_limit_kmh": rng.choice((60, 70, 80)),
        "density_per_km_lane": round(rng.uniform(6, 16) if family != "normal" else rng.uniform(4, 12), 1),
        "mix": {k: round(v, 3) for k, v in sorted(mix.items())},
        "potholes": potholes, "event": event, "sumo_seed": rng.randint(1, 2 ** 31 - 1),
    }


def run_id(params):
    return "%s-%s-%d" % ("lht" if params["traffic"] == "left" else "rht", params["family"], params["seed"])


def vehicle_length(name):
    return float(ET.fromstring("<vType " + VTYPES[name] + "/>").get("length"))


def vehicle_type(name, params):
    """The SUMO vType attributes of a class, with the scenario's vtype_max_speed_mps override (Phase 7A.3 Stage 9:
    expressway traffic able to exceed 25 m/s). Without the key every vType is exactly VTYPES's."""
    fast = (params.get("vtype_max_speed_mps") or {}).get(name)
    return VTYPES[name] if fast is None else re.sub(r'maxSpeed="[^"]*"', 'maxSpeed="%.1f"' % fast, VTYPES[name])


def vehicle_max_speed(name):
    return float(ET.fromstring("<vType " + VTYPES[name] + "/>").get("maxSpeed"))


def pothole_positions(params):
    """Webots (x, y, yaw) of each pothole: at_m ahead of the ego's start along its direction of travel."""
    p = PROFILES[params["profile"]]
    return [(p["start_x"] + p["direction"] * h["at_m"], LANE_Y[h["lane"]] + h["offset_m"], h["yaw_rad"])
            for h in params["potholes"]]


def pothole_nodes(params):
    """VRML for the supervisor to import: one RoadSensePothole per drawn pothole."""
    nodes = []
    for i, (h, (x, y, yaw)) in enumerate(zip(params["potholes"], pothole_positions(params))):
        nodes.append('DEF RS_POTHOLE_%d RoadSensePothole { translation %.2f %.2f 0.01 rotation 0 0 1 %.3f '
                     'name "pothole %d" severity "%s" size %.2f %.2f %.3f }'
                     % (i, x, y, yaw, i, h["severity"], h["length_m"], h["width_m"], h["depth_m"]))
    return nodes


def network(net_xml, params):
    """The scenario's sumo.net.xml from the world's: lane speeds set, and for left-hand traffic both carriageways
    driven the other way (each edge and lane shape reversed, lefthand="true"). Lane indices keep counting from the
    kerb, so every lane stays where it is."""
    root = ET.fromstring(net_xml)
    speed = "%.2f" % (params["speed_limit_kmh"] / 3.6)
    left = params["traffic"] == "left"
    if left:
        root.set("lefthand", "true")
    junctions = {j.get("id"): j for j in root.iter("junction")}
    for edge in root.iter("edge"):
        if edge.get("function") == "internal":
            continue
        for lane in edge.iter("lane"):
            if lane.get("allow") != "pedestrian":
                lane.set("speed", speed)
            if left:
                lane.set("shape", " ".join(reversed(lane.get("shape").split())))
        if left:
            start, end = edge.get("from"), edge.get("to")
            edge.set("from", end)
            edge.set("to", start)
            edge.set("shape", " ".join(reversed(edge.get("shape").split())))
            lanes = junctions[end].get("incLanes")
            junctions[end].set("incLanes", "")
            junctions[start].set("incLanes", lanes)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def routes(params):
    """sumo.rou.xml: the drawn traffic on both carriageways, clear of the ego's start."""
    rng = random.Random("routes:%d:%s" % (params["seed"], params["family"]))
    names = sorted(params["mix"])
    weights = [params["mix"][n] for n in names]
    lines = ['<routes>', '  <route id="0" edges="0"/>', '  <route id="1" edges="1"/>']
    lines += ['  <vType id="%s" %s/>' % (name, vehicle_type(name, params)) for name in sorted(VTYPES)]
    spacing = 1000.0 / params["density_per_km_lane"]
    vehicles = []
    for edge in ("0", "1"):
        for lane in (1, 2, 3):
            pos = rng.uniform(0, spacing)
            while pos < 4990:
                if not (edge == "0" and abs(pos - 40.0) < 60):  # the ego starts 40 m along edge 0
                    vehicles.append((edge, lane, round(pos, 1), rng.choices(names, weights)[0]))
                pos += spacing * rng.uniform(0.5, 1.5)
    for i, (edge, lane, pos, vtype) in enumerate(vehicles):
        lines.append('  <vehicle id="v%d" type="%s" route="%s" depart="0" departLane="%d" departPos="%.1f" '
                     'departSpeed="desired"/>' % (i, vtype, edge, lane, pos))
    lines += planned_traffic(params)
    lines.append('</routes>')
    return "\n".join(lines) + "\n"


def planned_traffic(params):
    """Route lines for the scenario's planned_traffic (Phase 7A.3 Stage 15; none without the key): vehicles on the
    ego's carriageway with a fixed speed, entering at depart_s at pos_m along edge "0". pos_m 0 is behind the ego,
    which starts 40 m along the edge: the drawn traffic all starts ahead of it, so nothing else ever comes from
    behind. Each has its own vType, its class's with maxSpeed and a fixed speedFactor giving exactly speed_mps."""
    lines, lane_mps = [], params["speed_limit_kmh"] / 3.6
    planned = sorted(params.get("planned_traffic") or (), key=lambda v: v["depart_s"])  # SUMO wants depart order
    for i, v in enumerate(planned):
        attrs = re.sub(r' speedFactor="[^"]*"', "", vehicle_type(v["type"], params))
        attrs = re.sub(r'maxSpeed="[^"]*"', 'maxSpeed="%.2f"' % v["speed_mps"], attrs)
        lines.append('  <vType id="planned%d" %s speedFactor="%.4f" speedDev="0"/>'
                     % (i, attrs, max(1.0, v["speed_mps"] / lane_mps)))
        lines.append('  <vehicle id="p%d" type="planned%d" route="0" depart="%.1f" departLane="%d" departPos="%.1f" '
                     'departSpeed="max"/>' % (i, i, v["depart_s"], v["lane"], v["pos_m"]))
    return lines


def _turn(axis_angle, yaw):
    """An axis-angle rotation turned by yaw about the world z axis (quaternion product)."""
    x, y, z, a = axis_angle
    n = math.sqrt(x * x + y * y + z * z)
    s = math.sin(a / 2) / n
    q = (math.cos(a / 2), x * s, y * s, z * s)
    t = (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2))
    w = t[0] * q[0] - t[3] * q[3]
    v = (t[0] * q[1] - t[3] * q[2], t[0] * q[2] + t[3] * q[1], t[0] * q[3] + t[3] * q[0])
    angle = 2 * math.acos(max(-1.0, min(1.0, w)))
    norm = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / norm for c in v) + (angle,)


def world(wbt, params, port):
    """The scenario's world from roadsense_highway.wbt: the ego placed for the profile, the viewpoint at the same
    offset from it (SumoInterface only shows SUMO vehicles within its radius of the viewpoint, and the viewpoint
    follows the ego at its initial offset), the fixed potholes and the parked vehicle(1) removed (the supervisor
    imports the drawn potholes), SUMO on its own port and seed."""
    p = PROFILES[params["profile"]]
    vp = re.search(r'Viewpoint \{\n  orientation ([^\n]+)\n  position ([^\n]+)\n', wbt)
    ego = re.search(r'DEF WEBOTS_VEHICLE0 LincolnMKZ \{\n  translation ([^\n]+)\n', wbt)
    if not vp or not ego:
        raise ValueError("viewpoint or ego not found in the world")
    turn = p["yaw"] - math.pi  # the world was built for the ego heading toward -x
    (vx, vy, vz), (ex, ey, _) = [float(c) for c in vp.group(2).split()], [float(c) for c in ego.group(1).split()]
    dx, dy = vx - ex, vy - ey
    c, s = math.cos(turn), math.sin(turn)
    position = (p["start_x"] + dx * c - dy * s, EGO_START_Y + dx * s + dy * c, vz)
    orientation = _turn([float(c) for c in vp.group(1).split()], turn)
    wbt = wbt.replace(vp.group(0), 'Viewpoint {\n  orientation %s\n  position %s\n' % (
        " ".join("%.6f" % c for c in orientation), " ".join("%.3f" % c for c in position)))
    out, n = re.subn(r'(DEF WEBOTS_VEHICLE0 LincolnMKZ \{\n  translation )[^\n]+\n  rotation [^\n]+',
                     lambda m: '%s%.2f %.2f 0.4\n  rotation 0 0 1 %.5f' % (m.group(1), p["start_x"], EGO_START_Y,
                                                                         p["yaw"]), wbt)
    if n != 1:
        raise ValueError("ego not found in the world")
    out = re.sub(r'DEF POTHOLE_\w+ RoadSensePothole \{[^}]*\}\n', "", out)
    out, n = re.subn(r'DEF WEBOTS_VEHICLE1 RangeRoverSportSVRSimple \{.*?\n\}\n', "", out, flags=re.S)
    if n != 1:
        raise ValueError("vehicle(1) not found in the world")
    out = out.replace('EXTERNPROTO "../protos/RoadSensePothole.proto"',
                      'IMPORTABLE EXTERNPROTO "../protos/RoadSensePothole.proto"')
    out, n = re.subn(r'SumoInterface \{\n', 'SumoInterface {\n  port %d\n  seed %d\n' % (port, params["sumo_seed"]),
                     out)
    if n != 1:
        raise ValueError("SumoInterface not found in the world")
    if "lane_change_delay_s" in params:
        out, n = re.subn(r'laneChangeDelay [0-9.]+', 'laneChangeDelay %.2f' % params["lane_change_delay_s"], out)
        if n != 1:
            raise ValueError("laneChangeDelay not found")
    return out


# --- ground truth


def _ego_frame(ego, x, y):
    c, s = math.cos(ego["yaw"]), math.sin(ego["yaw"])
    dx, dy = x - ego["x"], y - ego["y"]
    return dx * c + dy * s, dy * c - dx * s


def frame_outcomes(gt, potholes):
    """Outcomes of one ground-truth frame, from true positions only. gt: {"t", "ego": {x, y, yaw, speed},
    "vehicles": [{id, type, x, y, yaw, speed, length, width}]} with a vehicle's (x, y) at its front bumper (SUMO);
    potholes: [(x, y, yaw, length, width)]. Ahead/behind: vehicles overlapping the ego's lateral extent."""
    ego = gt["ego"]
    lead = follow = None
    collision = False
    for v in gt["vehicles"]:
        fx, fy = v["x"], v["y"]
        bx, by = fx - v["length"] * math.cos(v["yaw"]), fy - v["length"] * math.sin(v["yaw"])
        (f_lon, f_lat), (b_lon, b_lat) = _ego_frame(ego, fx, fy), _ego_frame(ego, bx, by)
        if abs((f_lat + b_lat) / 2) >= v["width"] / 2 + EGO_HALF_WIDTH_M:
            continue
        lo, hi = min(f_lon, b_lon), max(f_lon, b_lon)
        along = v["speed"] * math.cos(v["yaw"] - ego["yaw"])
        if lo <= EGO_FRONT_M and hi >= -EGO_REAR_M:
            collision = True
        elif lo > EGO_FRONT_M and (lead is None or lo - EGO_FRONT_M < lead["gap_m"]):
            lead = {"id": v["id"], "type": v["type"], "gap_m": lo - EGO_FRONT_M, "closing_mps": ego["speed"] - along}
        elif hi < -EGO_REAR_M and (follow is None or -EGO_REAR_M - hi < follow["gap_m"]):
            follow = {"id": v["id"], "type": v["type"], "gap_m": -EGO_REAR_M - hi, "closing_mps": along - ego["speed"],
                      "speed_mps": v["speed"]}
    out = {"t": gt["t"], "collision": collision, "lead_gap_m": None, "lead_ttc_s": None, "lead_headway_s": None,
           "follow_gap_m": None, "follow_ttc_s": None, "follow_headway_s": None, "pothole_under_ego": []}
    for side, r in (("lead", lead), ("follow", follow)):
        if r is None:
            continue
        out[side + "_gap_m"] = round(r["gap_m"], 2)
        out[side + "_type"] = r["type"]
        if r["closing_mps"] > 0.1:
            out[side + "_ttc_s"] = round(r["gap_m"] / r["closing_mps"], 2)
        speed = ego["speed"] if side == "lead" else r["speed_mps"]
        if speed > 2.0:
            out[side + "_headway_s"] = round(r["gap_m"] / speed, 2)
    for i, (x, y, _yaw, length, width) in enumerate(potholes):
        lon, lat = _ego_frame(ego, x, y)
        if -EGO_REAR_M - length / 2 <= lon <= EGO_FRONT_M + length / 2 and abs(lat) <= EGO_HALF_WIDTH_M + width / 2:
            out["pothole_under_ego"].append(i)
    ttc = [out[k] for k in ("lead_ttc_s", "follow_ttc_s") if out[k] is not None]
    gaps = [out[k] for k in ("lead_gap_m", "follow_gap_m") if out[k] is not None]
    out["near_miss"] = collision or any(t < 1.5 for t in ttc) or any(g < 2.0 for g in gaps)
    out["unsafe_headway"] = out["lead_headway_s"] is not None and out["lead_headway_s"] < 1.0
    out["tailgated"] = out["follow_headway_s"] is not None and out["follow_headway_s"] < 1.0
    return out


def summarize(frames, params):
    """A run's outcome summary from its ground-truth frames (the "frame" records of the file)."""
    holes = [(x, y, yaw, h["length_m"], h["width_m"])
             for h, (x, y, yaw) in zip(params["potholes"], pothole_positions(params))]
    outcomes = [frame_outcomes(f, holes) for f in frames]
    def low(key):
        values = [o[key] for o in outcomes if o[key] is not None]
        return round(min(values), 2) if values else None
    counts = {k: sum(1 for o in outcomes if o[k]) for k in ("collision", "near_miss", "unsafe_headway", "tailgated")}
    struck = sorted({i for o in outcomes for i in o["pothole_under_ego"]})
    types = {}
    for f in frames:
        for v in f["vehicles"]:
            types[v["type"]] = types.get(v["type"], 0) + 1
    return {"frames": len(frames), "frames_with": counts, "min_lead_ttc_s": low("lead_ttc_s"),
            "min_lead_gap_m": low("lead_gap_m"), "min_lead_headway_s": low("lead_headway_s"),
            "min_follow_headway_s": low("follow_headway_s"), "potholes_under_ego": struck,
            "vehicle_observations_by_type": dict(sorted(types.items()))}, outcomes


def read_ground_truth(path):
    header, frames, events, end = None, [], [], None
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            kind = r.pop("record")
            if kind == "header":
                header = r
            elif kind == "frame":
                frames.append(r)
            elif kind == "event":
                events.append(r)
            elif kind == "end":
                end = r
    return header, frames, events, end
