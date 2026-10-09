"""Road hazards for EGO_ROADSENSE: the generic HazardDetection, and the simulated sensor that produces them here.

Plain Python without Webots imports (and Python 3.9, which Webots runs controllers with). A HazardDetection is one
observation of a road hazard on the ground, in the world frame, from any source: today the Webots hazard camera below,
later a real camera, an IMU or another vehicle. hazard_map.py keeps them. Positions in m, times in s of simulation time.
"""
import math
from typing import NamedTuple, Optional

import perception

SEVERITIES = ("LOW", "MEDIUM", "HIGH")  # road-hazard severity, not a collision-risk level


class HazardDetection(NamedTuple):
    """One observation of a road hazard."""
    timestamp: float
    source: str  # the sensor that saw it
    hazard_type: str  # "pothole"
    world_x: float
    world_y: float
    severity: str  # one of SEVERITIES
    confidence: float  # 0 to 1
    dimensions: Optional[dict]  # {"length_m", "width_m", "depth_m"}, None if unknown


# SIMULATION ONLY: the Webots hazard camera. `hazard camera` on EGO_ROADSENSE in roadsense_highway.wbt recognizes the
# world's RoadSensePothole objects with Webots camera recognition: inside its field of view, up to 60 m, not hidden
# behind a vehicle. Each comes with the PROTO's label, e.g. "pothole severity=HIGH length=1.2 width=0.9 depth=0.08":
# what a real detector would estimate, without its errors.
SOURCE = "webots_simulated_road_sensor"
CONFIDENCE = 0.9  # the simulation's detections are right, but a detector never claims certainty
HAZARD_CAMERA = (1.61, 0.0)  # ego-frame x, y of `hazard camera` (sensorsSlotTop + 0.5 m), facing forward
_SIMULATED_TYPES = ("pothole",)


def simulated_detections(objects, t, pose):
    """SIMULATION ONLY. Detections from the hazard camera's recognized objects, each (position, label): the object's
    centre relative to the camera (x ahead, y left, z up) and the recognized Solid's model field. Objects that aren't
    labelled hazards (vehicles), have no RoadSense severity or no finite position are skipped. pose is the ego's
    world (x, y, yaw)."""
    detections = []
    for (x, y, _), label in objects:
        words = label.split()
        if not words or words[0] not in _SIMULATED_TYPES:
            continue
        attributes = dict(w.split("=", 1) for w in words[1:] if "=" in w)
        if attributes.get("severity") not in SEVERITIES or not (math.isfinite(x) and math.isfinite(y)):
            continue
        world_x, world_y = perception.ego_to_world(HAZARD_CAMERA[0] + x, HAZARD_CAMERA[1] + y, pose)
        detections.append(HazardDetection(t, SOURCE, words[0], world_x, world_y, attributes["severity"], CONFIDENCE,
                                          _dimensions(attributes)))
    return detections


def _dimensions(attributes):
    try:
        return {"length_m": float(attributes["length"]), "width_m": float(attributes["width"]),
                "depth_m": float(attributes["depth"])}
    except (KeyError, ValueError):
        return None
