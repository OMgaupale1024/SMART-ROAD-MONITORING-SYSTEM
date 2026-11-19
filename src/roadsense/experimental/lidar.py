"""Stage W11, experimental: a compact binary lidar format and geometry-only lidar processing (no fusion, no learning).

Nothing here is used by RoadSense's authoritative perception, tracking or safety logic. It is written so it could run
on the edge device (numpy, no simulator imports): the simulator only produces range images; processing is separate.

Format (little-endian, versioned; one CONFIG per sensor configuration, then FRAMEs that name it by config_id):

  CONFIG  "RSLC" u16 version u16 header_bytes u32 config_id  16s sensor
          6f mount (x, y, z, roll, pitch, yaw: ego frame, m / rad)  u16 width u16 layers
          f fov f az0 f az_step  (column i looks at az0 + i * az_step, rad, positive left)
          f min_range f max_range u8 encoding (1 float32 m, 2 uint16 at `scale` m per unit, 0 = no return) f scale
          layers x f elevation (rad, calibrated per beam, layer 0 first)
  FRAME   "RSLF" u16 version u16 flags (1 zlib, 2 validity bitmask follows) u32 config_id u32 seq
          d t_capture (s, sensor clock)  f scan_s (0: every beam at t_capture)  u32 payload_bytes, payload
  OBJECTS "RSLO" u16 version u16 count u32 config_id u32 seq d t_capture, count x 22-byte clusters (cm, int16):
          near x y, x min max, y min max, z min max, u16 points, u8 confidence (/255), u8 flags

config_id is the CRC-32 of the CONFIG body, so a frame cannot be decoded against the wrong beam table.
"""
import math
import struct
import zlib
from collections import defaultdict

import numpy as np

VERSION = 1
FLOAT32, UINT16 = 1, 2
ZLIB, MASK = 1, 2
_CFG = struct.Struct("<4sHHI16s6fHHfffffBf")
_FRM = struct.Struct("<4sHHIIdfI")
_OBJ = struct.Struct("<4sHHIId")
_CLU = struct.Struct("<9hHBB")

# Webots' 16-layer, 30-degree lidar spreads its beams over slightly more than verticalFieldOfView: these angles are
# fitted from its own point cloud (docs/webots-sensor-diagnostics.md, W4); top layer first.
ROOF16_ELEVATIONS = tuple(0.265282 - i * 0.0353709 for i in range(16))
FLAG_NEAR_EDGE, FLAG_FRAGMENT, FLAG_TOUCHES_EGO_ZONE = 1, 2, 4


class Config:
    def __init__(self, sensor, mount, width, layers, fov, az0, az_step, min_range, max_range, elevations,
                 encoding=FLOAT32, scale=0.01):
        self.sensor, self.mount, self.width, self.layers = sensor, tuple(mount), width, layers
        self.fov, self.az0, self.az_step = fov, az0, az_step
        self.min_range, self.max_range, self.encoding, self.scale = min_range, max_range, encoding, scale
        self.elevations = tuple(elevations)
        assert len(self.elevations) == layers

    @classmethod
    def webots(cls, sensor, mount, width, layers, fov, min_range, max_range, elevations, **kw):
        """Webots range images: columns left to right at fov/2 - (i + 0.5) fov/width (verified, W4)."""
        return cls(sensor, mount, width, layers, fov, fov / 2 - 0.5 * fov / width, -fov / width, min_range,
                   max_range, elevations, **kw)

    def body(self):
        head = _CFG.pack(b"RSLC", VERSION, _CFG.size + 4 * self.layers, 0, self.sensor.encode()[:16], *self.mount,
                         self.width, self.layers, self.fov, self.az0, self.az_step, self.min_range, self.max_range,
                         self.encoding, self.scale)
        return head + struct.pack("<%df" % self.layers, *self.elevations)

    @property
    def config_id(self):
        return zlib.crc32(self.body())

    def encode(self):
        b = bytearray(self.body())
        struct.pack_into("<I", b, 8, self.config_id)
        return bytes(b)

    @classmethod
    def decode(cls, data):
        f = _CFG.unpack_from(data)
        if f[0] != b"RSLC" or f[1] != VERSION:
            raise ValueError("not a version %d lidar config" % VERSION)
        layers = f[12]
        cfg = cls(f[4].rstrip(b"\0").decode(), f[5:11], f[11], layers, f[13], f[14], f[15], f[16], f[17],
                  struct.unpack_from("<%df" % layers, data, _CFG.size), f[18], f[19])
        if cfg.config_id != f[3]:
            raise ValueError("config_id does not match the config body")
        return cfg


