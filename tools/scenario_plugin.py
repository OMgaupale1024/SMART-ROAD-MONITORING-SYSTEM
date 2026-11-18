"""sumo_supervisor plugin for Phase 6C scenarios: places the drawn potholes, stages the family's event with TraCI,
logs simulator ground truth every SUMO step (200 ms) and quits at the drawn duration.

Loaded from a generated `<world>_net/plugin.py`; reads ROADSENSE_SCENARIO (scenario.json) and writes
ROADSENSE_GROUND_TRUTH (JSON lines). Python 3.9, as Webots runs it. Never imports RoadSense.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios  # noqa: E402

EGO = "webotsVehicle0"  # the Webots ego as sumo_supervisor mirrors it into SUMO
RANGE_M = 150.0  # ground truth keeps the vehicles within this distance of the ego
SPAWN_WAIT_S = 2.0
PLACE_RETRY_S = 10.0  # an event vehicle waits this long for its slot to be clear, then the event is abandoned
CLEAR_M = 8.0


class SumoSupervisorPlugin:
    def __init__(self, supervisor, traci, net):
        self.sup, self.traci = supervisor, traci
        with open(os.environ["ROADSENSE_SCENARIO"]) as f:
            self.p = json.load(f)
        self.profile = scenarios.PROFILES[self.p["profile"]]
        self.out = open(os.environ["ROADSENSE_GROUND_TRUTH"], "w", buffering=1)
        self.ego = supervisor.getFromDef("WEBOTS_VEHICLE0")
        children = supervisor.getRoot().getField("children")
        for node in scenarios.pothole_nodes(self.p):
            children.importMFNodeFromString(-1, node)
        self.events = [dict(e, phase="waiting") for e in (self.p.get("event"), self.p.get("secondary_event")) if e]
        for index, event in enumerate(self.events):
            event["id_suffix"] = "" if index == 0 else "_%d" % (index + 1)
        self.event = self.events[0] if self.events else None
        self.frames = 0
        self.done = False
        self._write({"record": "header", "schema": "roadsense.groundtruth.v2", "run_id": self.p.get("run_id") or scenarios.run_id(self.p),
                     "scenario": self.p, "coordinate_frame": "webots_world", "simulated": True,
                     "clock": "simulation", "period_s": 0.2,
                     "potholes": [{"x": round(x, 2), "y": round(y, 2), "yaw": yaw}
                                  for x, y, yaw in scenarios.pothole_positions(self.p)],
                     "ego_footprint_m": {"rear": scenarios.EGO_REAR_M, "front": scenarios.EGO_FRONT_M,
                                         "half_width": scenarios.EGO_HALF_WIDTH_M},
                     "vehicle_position": "front bumper centre reconstructed from physical Webots centre and intended SUMO length",
                     "pose_source": "webots_physical", "dimensions_source": "SUMO intended (proxy)",
                     "sumo_pose_source": "SUMO logical; may differ during Webots smoothing"})

    def _write(self, record):
        self.out.write(json.dumps(record, separators=(",", ":")) + "\n")

    # --- geometry

    def _lane_pos(self, x):
        """Position along edge 0 of Webots x (edge 0 runs with the ego's direction of travel)."""
        return x + 5000.0 if self.profile["direction"] > 0 else -x

    def _ego_state(self):
        x, y, _z = self.ego.getPosition()
        m = self.ego.getOrientation()
        vx, vy = self.ego.getVelocity()[:2]
        travelled = (x - self.profile["start_x"]) * self.profile["direction"]
        return {"x": round(x, 3), "y": round(y, 3), "yaw": round(math.atan2(m[3], m[0]), 4),
                "speed": round(math.hypot(vx, vy), 3), "travelled_m": round(travelled, 1)}

    def _ego_lane(self, ego):
        return min(scenarios.LANE_Y, key=lambda i: abs(scenarios.LANE_Y[i] - ego["y"]))

    def _toward(self, side, lane):
        """SUMO lane index on the driver's side of lane (indices grow toward the median)."""
        median = "left" if self.p["traffic"] == "right" else "right"
        return lane + (1 if side == median else -1)

    # --- the event

    def _blocker(self, lane, front_pos, length, ids):
        """A vehicle in the lane within CLEAR_M of the slot [front_pos - length, front_pos], or None. Not the ego:
        the slot's distance from it is the drawn parameter, and its SUMO mirror lags the Webots ego by a step."""
        v = self.traci.vehicle
        for vid in ids:
            if vid != EGO and v.getRoadID(vid) == "0" and v.getLaneIndex(vid) == lane:
                pos = v.getLanePosition(vid)
                if front_pos - length - CLEAR_M < pos < front_pos + v.getLength(vid) + CLEAR_M:
                    return [vid, round(pos, 1), v.getLength(vid), round(front_pos, 1)]
        return None

    def _spawn(self, name, vtype, lane, front_pos, speed, now, ids):
        """Place an event vehicle as sumo_supervisor places the ego (add, then moveToXY, which bypasses SUMO's
        insertion checks), once its slot in the lane is clear; False while it is not."""
        speed = min(max(0., speed), scenarios.vehicle_max_speed(vtype))
        blocker = self._blocker(lane, front_pos, scenarios.vehicle_length(vtype), ids)
        if blocker:
            self.event["blocked_by"] = blocker
            return False
        d = self.profile["direction"]
        x = (front_pos - 5000.0) if d > 0 else -front_pos  # Webots x of the lane position
        v = self.traci.vehicle
        try:
            v.add(name, "0", typeID=vtype, depart="now", departSpeed="%.2f" % max(0.0, speed))
            v.moveToXY(name, "0", lane, x + scenarios.NET_OFFSET[0], scenarios.LANE_Y[lane] + scenarios.NET_OFFSET[1],
                       angle=90.0 if d > 0 else 270.0, keepRoute=1)
            v.setSpeed(name, max(0.0, speed))
        except Exception as e:  # noqa: BLE001 - recorded, the run goes on
            self._log_event(now, "spawn_failed", vehicle=name, error=str(e))
            self.event["phase"] = "failed"
            return True
        self.event.update(vehicle=name, spawned_at=now, phase="spawned", start_speed=round(max(0.0, speed), 2))
        return True

    def _present(self, name):
        return name in self.traci.vehicle.getIDList()

    def _log_event(self, now, what, **detail):
        self._write(dict({"record": "event", "t": round(now, 2), "kind": self.event["kind"], "what": what}, **detail))

    def _step_event(self, now, ego, ids):
        e, v = self.event, self.traci.vehicle
        if e["phase"] in ("done", "failed") or EGO not in ids:
            return
        lane = self._ego_lane(ego)
        front = self._lane_pos(ego["x"] + self.profile["direction"] * scenarios.EGO_FRONT_M)
        rear = self._lane_pos(ego["x"] - self.profile["direction"] * scenarios.EGO_REAR_M)
        vtype, suffix = e.get("vehicle_type", "car"), e.get("id_suffix", "")
        if e["phase"] == "waiting":
            if ego["travelled_m"] < e["at_m"]:
                return
            e.setdefault("triggered_at", now)
            if now - e["triggered_at"] > PLACE_RETRY_S:
                e["phase"] = "failed"
                self._log_event(now, "slot_never_clear", ego_lane=lane, blocked_by=e.get("blocked_by"))
                return
            if e["kind"] == "sudden_brake":
                ok = self._spawn("rs_lead" + suffix, vtype, lane, front + e["gap_m"] + scenarios.vehicle_length(vtype), ego["speed"], now, ids)
            elif e["kind"] == "cut_in":
                target = self._toward(e["side"], lane)
                if target not in (1, 2, 3):
                    e["side"] = "right" if e["side"] == "left" else "left"
                    target = self._toward(e["side"], lane)
                    e["side_switched"] = True
                e["from_lane"], e["to_lane"] = target, lane
                ok = self._spawn("rs_cutter" + suffix, vtype, target, front + e["gap_m"] + scenarios.vehicle_length(vtype),
                                 ego["speed"] + e["speed_delta_kmh"] / 3.6, now, ids)
            else:
                ok = self._spawn("rs_tailgater" + suffix, vtype, lane, rear - 15.0, ego["speed"], now, ids)
            if ok:
                self._log_event(now, "spawn_requested", ego_lane=lane, ego_speed=ego["speed"],
                                ego_travelled_m=ego["travelled_m"], waited_s=round(now - e["triggered_at"], 2))
            return
        name = e["vehicle"]
        if e["phase"] == "spawned":
            if not self._present(name):
                if now - e["spawned_at"] > SPAWN_WAIT_S:
                    try:
                        v.remove(name)
                    except Exception:  # noqa: BLE001
                        pass
                    e["phase"] = "failed"
                    self._log_event(now, "not_inserted", vehicle=name)
                return
            v.setLaneChangeMode(name, 0)
            v.setSpeedMode(name, 0)  # its staged speed exactly, until it acts (the tailgater: throughout)
            v.setSpeed(name, e["start_speed"])
            if e["kind"] == "sudden_brake":
                v.setDecel(name, e["decel_mps2"])  # setSpeed below brakes at this limit: the drawn deceleration
                v.setEmergencyDecel(name, e["decel_mps2"] + 2.0)
            e["phase"], e["since"] = "settling", now
            self._log_event(now, "inserted", vehicle=name, lane=v.getLaneIndex(name))
            return
        if not self._present(name):
            e["phase"] = "done"
            self._log_event(now, "vehicle_left", vehicle=name)
            return
        if e["phase"] == "settling" and now - e["since"] >= e.get("settle_s", {"sudden_brake": 3.0, "cut_in": 1.0}.get(e["kind"], 0.0)):
            if e["kind"] != "tailgating":
                v.setSpeedMode(name, 31)  # SUMO's own limits again for the manoeuvre
            if e["kind"] == "sudden_brake":
                to = e["to_kmh"] / 3.6
                duration = max(0.4, (v.getSpeed(name) - to) / e["decel_mps2"])
                v.setSpeed(name, to)  # held until released (slowDown would hand back control after braking)
                e["phase"], e["since"], e["brake_s"] = "acting", now, duration
            elif e["kind"] == "cut_in":
                v.setLaneChangeMode(name, e.get("lane_change_mode", 512))  # 512 enforces gaps; 0 deliberately bypasses them.
                v.changeLane(name, e["to_lane"], e.get("request_s", 3.0))
                e["phase"], e["since"] = "acting", now
            else:
                e["phase"], e["since"] = "acting", now
            self._log_event(now, "act", vehicle=name, speed=round(v.getSpeed(name), 2), lane=v.getLaneIndex(name))
            return
        if e["phase"] == "acting" and e["kind"] == "tailgating" and now - e["since"] < e["duration_s"]:
            # hold the drawn time gap behind the ego's rear: a proportional speed command on the gap error
            gap = rear - v.getLanePosition(name)
            target = max(2.0, e["time_gap_s"] * ego["speed"])
            v.setSpeed(name, max(0.0, ego["speed"] + max(-4.0, min(4.0, 0.6 * (gap - target)))))
            return
        if e["phase"] == "acting":
            if e["kind"] == "sudden_brake" and now - e["since"] >= e["brake_s"] + e["hold_s"]:
                v.setSpeed(name, -1)
                e["phase"] = "done"
            elif e["kind"] == "cut_in" and now - e["since"] >= e.get("release_after_s", 6.0):
                v.setSpeed(name, -1)
                v.setLaneChangeMode(name, 1621)
                e["phase"] = "done"
            elif e["kind"] == "tailgating":
                v.setSpeedMode(name, 31)
                v.setSpeed(name, -1)
                e["phase"] = "done"
            if e["phase"] == "done":
                self._log_event(now, "released", vehicle=name, lane=v.getLaneIndex(name))

    # --- every SUMO step

    def run(self, ms):
        if self.done:
            return
        now = self.sup.getTime()
        ego = self._ego_state()
        v = self.traci.vehicle
        ids = v.getIDList()
        for event in getattr(self, "events", [self.event] if self.event else []):
            self.event = event
            self._step_event(now, ego, ids)
        if hasattr(self, "events"):
            self.event = self.events[0] if self.events else None
        vehicles = []
        for vid in ids:
            if vid.startswith("webotsVehicle"):
                continue
            sx, sy = v.getPosition(vid)
            x, y = sx - scenarios.NET_OFFSET[0], sy - scenarios.NET_OFFSET[1]
            if abs(x - ego["x"]) > RANGE_M or abs(y - ego["y"]) > 40:
                continue
            vehicles.append({"id": vid, "type": v.getTypeID(vid).split("@")[0],  # SUMO copies types as car@rs_lead
                             "x": round(x, 3), "y": round(y, 3),
                             "yaw": round(math.radians(90.0 - v.getAngle(vid)), 4), "speed": round(v.getSpeed(vid), 3),
                             "length": v.getLength(vid), "width": v.getWidth(vid), "edge": v.getRoadID(vid),
                             "lane": v.getLaneIndex(vid)})
        # SUMO lane changes are smoothed by Webots; label the physical objects that radar observes.
        logical = vehicles
        physical = []
        nodes = {car.currentID: car.node for car in self.sup.vehicles.values() if car.inUse}
        missing = []
        for vehicle in logical:
            node = nodes.get(vehicle["id"])
            if node is None:
                missing.append(vehicle["id"])
                continue
            x, y, _ = node.getPosition()
            m = node.getOrientation()
            yaw = math.atan2(m[3], m[0])
            vx, vy = node.getVelocity()[:2]
            physical.append(dict(vehicle, x=round(x + vehicle["length"] / 2 * math.cos(yaw), 3),
                                 y=round(y + vehicle["length"] / 2 * math.sin(yaw), 3),
                                 yaw=round(yaw, 4), speed=round(vx * math.cos(yaw) + vy * math.sin(yaw), 3)))
        self._write({"record": "frame", "t": round(now, 2), "ego": ego, "ego_in_sumo": EGO in ids,
                     "vehicles": physical, "sumo_vehicles": logical, "missing_physical": missing})
        self.frames += 1
        if now >= self.p["duration_s"] or ego["travelled_m"] > 4900:
            self.done = True
            self._write({"record": "end", "t": round(now, 2), "frames": self.frames,
                         "reason": "duration" if now >= self.p["duration_s"] else "end_of_road",
                         "event": {k: val for k, val in (self.event or {}).items() if k != "since"} or None,
                         "events": [{k: val for k, val in e.items() if k != "since"} for e in getattr(self, "events", [])]})
            self.out.close()
            self.sup.simulationQuit(0)
