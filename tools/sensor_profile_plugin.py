"""sumo_supervisor plugin for the experimental sensor profile (Stages W9-W10): the Phase 6C scenario plugin, plus

- autorickshaws (SUMO vType "autorickshaw") drawn as RoadSenseAutorickshaw, whose body matches the SUMO vehicle,
  instead of a random 4-5 m car model (ROADSENSE_PROFILE_RICKSHAW=1, the default);
- ROADSENSE_PROFILE_SYNC, how a SUMO step reaches Webots. SUMO's state for SUMO time s matches the ego's Webots pose
  at Webots time s (sumo_supervisor mirrors the ego into SUMO before each step), but it only exists from Webots time
  s + step. "stock": Webots' behaviour, each car put at the pose of the step before and moved toward the new one, so
  Webots shows SUMO time t - 2 steps; "current": put at the new pose, moved on at the same velocity (t - 1 step);
  "aligned": put at the new pose extrapolated by one step at that velocity (t, up to the extrapolation error, but the
  car jumps by that error every step, which Webots' radar turns into range-rate error); "smooth": aligned, without
  the jumps: the car is left where it is and given the velocity that brings it to the pose SUMO's state implies one
  step later (two steps' extrapolation); a car more than 5 m away from it (just assigned) is placed there. The
  angular velocity is also given the right sign (Webots' moves the heading away from the next pose: current - target);
- SUMO's own state logged at each step (ROADSENSE_PROFILE_SUMO_LOG), with both clocks: scoring only.

The stock controller (Webots' projects/default/controllers/sumo_supervisor) is not copied or modified: this module
adjusts the running supervisor instance and the Objects.Vehicle class from the per-world plugin hook. Python 3.9.
"""
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Objects  # noqa: E402  (sumo_supervisor's own modules, on the controller's path)
from SumoSupervisor import rotation_from_yaw_pitch_roll  # noqa: E402
import scenarios  # noqa: E402
from scenario_plugin import SumoSupervisorPlugin as ScenarioPlugin  # noqa: E402
from sensor_profile import MODEL_BOXES, body_x_range  # noqa: E402

RICKSHAW = "RoadSenseAutorickshaw"

# sumo_supervisor places the wrapper at the centre of the SUMO vehicle (front bumper - length / 2) and each model a
# fixed distance per class behind it (car 2.85 m, truck 5.2, bus 4.6, motorcycle 1.7), which puts every stock body
# 0.9 to 3.1 m behind the SUMO vehicle. Aligned: the model is shifted so its body's centre is on the wrapper.
RICKSHAW_DX = -0.95


def _rickshaw_string(index):
    name = "SUMO_VEHICLE%d" % index
    return ("DEF %s Solid {\n  translation 10000 0 0.5\n children [\n%s {\n  translation %g 0 0\n }\n ]\n}\n"
            % (name, RICKSHAW, RICKSHAW_DX), name)


