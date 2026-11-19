"""W10: what the vectorProjection fix (patch A) changes for the hazard camera's pothole Recognition, offline with the
WbObjectDetection port in tools/webots_calibration.py (frustum clipping of the object's box; Recognition reports the
clipped box's centre, which roadsense.autonomy.hazards turns into the pothole's world position).

Camera: `hazard camera` (fieldOfView 0.87, 512x256, maxRange 60) about CAMERA_HEIGHT_M above the road, level. A pothole
is a flat L x W shape on the road (RoadSensePothole). Prints, per lane and size, the ranges at which each build reports
it and the horizontal error of the reported centre against the true one."""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webots_calibration as wc  # noqa: E402

HFOV, WIDTH, HEIGHT, MAX_RANGE = 0.87, 512, 256, 60.0
VFOV = 2 * math.atan(math.tan(HFOV / 2) * HEIGHT / WIDTH)
CAMERA_HEIGHT_M = 1.72  # measured: Recognition z of distant potholes is -1.70 m on a level ego
MERGE_RADIUS_M = 2.0  # roadsense.autonomy.hazard_map


def recognised(lon, lat, length, width, fixed, h=CAMERA_HEIGHT_M):
    corners = [(lon + dx * length, lat + dy * width, -h + dz) for dx in (-0.5, 0.5) for dy in (-0.5, 0.5)
               for dz in (0.0, 0.006)]
    b = wc.webots_box_bounds(corners, wc.frustum_normals(HFOV, VFOV, MAX_RANGE), fixed)
    if b is None or math.sqrt(sum(((x + y) / 2) ** 2 for x, y in zip(*b))) > MAX_RANGE:
        return None
    return tuple((x + y) / 2 for x, y in zip(*b))


def sweep(lat, length, width):
    out = {}
    for name, fixed in (("installed", False), ("patched_A", True)):
        seen, err = [], []
        for i in range(10, 650):
            lon = i * 0.1
            c = recognised(lon, lat, length, width, fixed)
            if c is not None:
                seen.append(lon)
                err.append((lon, math.hypot(c[0] - lon, c[1] - lat)))
        worst = max(err, key=lambda e: e[1]) if err else (None, None)
        out[name] = {"visible_from_m": min(seen, default=None), "visible_to_m": max(seen, default=None),
                     "centre_error_max_m": round(worst[1], 3) if err else None,
                     "at_m": round(worst[0], 1) if err else None,
                     "error_over_merge_radius_m": [round(l, 1) for l, e in err if e > MERGE_RADIUS_M][:1] or None,
                     "error_at_10_20_40_m": [round(e, 3) for l, e in err if round(l, 1) in (10.0, 20.0, 40.0)]}
    return out


def main():
    res = {}
    for lane, lat in (("ego", 0.0), ("adjacent", 3.75), ("two over", 7.5)):
        for length, width in ((0.74, 0.58), (1.56, 0.82)):
            res["%s lane, %.2f x %.2f m" % (lane, length, width)] = sweep(lat, length, width)
    print(json.dumps({"camera": {"hfov": HFOV, "vfov": round(VFOV, 4), "height_m": CAMERA_HEIGHT_M,
                                 "max_range_m": MAX_RANGE}, "potholes": res}, indent=1))


if __name__ == "__main__":
    main()
