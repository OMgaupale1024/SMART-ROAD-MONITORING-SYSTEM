"""Experimental radar measurement model: every Webots radar return kept, with its uncertainty, flags and a conservative
clearance interval. Inference uses only the return, the radar's mount and calibration constants measured on
development recordings: no simulator dimensions, identities or positions.

What one return is (docs/webots-sensor-correction.md §6): the range is to the centre of the part of the target's
bounding body inside the beam, and the bearing to the same point (W8 AB build, bearing_reference "point") or to the
target's model origin, its rear axle (installed build, "origin"). Neither is the near surface: a measured point is
1.6-2.2 m (median) farther from the ego's body than the target's surface. The clearance interval below is the point's
clearance minus the slack measured between such points and the true surface (SLACK_M), never one fixed vehicle length.
"""
import math
from typing import NamedTuple, Optional

ANGULAR_NOISE_RAD = 0.001
RANGE_NOISE_M = 0.25
MAX_PLAUSIBLE_RANGE_RATE_MPS = 60.0  # faster: Webots' first refresh of a target (or NaN): position kept, speed not
EGO_BOX = (-1.01, 3.89, 0.9)  # EGO_ROADSENSE's body (LincolnMKZ): rear x, front x, half width, ego frame

# Reference-point spread around the target's body centre, 1 sigma (along the line of sight, across it), measured
# against drawn bodies in the W9 development runs (C-smooth / C-smooth-AB: longitudinal p95 0.50-0.68 m, lateral p95
# 0.22 m with the AB build and 0.72 m installed).
REFERENCE_SIGMA_M = {"point": (0.30, 0.12), "origin": (0.35, 0.40)}

# Self-clutter signatures from target-free calibration runs (W8-W11 §6.3): (range interval, clamp side or None,
# presence). A return matching one may be the ego's own body; it is kept and flagged, never used to start a track.
SELF_CLUTTER = {
    "front": {"range": (0.95, 1.6), "edge_sign": None},
    "rear": {"range": (0.95, 1.1), "edge_sign": None},
    "corner_fl": {"range": (1.5, 3.7), "edge_sign": -1},
    "corner_fr": {"range": (1.5, 3.7), "edge_sign": +1},
}

# Clearance slack (point clearance - true surface clearance, m): quantiles (q01, q50, q99) by radar group, geometry
# ("inline": the point within 1.5 m of the ego's centre line, else "side") and range band. Measured with
# tools/experimental_replay.py calibrate on the W9 development runs (C-smooth-AB, C-aligned-AB), not on the
# validation runs it is evaluated on. A band with too few samples falls back to its group's "all" entry.
SLACK_M = {}  # filled from calibration.json at import (see load_slack); empty -> DEFAULT_SLACK
DEFAULT_SLACK = (0.0, 2.0, 6.0)
RANGE_BANDS = ((0, 30), (30, 80), (80, 1e9))


class RadarSpec(NamedTuple):
    name: str  # the device name
    group: str  # "front", "rear", "corner_fl", ... (calibration key)
    mount: tuple  # x, y, z, yaw (ego frame)
    min_range: float
    max_range: float
    hfov: float
    bearing_reference: str = "point"  # "point" (W8 AB build) or "origin" (installed build)


class RadarMeasurement(NamedTuple):
    t: float
    sensor: str
    lon: float  # ego frame point
    lat: float
    range: float  # from the mount
    bearing: float  # ego frame, rad, positive left
    sigma_range: float
    sigma_bearing: float
    range_rate: Optional[float]  # None when unreliable
    power: Optional[float]
    flags: frozenset  # self_suspect, range_clamped, bearing_clamped, velocity_unreliable
    clearance: tuple  # (lo, hi) m from the ego's body to the target's surface
    mount: tuple  # x, y (ego frame)


def footprint_distance(lon, lat, box=EGO_BOX):
    rear, front, half = box
    return math.hypot(max(rear - lon, 0.0, lon - front), max(abs(lat) - half, 0.0))


def band(rng):
    return next("%d-%s" % (lo, "" if hi > 1e8 else int(hi)) for lo, hi in RANGE_BANDS if lo <= rng < hi)


def slack(group, lat, rng):
    table = SLACK_M.get(group, {})
    key = ("inline" if abs(lat) < 1.5 else "side") + " " + band(rng)
    return table.get(key) or table.get("all") or DEFAULT_SLACK


def measure(spec, t, distance, azimuth, range_rate, power=None):
    """One return (Webots convention: azimuth positive to the radar's right) -> RadarMeasurement."""
    x, y, _z, yaw = spec.mount
    edge = spec.hfov / 2
    flags = set()
    range_clamped = distance <= spec.min_range + 1e-4
    bearing_clamped = abs(azimuth) >= edge - 5 * ANGULAR_NOISE_RAD
    if range_clamped:
        flags.add("range_clamped")
    if bearing_clamped:
        flags.add("bearing_clamped")
    sig = SELF_CLUTTER.get(spec.group)
    if (sig and sig["range"][0] <= distance <= sig["range"][1]
            and (range_clamped or (bearing_clamped and (sig["edge_sign"] is None
                                                        or math.copysign(1, azimuth) == sig["edge_sign"])))
            and (range_rate is None or abs(range_rate) < 1.0)):
        flags.add("self_suspect")
    rr = range_rate if range_rate is not None and math.isfinite(range_rate) \
        and abs(range_rate) <= MAX_PLAUSIBLE_RANGE_RATE_MPS else None
    if rr is None:
        flags.add("velocity_unreliable")
    ref_r, ref_c = REFERENCE_SIGMA_M[spec.bearing_reference]
    rng = max(distance, 0.1)
    sigma_r = math.hypot(RANGE_NOISE_M, ref_r)
    sigma_b = math.hypot(ANGULAR_NOISE_RAD, ref_c / rng)
    az = azimuth
    if range_clamped:  # anywhere within the minimum range
        rng, sigma_r = spec.min_range / 2, spec.min_range / 2
    if bearing_clamped:  # the target is partly outside the field of view: beyond the edge, angle unknown
        sigma_b = max(sigma_b, 0.15)
        az = math.copysign(edge + 0.15, azimuth)
    bearing = yaw - az
    lon, lat = x + rng * math.cos(bearing), y + rng * math.sin(bearing)
    c = footprint_distance(lon, lat)
    lo_s, _mid, hi_s = slack(spec.group, lat, rng)
    clearance = (max(0.0, c - hi_s), max(0.0, c - lo_s))
    if range_clamped:
        clearance = (0.0, max(0.0, c))
    return RadarMeasurement(t, spec.name, lon, lat, rng, bearing, sigma_r, sigma_b, rr, power, frozenset(flags),
                            clearance, (x, y))


def load_slack(table):
    SLACK_M.clear()
    SLACK_M.update({g: {k: tuple(v) for k, v in bands.items()} for g, bands in table.items()})


def _load_default():
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
    if os.path.exists(path):
        with open(path) as f:
            load_slack(json.load(f).get("radar_slack_m", {}))


_load_default()