class SumoSupervisorPlugin(ScenarioPlugin):
    def __init__(self, supervisor, traci, net):
        super().__init__(supervisor, traci, net)
        self.sync = os.environ.get("ROADSENSE_PROFILE_SYNC", "stock")
        if self.sync not in ("stock", "current", "aligned", "smooth"):
            raise ValueError("ROADSENSE_PROFILE_SYNC: stock, current, aligned or smooth")
        path = os.environ.get("ROADSENSE_PROFILE_SUMO_LOG")
        self.sumo_log = open(path, "w", buffering=1) if path else None
        if os.environ.get("ROADSENSE_PROFILE_RICKSHAW", "1") == "1":
            self._map_rickshaws(supervisor, traci)
        if os.environ.get("ROADSENSE_PROFILE_ALIGN_BODIES", "1") == "1":
            self._align_bodies()
        self._wrap_update(supervisor)

    @staticmethod
    def _map_rickshaws(sup, traci):
        Objects.WHEEL_RADIUS[RICKSHAW] = 0.2
        generate, init, get_class = Objects.Vehicle.generate_vehicle_string, Objects.Vehicle.__init__, sup.get_vehicle_class

        def vehicle_class(vid):
            if vid not in sup.vehiclesClass and traci.vehicle.getTypeID(vid).split("@")[0] == "autorickshaw":
                sup.vehiclesClass[vid] = "autorickshaw"
            return get_class(vid)

        def vehicle_string(index, vehicle_class):
            return _rickshaw_string(index) if vehicle_class == "autorickshaw" else generate(index, vehicle_class)

        def vehicle_init(self, node):
            car = node.getField("children").getMFNode(0) if node.getTypeName() == "Solid" else node
            if car.getTypeName() != RICKSHAW:
                return init(self, node)
            Objects.CAR_MODEL.append(RICKSHAW)  # a car to Vehicle.__init__, then its own class (never pooled with cars)
            try:
                init(self, node)
            finally:
                Objects.CAR_MODEL.remove(RICKSHAW)
            self.vehicleClass = "autorickshaw"

        sup.get_vehicle_class = vehicle_class
        Objects.Vehicle.generate_vehicle_string = staticmethod(vehicle_string)
        Objects.Vehicle.__init__ = vehicle_init

    @staticmethod
    def _align_bodies():
        generate = Objects.Vehicle.generate_vehicle_string

        def vehicle_string(index, vehicle_class):
            text, name = generate(index, vehicle_class)
            m = re.search(r"children \[\n(\w+)\s*\{\n  translation (-?[0-9.]+) 0 0\n", text)
            if m and m.group(1) in MODEL_BOXES:
                lo, hi = body_x_range(m.group(1))
                text = text[:m.start(2)] + "%.4f" % (-(lo + hi) / 2) + text[m.end(2):]
            return text, name

        Objects.Vehicle.generate_vehicle_string = staticmethod(vehicle_string)

    def _wrap_update(self, sup):
        update = sup.update_vehicles_position_and_velocity

        def placed(step, rotate_wheels):
            self._log_sumo(step)
            if self.sync == "stock":
                return update(step, rotate_wheels)
            lead = 0.0 if self.sync == "current" else 1.0
            for car in sup.vehicles.values():  # Webots' method, but from the newest pose, not the previous one
                if not car.inUse:
                    continue
                move = [car.targetPos[j] - car.currentPos[j] for j in range(3)]
                turn = []
                for j in range(3):  # roll, pitch, yaw: target - current, wrapped
                    diff = (car.targetAngles[j] - car.currentAngles[j] + 2 * math.pi) % (2 * math.pi)
                    turn.append(diff - 2 * math.pi if diff > math.pi else diff)
                roll, pitch, yaw = (car.targetAngles[j] + lead * turn[j] for j in range(3))
                car.rotation.setSFRotation(rotation_from_yaw_pitch_roll(yaw, pitch, roll))
                aim = [car.targetPos[j] + lead * move[j] for j in range(3)]
                if self.sync == "smooth":
                    now = car.translation.getSFVec3f()
                    goal = [aim[j] + move[j] for j in range(3)]  # where SUMO's state implies it is one step later
                    if math.hypot(goal[0] - now[0], goal[1] - now[1]) < 5.0:
                        car.node.setVelocity([1000 * (goal[j] - now[j]) / step for j in range(3)]
                                             + [1000 * x / step for x in turn])
                        continue
                car.translation.setSFVec3f(aim)
                car.node.setVelocity([1000 * x / step for x in move + turn])
                if rotate_wheels:
                    for wheel in car.wheelsAngularVelocity:
                        wheel.setSFVec3f([0, car.speed / car.wheelRadius, 0])

        sup.update_vehicles_position_and_velocity = placed

    def _log_sumo(self, step):
        """SUMO's state just computed by simulationStep, before Webots shows it (scoring only)."""
        if self.sumo_log is None or self.done:
            return
        v = self.traci.vehicle
        cars, ego = [], None
        if "webotsVehicle0" in v.getIDList():
            ex, ey = v.getPosition("webotsVehicle0")
            ego = {"x": round(ex - scenarios.NET_OFFSET[0], 4), "y": round(ey - scenarios.NET_OFFSET[1], 4),
                   "yaw": round(math.radians(90.0 - v.getAngle("webotsVehicle0")), 5), "length": v.getLength("webotsVehicle0")}
        nodes = {car.currentID: i for i, car in self.sup.vehicles.items() if car.inUse}
        for vid in v.getIDList():
            if vid.startswith("webotsVehicle"):
                continue
            sx, sy = v.getPosition(vid)
            cars.append({"id": vid, "node": nodes.get(vid), "type": v.getTypeID(vid).split("@")[0],
                         "x": round(sx - scenarios.NET_OFFSET[0], 4), "y": round(sy - scenarios.NET_OFFSET[1], 4),
                         "yaw": round(math.radians(90.0 - v.getAngle(vid)), 5), "speed": round(v.getSpeed(vid), 4),
                         "accel": round(v.getAcceleration(vid), 4), "length": v.getLength(vid),
                         "width": v.getWidth(vid), "height": v.getHeight(vid), "lane": v.getLaneIndex(vid)})
        self.sumo_log.write(json.dumps({"webots_t": round(self.sup.getTime(), 3),
                                        "sumo_t": round(self.traci.simulation.getTime(), 3), "step_ms": step,
                                        "sync": self.sync, "ego_sumo": ego, "vehicles": cars}, separators=(",", ":")) + "\n")
