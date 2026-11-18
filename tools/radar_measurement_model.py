"""Stage W8-W11 section 6, experimental: an ambiguity-preserving description of one Webots radar return.

A proposal, evaluated offline on development recordings only. RoadSense's authoritative perception
(perception.radar_detections, which drops returns within EDGE_MARGIN_RAD of the field-of-view edge and range rates
beyond MAX_RANGE_RATE_MPS), its trackers and its safety logic are unchanged and do not import this.

Instead of keeping or dropping, every return is kept with flags and intervals:
- self_suspect: matches the radar's self-clutter signature (its own vehicle's body), from a target-free calibration;
  kept, so a real obstacle in the same cell is not lost, and left for another sensor to confirm or rule out;
- range_clamped: at the minimum-range clamp: the range is an interval [0, min_range], not a value;
- bearing_clamped: at the field-of-view edge: the bearing is one-sided, [edge - 3 sigma, edge + beyond], the object
  is partly outside the field of view; the side is known, the angle is not;
- velocity_unreliable: the first refresh of a target (Webots differences the target's translation against the last
  one it saw, which may be a far-away parking position), NaN or an implausible magnitude: position kept, speed not;
- the point is not the object's near face: Webots measures to the centre of the part of the body inside the beam,
  so the clearance to the object's surface can be smaller than the point's by up to `max_half_extent` (class
  agnostic: no dimensions or identities from the simulator).
"""
import math

ANGULAR_NOISE_RAD = 0.001  # the RoadSense radars' angularNoise
RANGE_NOISE_M = 0.25  # rangeNoise
MAX_HALF_EXTENT_M = 6.0  # half a 12 m bus or truck: how far the near face may be inside the measured point
MAX_RANGE_RATE_MPS = 100.0

# Self-clutter signatures measured with no target anywhere (calibration world, ego at 0, 10 and 25 m/s, 900 radar
# samples, installed R2025b and the W8 AB build alike): radar -> (bearing at the clamp, range interval, presence).
# The front corners see the ego's front 1.7-3.6 m away at their inner edge on every sample; the front and rear
# radars see it at the 1 m minimum-range clamp on 59-63% and 19% of samples.
SELF_CLUTTER = {
    "radar front": {"edge_sign": None, "range": (0.95, 1.6), "presence": 0.6},
    "radar rear": {"edge_sign": None, "range": (0.95, 1.1), "presence": 0.19},
    "exp radar corner fl": {"edge_sign": -1, "range": (1.5, 3.7), "presence": 1.0},
    "exp radar corner fr": {"edge_sign": +1, "range": (1.5, 3.7), "presence": 1.0},
}


def build_self_clutter(samples, hfov, margin=0.005):
    """A signature from target-free samples [(distance, azimuth, speed)]: the range interval (+- 4 sigma of the
    noise) of returns at the clamp, the side of the clamp, and how often one is present (one return per sample)."""
    hits = [s for s in samples if abs(abs(s[1]) - hfov / 2) < margin or s[0] <= 1.0001]
    if not hits:
        return None
    sides = {1 if s[1] > 0 else -1 for s in hits if s[0] > 1.0001}
    return {"edge_sign": sides.pop() if len(sides) == 1 else None,
            "range": (min(s[0] for s in hits) - RANGE_NOISE_M, max(s[0] for s in hits) + RANGE_NOISE_M),
            "presence": len(hits) / len(samples)}


def describe(radar, d, az, speed, hfov, min_range=1.0, first_refresh=False, clutter=SELF_CLUTTER,
             edge_margin=5 * ANGULAR_NOISE_RAD):
    """One return -> the flags and intervals above. Azimuth as Webots reports it (positive to the right)."""
    edge = hfov / 2
    range_clamped = d <= min_range + 1e-4
    bearing_clamped = abs(az) >= edge - edge_margin
    sig = clutter.get(radar)
    self_suspect = bool(sig and sig["range"][0] <= d <= sig["range"][1]
                        and (range_clamped or (bearing_clamped and (sig["edge_sign"] is None
                                                                    or math.copysign(1, az) == sig["edge_sign"])))
                        and (speed is None or not math.isfinite(speed) or abs(speed) < 1.0))
    velocity_ok = (not first_refresh and speed is not None and math.isfinite(speed)
                   and abs(speed) <= MAX_RANGE_RATE_MPS)
    return {
        "range": (0.0, min_range) if range_clamped else (max(0.0, d - 3 * RANGE_NOISE_M), d + 3 * RANGE_NOISE_M),
        "bearing": ((math.copysign(edge - 3 * ANGULAR_NOISE_RAD, az), math.copysign(math.pi, az))
                    if bearing_clamped else (az - 3 * ANGULAR_NOISE_RAD, az + 3 * ANGULAR_NOISE_RAD)),
        "range_rate": speed if velocity_ok else None,
        "self_suspect": self_suspect, "range_clamped": range_clamped, "bearing_clamped": bearing_clamped,
        "velocity_unreliable": not velocity_ok,
        # the object's surface may be up to this much closer than the measured point (reference-point ambiguity)
        "near_face_slack_m": MAX_HALF_EXTENT_M,
        "roadsense_current_rule": "dropped" if bearing_clamped else "kept",
    }