def encode_frame(cfg, ranges, seq, t_capture, compress=False, scan_s=0.0):
    """ranges: (layers, width) metres; inf, NaN, <= 0 or beyond max_range = no return."""
    r = np.asarray(ranges, dtype=np.float64).reshape(cfg.layers, cfg.width)
    valid = np.isfinite(r) & (r > 0) & (r <= cfg.max_range)
    flags = 0
    if cfg.encoding == UINT16:
        q = np.where(valid, np.clip(np.rint(r / cfg.scale), 1, 65535), 0).astype("<u2")
        payload = q.tobytes()
    else:
        payload = np.where(valid, r, np.inf).astype("<f4").tobytes() + np.packbits(valid).tobytes()
        flags |= MASK
    if compress:
        payload, flags = zlib.compress(payload, 6), flags | ZLIB
    return _FRM.pack(b"RSLF", VERSION, flags, cfg.config_id, seq, t_capture, scan_s, len(payload)) + payload


def decode_frame(cfg, data):
    """-> (seq, t_capture, ranges (layers, width) float64 with NaN for no return, valid mask)."""
    magic, version, flags, config_id, seq, t, _scan, n = _FRM.unpack_from(data)
    if magic != b"RSLF" or version != VERSION:
        raise ValueError("not a version %d lidar frame" % VERSION)
    if config_id != cfg.config_id:
        raise ValueError("frame belongs to another lidar configuration")
    payload = data[_FRM.size:_FRM.size + n]
    if flags & ZLIB:
        payload = zlib.decompress(payload)
    cells = cfg.layers * cfg.width
    if cfg.encoding == UINT16:
        q = np.frombuffer(payload, "<u2", cells).astype(np.float64)
        valid = q > 0
        r = q * cfg.scale
    else:
        r = np.frombuffer(payload, "<f4", cells).astype(np.float64)
        valid = np.unpackbits(np.frombuffer(payload, np.uint8, offset=4 * cells), count=cells).astype(bool)
    r = np.where(valid, r, np.nan)
    return seq, t, r.reshape(cfg.layers, cfg.width), valid.reshape(cfg.layers, cfg.width)


def points(cfg, ranges, attitude=(0.0, 0.0)):
    """Ego-frame points (N, 3) and their (layer, column) from a range image (mount yaw only: roll = pitch = 0).
    attitude: the vehicle's (roll, pitch) from its IMU; the points are levelled with it (a braking car pitches by
    several degrees, which otherwise puts the road surface into the obstacle height band)."""
    r = np.asarray(ranges, dtype=np.float64)
    ok = np.isfinite(r) & (r > 0)
    layer, col = np.nonzero(ok)
    rr = r[ok]
    az = cfg.az0 + col * cfg.az_step
    el = np.asarray(cfg.elevations)[layer]
    x, y, z = rr * np.cos(el) * np.cos(az), rr * np.cos(el) * np.sin(az), rr * np.sin(el)
    mx, my, mz, _roll, _pitch, yaw = cfg.mount
    c, s = math.cos(yaw), math.sin(yaw)
    pts = np.stack([mx + c * x - s * y, my + s * x + c * y, mz + z], 1)
    roll, pitch = attitude
    if roll or pitch:  # level frame = Ry(pitch) Rx(roll) vehicle frame (Webots roll-pitch-yaw)
        cr, sr, cp, sp = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch)
        rot = np.array([[cp, sp * sr, sp * cr], [0, cr, -sr], [-sp, cp * sr, cp * cr]])
        pts = pts @ rot.T
    return pts, layer, col


def ground_height(pts, layer, lowest_layers=4, near=3.0, far=25.0):
    """Ground height under the ego: the median height of the lowest beams' returns 3-25 m away. They reach the road
    a few metres out unless something stands there; the median ignores a vehicle covering part of the ring.
    ponytail: one flat plane; per-column slope segmentation if roads with grade or camber matter."""
    rho = np.hypot(pts[:, 0], pts[:, 1])
    sel = (layer >= layer.max() - lowest_layers + 1) & (rho > near) & (rho < far) if len(layer) else layer
    return float(np.median(pts[sel, 2])) if np.count_nonzero(sel) >= 20 else None


