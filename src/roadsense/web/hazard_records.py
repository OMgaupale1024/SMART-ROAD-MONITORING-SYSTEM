"""Road-hazard records that outlive the live state: the roadsense.hazard.v1 structure (docs/hazard-records.md).

The live state carries the hazard map as it is in one processing cycle, relative to the car. A record is what would
be stored or sent on: where the hazard is, in a named coordinate frame, how sure RoadSense is, and where the
knowledge came from. Positions from the Webots world are simulation coordinates and say so; "geo" stays null until a
producer supplies a real GNSS fix. Nothing here invents one.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

SCHEMA = "roadsense.hazard.v1"
COLLECTION_SCHEMA = "roadsense.hazards.v1"
FRAME = "webots_world"  # metres in the Webots world frame: the only producer today is the simulation


def _number(value: Any) -> Optional[float]:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def record(hazard: Dict[str, Any], run_id: str, processor: Optional[str]) -> Optional[Dict[str, Any]]:
    """One hazard of a live state's hazard map as a record; None if it has no id or no world position."""
    position = hazard.get("world_position") if isinstance(hazard.get("world_position"), dict) else {}
    x, y = _number(position.get("x")), _number(position.get("y"))
    hazard_id = hazard.get("hazard_id")
    if not isinstance(hazard_id, str) or x is None or y is None:
        return None
    dimensions = hazard.get("dimensions") if isinstance(hazard.get("dimensions"), dict) else None
    return {
        "schema": SCHEMA,
        # unique per run and local id, so sending a record twice changes nothing; a central map decides which
        # records from different runs and vehicles describe the same hazard
        "record_id": "%s:%s" % (run_id, hazard_id),
        "local_id": hazard_id,
        "type": hazard.get("type"),
        "severity": hazard.get("severity"),
        "confidence": _number(hazard.get("confidence")),
        "observations": _number(hazard.get("observation_count")),
        "status": hazard.get("status"),
        "dimensions_m": dimensions and {
            key: _number(dimensions.get(key + "_m")) for key in ("length", "width", "depth")},
        "position": {"frame": FRAME, "x_m": x, "y_m": y, "geo": None},
        "observed": {"clock": "simulation", "first_seen_s": _number(hazard.get("first_seen_s")),
                     "last_seen_s": _number(hazard.get("last_seen_s"))},
        "provenance": {"simulated": True, "sensor": hazard.get("source"), "vehicle_id": hazard.get("source_vehicle"),
                       "run_id": run_id, "processor": processor},
    }


def collection(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The records of a live state's hazard map (GET /api/hazards); empty before the first state."""
    simulation = (state or {}).get("simulation") or {}
    run_id = simulation.get("run_id")
    processor = "edge" if simulation.get("processing") == "edge" else simulation.get("source")
    hazards: List[Dict[str, Any]] = [h for h in (state or {}).get("hazards", []) if isinstance(h, dict)]
    records = [r for r in (record(h, run_id, processor) for h in hazards) if r is not None]
    return {"schema": COLLECTION_SCHEMA, "run_id": run_id, "coordinate_frame": FRAME, "geo_configured": False,
            "records": records}