def clusters(pts, cell=0.4):
    """2D connected components (8-neighbour) on a grid: deterministic; index arrays into pts."""
    if len(pts) == 0:
        return []
    keys = np.floor(pts[:, :2] / cell).astype(np.int64)
    cells = defaultdict(list)
    for i, k in enumerate(map(tuple, keys)):
        cells[k].append(i)
    seen, out = set(), []
    for k in cells:
        if k in seen:
            continue
        stack, members = [k], []
        seen.add(k)
        while stack:
            c = stack.pop()
            members.extend(cells[c])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (c[0] + dx, c[1] + dy)
                    if nb in cells and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        out.append(np.asarray(members))
    return out


def process(cfg, ranges, ego_box, min_points=3, height=(0.2, 4.0), ground=None, attitude=(0.0, 0.0)):
    """Range image -> obstacle clusters. ego_box = (rear x, front x, half width) of the ego's own body, removed with
    a 0.3 m margin. Heights are above the estimated ground; points higher than height[1] (gantries, signs) and
    lower than height[0] (road) are not obstacles. Each cluster: dict of near point (the obstacle point closest to
    the ego's footprint: what clearance depends on), its extent (visible surfaces only: a lower bound on the body),
    point count, a confidence, per-axis 1-sigma uncertainties of the near point from the beam spacing there, and
    near_range_interval: horizontal range from the sensor within which the object's nearest surface lies."""
    pts, layer, col = points(cfg, ranges, attitude)
    g = ground_height(pts, layer) if ground is None else ground
    if g is None:
        return {"ground": None, "clusters": [], "obstacle_points": 0}
    rear, front, half = ego_box
    h = pts[:, 2] - g
    ego = (pts[:, 0] > rear - 0.3) & (pts[:, 0] < front + 0.3) & (np.abs(pts[:, 1]) < half + 0.3)
    keep = (h > height[0]) & (h < height[1]) & ~ego
    obj, ocol, olayer = pts[keep], col[keep], layer[keep]
    el = np.asarray(cfg.elevations)
    out = []
    az_step, el_step = abs(cfg.az_step), abs(cfg.elevations[0] - cfg.elevations[-1]) / max(1, cfg.layers - 1)
    for idx in clusters(obj):
        if len(idx) < min_points:
            continue
        c = obj[idx]
        dx = np.maximum(np.maximum(rear - c[:, 0], c[:, 0] - front), 0)
        dy = np.maximum(np.abs(c[:, 1]) - half, 0)
        k = int(np.argmin(np.hypot(dx, dy)))
        near = c[k]
        rng = float(math.hypot(near[0] - cfg.mount[0], near[1] - cfg.mount[1]))
        # 1-sigma: half the gap between neighbouring beams at that range, plus range noise; along the line of sight
        # the face can also lie up to one layer gap beyond the visible return (low structure between layers)
        lateral_sigma = 0.5 * rng * az_step + 0.02
        unseen_gap = rng * el_step  # vertical gap between layers there: structure inside it is not observed
        edge = bool(np.any(ocol[idx] == 0) or np.any(ocol[idx] == cfg.width - 1)) if cfg.fov < 6.28 else False
        # Low structure in front of the visible face (a truck's chassis in the gap between two layers): in each
        # column through the face whose lowest return is above the road, the beam below reached the road at some
        # range and nothing between there and the face was seen below the next beam. The face may be that close.
        horiz = np.hypot(c[:, 0] - cfg.mount[0], c[:, 1] - cfg.mount[1])
        cols, lay = ocol[idx], olayer[idx]
        hidden_from = rng
        for column in np.unique(cols[horiz < horiz.min() + 1.0]):
            on = cols == column
            low = int(np.argmax(np.where(on, lay, -1)))  # the lowest beam (highest layer index) on this column
            below = lay[low] + 1
            if c[low, 2] - g > 0.3 and below < cfg.layers and el[below] < 0:
                hidden_from = min(hidden_from, max(0.0, (cfg.mount[2] - g) / math.tan(-el[below])))
        face_low = float(c[horiz < horiz.min() + 1.0, 2].min() - g)  # lowest visible point of the near face
        out.append({"near": (float(near[0]), float(near[1])), "x": (float(c[:, 0].min()), float(c[:, 0].max())),
                    "y": (float(c[:, 1].min()), float(c[:, 1].max())), "z": (float(c[:, 2].min() - g),
                                                                            float(c[:, 2].max() - g)),
                    "n": int(len(c)), "range": rng, "confidence": float(1 - math.exp(-len(c) / 8.0)),
                    "sigma_lateral": lateral_sigma, "sigma_line_of_sight": 0.05, "unseen_vertical_gap": unseen_gap,
                    "near_range_interval": (float(hidden_from), rng), "face_low": face_low,
                    "near_edge": edge})
    return {"ground": g, "clusters": out, "obstacle_points": int(keep.sum())}


def zone_occupied(clusters_, zone):
    """Any cluster with its extent overlapping zone = (x min, x max, y min, y max), ego frame."""
    x0, x1, y0, y1 = zone
    return any(c["x"][0] <= x1 and c["x"][1] >= x0 and c["y"][0] <= y1 and c["y"][1] >= y0 for c in clusters_)


def encode_objects(cfg, seq, t_capture, clusters_):
    cm = lambda v: int(max(-32768, min(32767, round(v * 100))))  # noqa: E731
    body = b"".join(_CLU.pack(cm(c["near"][0]), cm(c["near"][1]), cm(c["x"][0]), cm(c["x"][1]), cm(c["y"][0]),
                              cm(c["y"][1]), cm(c["z"][0]), cm(c["z"][1]), 0, min(c["n"], 65535),
                              int(round(255 * c["confidence"])), FLAG_NEAR_EDGE if c.get("near_edge") else 0)
                    for c in clusters_)
    return _OBJ.pack(b"RSLO", VERSION, len(clusters_), cfg.config_id, seq, t_capture) + body


def decode_objects(data):
    magic, version, count, config_id, seq, t = _OBJ.unpack_from(data)
    if magic != b"RSLO" or version != VERSION:
        raise ValueError("not a version %d object list" % VERSION)
    out = []
    for i in range(count):
        f = _CLU.unpack_from(data, _OBJ.size + i * _CLU.size)
        out.append({"near": (f[0] / 100, f[1] / 100), "x": (f[2] / 100, f[3] / 100), "y": (f[4] / 100, f[5] / 100),
                    "z": (f[6] / 100, f[7] / 100), "n": f[9], "confidence": f[10] / 255, "near_edge": bool(f[11] & 1)})
    return config_id, seq, t, out


SEEN_THROUGH, OCCUPIED, OCCLUDED, UNKNOWN = "seen_through", "occupied", "occluded", "unknown"


def free_space(cfg, ranges, ground, lon, lat, radius=1.0, height=(0.4, 2.0)):
    """What the range image says about a disc of `radius` at ego-frame (lon, lat), for an object standing between
    height[0] and height[1] above the ground: OCCUPIED (a return inside the disc), SEEN_THROUGH (beams that would hit
    such an object there returned from beyond it, and none inside), OCCLUDED (they returned from nearer: something in
    front) or UNKNOWN (no beam crosses that height band there, or no returns: sky, out of range, absorbing). Absence
    of a return is never taken as an empty road."""
    if ground is None:
        return UNKNOWN
    mx, my, mz = cfg.mount[0], cfg.mount[1], cfg.mount[2]
    dx, dy = lon - mx, lat - my
    rho = math.hypot(dx, dy)
    if rho <= radius:
        return UNKNOWN
    az = math.atan2(dy, dx) - cfg.mount[5]
    half = math.asin(min(1.0, radius / rho))
    c0 = (az - half - cfg.az0) / cfg.az_step
    c1 = (az + half - cfg.az0) / cfg.az_step
    lo, hi = int(math.floor(min(c0, c1))), int(math.ceil(max(c0, c1)))
    cols = [c % cfg.width for c in range(lo, hi + 1)] if cfg.fov > 6.28 else \
        [c for c in range(lo, hi + 1) if 0 <= c < cfg.width]
    seen = {OCCUPIED: 0, SEEN_THROUGH: 0, OCCLUDED: 0}
    r = np.asarray(ranges)
    for layer, el in enumerate(cfg.elevations):
        z = mz + rho * math.tan(el) - ground
        if not height[0] <= z <= height[1]:
            continue
        for c in cols:
            v = r[layer, c]
            if not np.isfinite(v):
                continue
            h = v * math.cos(el)
            if h > rho + radius:
                seen[SEEN_THROUGH] += 1
            elif h >= rho - radius:
                seen[OCCUPIED] += 1
            else:
                seen[OCCLUDED] += 1
    if seen[OCCUPIED]:
        return OCCUPIED
    if seen[SEEN_THROUGH] >= 2 and seen[SEEN_THROUGH] >= seen[OCCLUDED]:
        return SEEN_THROUGH
    return OCCLUDED if seen[OCCLUDED] else UNKNOWN
