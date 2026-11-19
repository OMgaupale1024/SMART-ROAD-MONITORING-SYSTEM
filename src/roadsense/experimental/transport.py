"""Experimental sensor-frame transport: one versioned binary frame per perception cycle (RSSF version 1).

The existing JSON interface (roadsense.sensors.v1, pipeline.SENSOR_SCHEMA) is unchanged and still what the
authoritative pipeline reads; from_v1/to_v1 convert between the two, so either side can be fed from the other.

Frame (little-endian):

  header   "RSSF" u16 version u16 flags u32 seq d t_frame (s, sensor clock: when the frame was assembled)
           u16 sections u16 reserved u32 total_bytes u32 crc32 (of the whole frame with this field zero)
  section  u8 kind u8 health u16 reserved d t_capture (s) u32 payload_bytes, payload

  kind 1 GNSS   d x d y d z f vx f vy f speed                         (world frame, m, m/s)
  kind 2 IMU    f roll f pitch f yaw                                   (rad)
  kind 3 RADAR  32s name 6f mount (x y z roll pitch yaw, ego frame) f min_range f max_range f hfov f vfov u16 n,
                n x (f distance f azimuth f range_rate f power u8 flags)  (Webots convention: azimuth positive to the
                radar's right; flags 1 = range rate valid as reported (finite), 2 = power valid)
  kind 4 LIDAR_CONFIG  a lidar.Config (RSLC), sent in the first frame and every CONFIG_EVERY frames
  kind 5 LIDAR_FRAME   a lidar range image (RSLF), naming its config by config_id

health: 0 OK, 1 NO_DATA (alive, nothing this cycle), 2 FAILED (outage). An absent section is a missing sensor.

Receiver.accept() is the only way a frame reaches perception: it rejects (and counts) frames that are truncated, too
large, corrupt, of another version, out of order or duplicated, and drops sections that are stale, from the future,
from an unknown or changed sensor configuration, or malformed, so none of them can enter fusion unnoticed.
"""
import math
import struct
import zlib
from typing import NamedTuple, Optional

from . import lidar

VERSION = 1
MAGIC = b"RSSF"
GNSS, IMU, RADAR, LIDAR_CONFIG, LIDAR_FRAME = 1, 2, 3, 4, 5
OK, NO_DATA, FAILED = 0, 1, 2
RR_VALID, POWER_VALID = 1, 2
MAX_FRAME_BYTES = 256 * 1024  # a uint16 + zlib range image is ~20 kB; raw float32 would be 115 kB
CONFIG_EVERY = 100  # frames between repeated lidar configurations (10 s at 10 Hz), so a late receiver can decode

_HDR = struct.Struct("<4sHHIdHHII")
_SEC = struct.Struct("<BBHdI")
_GNSS = struct.Struct("<dddfff")
_IMU = struct.Struct("<fff")
_RAD = struct.Struct("<32s6fffffH")
_RET = struct.Struct("<ffffB")


class RadarReturn(NamedTuple):
    distance: float
    azimuth: float  # rad, positive to the radar's right (Webots)
    range_rate: Optional[float]  # m/s, negative = closing; None if not reported as a finite number
    power: Optional[float]  # received power, dB


class RadarScan(NamedTuple):
    name: str
    t: float
    health: int
    mount: tuple  # x, y, z, roll, pitch, yaw (ego frame)
    limits: tuple  # min range, max range, horizontal FOV, vertical FOV
    returns: tuple


class Frame(NamedTuple):
    seq: int
    t: float
    gnss: Optional[dict]
    imu: Optional[dict]
    radars: dict  # name -> RadarScan
    lidar: Optional[dict]  # {"t", "health", "config", "ranges", "valid", "seq"}
    dropped: tuple  # (sensor, reason) for every section the receiver refused


def _section(kind, health, t, payload):
    return _SEC.pack(kind, health, 0, t, len(payload)) + payload


def radar_section(scan):
    name = scan.name.encode()
    if len(name) > 32:
        raise ValueError("radar name longer than 32 bytes: %r" % scan.name)
    body = _RAD.pack(name, *scan.mount, *scan.limits, len(scan.returns))
    for r in scan.returns:
        rr = r.range_rate if r.range_rate is not None and math.isfinite(r.range_rate) else float("nan")
        pw = r.power if r.power is not None and math.isfinite(r.power) else float("nan")
        body += _RET.pack(r.distance, r.azimuth, rr, pw, (RR_VALID if math.isfinite(rr) else 0)
                          | (POWER_VALID if math.isfinite(pw) else 0))
    return _section(RADAR, scan.health, scan.t, body)


def encode(seq, t, gnss=None, imu=None, radars=(), lidar_config=None, lidar_frame=None, lidar_t=None,
           lidar_health=OK):
    """One frame. gnss: {"t", "position" (x, y, z), "velocity" (vx, vy), "speed", "health"}; imu: {"t", "rpy",
    "health"}; radars: RadarScan list; lidar_config: a lidar.Config to include; lidar_frame: RSLF bytes."""
    sections = []
    if gnss is not None:
        sections.append(_section(GNSS, gnss.get("health", OK), gnss["t"],
                                 _GNSS.pack(*gnss["position"][:3], *gnss["velocity"][:2], gnss["speed"])))
    if imu is not None:
        sections.append(_section(IMU, imu.get("health", OK), imu["t"], _IMU.pack(*imu["rpy"][:3])))
    sections += [radar_section(s) for s in radars]
    if lidar_config is not None:
        sections.append(_section(LIDAR_CONFIG, OK, t, lidar_config.encode()))
    if lidar_frame is not None or lidar_health != OK:
        sections.append(_section(LIDAR_FRAME, lidar_health, t if lidar_t is None else lidar_t, lidar_frame or b""))
    body = b"".join(sections)
    total = _HDR.size + len(body)
    head = _HDR.pack(MAGIC, VERSION, 0, seq, t, len(sections), 0, total, 0)
    crc = zlib.crc32(head + body)
    return head[:-4] + struct.pack("<I", crc) + body


class Rejected(ValueError):
    pass


class Receiver:
    """Validates frames for one perception consumer. radar_mounts: name -> expected mount (6 floats); a scan whose
    mount differs by more than mount_tol (m or rad) is from another configuration and is dropped. max_age_s: a
    section captured longer than this before its frame's time is stale; one stamped after it is from the future."""

    def __init__(self, radar_mounts, max_age_s=0.25, future_tol_s=0.005, mount_tol=0.01,
                 max_frame_bytes=MAX_FRAME_BYTES):
        self.radar_mounts, self.max_age_s, self.future_tol_s = dict(radar_mounts), max_age_s, future_tol_s
        self.mount_tol, self.max_frame_bytes = mount_tol, max_frame_bytes
        self.lidar_configs = {}  # config_id -> lidar.Config, from LIDAR_CONFIG sections
        self.last_seq = None
        self.counts = {"accepted": 0, "rejected": {}, "dropped_sections": {}, "sequence_gaps": 0}

    def _count(self, table, reason):
        self.counts[table][reason] = self.counts[table].get(reason, 0) + 1

    def accept(self, data):
        """bytes -> Frame, or raises Rejected (counted)."""
        try:
            frame = self._parse(data)
        except Rejected as e:
            self._count("rejected", str(e).split(":")[0])
            raise
        self.counts["accepted"] += 1
        for _sensor, reason in frame.dropped:
            self._count("dropped_sections", reason)
        return frame

    def _parse(self, data):
        if len(data) > self.max_frame_bytes:
            raise Rejected("too_large: %d bytes" % len(data))
        if len(data) < _HDR.size:
            raise Rejected("truncated: header")
        magic, version, _flags, seq, t, n, _r, total, crc = _HDR.unpack_from(data)
        if magic != MAGIC:
            raise Rejected("not_rssf")
        if version != VERSION:
            raise Rejected("version: %d" % version)
        if total != len(data):
            raise Rejected("truncated: %d of %d bytes" % (len(data), total))
        if zlib.crc32(data[:_HDR.size - 4] + b"\0\0\0\0" + data[_HDR.size:]) != crc:
            raise Rejected("corrupt: crc")
        if not math.isfinite(t):
            raise Rejected("corrupt: time")
        if self.last_seq is not None and seq <= self.last_seq:
            raise Rejected("out_of_order: seq %d after %d" % (seq, self.last_seq))
        if self.last_seq is not None and seq > self.last_seq + 1:
            self.counts["sequence_gaps"] += seq - self.last_seq - 1
        gnss = imu = lidar_sec = None
        radars, dropped = {}, []
        off = _HDR.size
        sections = []
        for _ in range(n):
            if off + _SEC.size > len(data):
                raise Rejected("truncated: section")
            kind, health, _r, tc, size = _SEC.unpack_from(data, off)
            off += _SEC.size
            if off + size > len(data):
                raise Rejected("truncated: payload")
            sections.append((kind, health, tc, data[off:off + size]))
            off += size
        if off != len(data):
            raise Rejected("corrupt: trailing bytes")
        self.last_seq = seq  # a structurally valid frame: later ones must follow it
        for kind, _h, _tc, payload in sections:  # configurations first: a frame may carry its own
            if kind == LIDAR_CONFIG:
                try:
                    cfg = lidar.Config.decode(payload)
                except (ValueError, struct.error):
                    dropped.append(("lidar", "bad_config"))
                    continue
                self.lidar_configs[cfg.config_id] = cfg
        for kind, health, tc, payload in sections:
            sensor = {GNSS: "gnss", IMU: "imu", LIDAR_FRAME: "lidar"}.get(kind, "radar")
            if kind == LIDAR_CONFIG:
                continue
            if not math.isfinite(tc) or tc > t + self.future_tol_s:
                dropped.append((sensor, "future"))
                continue
            if t - tc > self.max_age_s:
                dropped.append((sensor, "stale"))
                continue
            try:
                if kind == GNSS:
                    x, y, z, vx, vy, speed = _GNSS.unpack(payload)
                    gnss = {"t": tc, "health": health, "position": (x, y, z), "velocity": (vx, vy), "speed": speed}
                elif kind == IMU:
                    imu = {"t": tc, "health": health, "rpy": _IMU.unpack(payload)}
                elif kind == RADAR:
                    scan = self._radar(tc, health, payload)
                    expected = self.radar_mounts.get(scan.name)
                    if expected is None:
                        dropped.append((scan.name, "unknown_sensor"))
                    elif max(abs(a - b) for a, b in zip(scan.mount, expected)) > self.mount_tol:
                        dropped.append((scan.name, "mount_changed"))
                    else:
                        radars[scan.name] = scan
                elif kind == LIDAR_FRAME:
                    if health != OK or not payload:
                        lidar_sec = {"t": tc, "health": health if health != OK else NO_DATA}
                        continue
                    cid = struct.unpack_from("<I", payload, 8)[0]
                    cfg = self.lidar_configs.get(cid)
                    if cfg is None:
                        dropped.append(("lidar", "unknown_config"))
                        continue
                    lseq, lt, ranges, valid = lidar.decode_frame(cfg, payload)
                    if abs(lt - tc) > self.future_tol_s:
                        dropped.append(("lidar", "time_mismatch"))
                        continue
                    lidar_sec = {"t": tc, "health": OK, "config": cfg, "ranges": ranges, "valid": valid, "seq": lseq}
                else:
                    dropped.append(("kind%d" % kind, "unknown_kind"))
            except (struct.error, ValueError, zlib.error):
                dropped.append((sensor, "malformed"))
        return Frame(seq, t, gnss, imu, radars, lidar_sec, tuple(dropped))

    @staticmethod
    def _radar(tc, health, payload):
        f = _RAD.unpack_from(payload)
        n = f[-1]
        if _RAD.size + n * _RET.size != len(payload):
            raise ValueError("radar payload size")
        returns = []
        for i in range(n):
            d, az, rr, pw, flags = _RET.unpack_from(payload, _RAD.size + i * _RET.size)
            if not (math.isfinite(d) and math.isfinite(az)):
                raise ValueError("radar return")
            returns.append(RadarReturn(d, az, rr if flags & RR_VALID else None, pw if flags & POWER_VALID else None))
        return RadarScan(f[0].rstrip(b"\0").decode(), tc, health, tuple(f[1:7]), tuple(f[7:11]), tuple(returns))


# ---------------------------------------------------------------------------------------------------------------
# the existing JSON interface (roadsense.sensors.v1), both ways

def from_v1(frame, seq, radar_mounts, vfov=0.07854):
    """A roadsense.sensors.v1 dict -> RSSF bytes (radar, GNSS from the ego's GPS, IMU yaw). radar_mounts: name ->
    (x, y, z, roll, pitch, yaw), e.g. from perception.RADARS. objects and sides have no RSSF section yet."""
    t, ego = frame["timestamp"], frame["ego"]
    scans = []
    for name, radar in frame["radars"].items():
        lo, hi, fov = radar["limits"]
        scans.append(RadarScan(name, t, OK if radar["targets"] else NO_DATA, tuple(radar_mounts[name]),
                               (lo, hi, fov, vfov), tuple(RadarReturn(d, az, s, None) for d, az, s in radar["targets"])))
    return encode(seq, t, gnss={"t": t, "position": ego["position"], "velocity": ego["velocity"],
                                "speed": ego["speed_mps"]},
                  imu={"t": t, "rpy": (0.0, 0.0, ego["yaw_rad"])}, radars=scans)


def to_v1(frame, ego_extra=None):
    """A received Frame -> the roadsense.sensors.v1 dict the authoritative pipeline reads (objects empty, sides
    unknown). ego_extra supplies what RSSF does not carry: vehicle_id, steering_rad, maneuver, lane."""
    extra = {"vehicle_id": "EGO_ROADSENSE", "steering_rad": 0.0, "maneuver": "lane_keep", "lane": None}
    extra.update(ego_extra or {})
    g = frame.gnss
    return {"schema": "roadsense.sensors.v1", "timestamp": frame.t,
            "ego": dict(extra, position=list(g["position"]), velocity=list(g["velocity"]), speed_mps=g["speed"],
                        yaw_rad=frame.imu["rpy"][2]),
            "radars": {name: {"targets": [(r.distance, r.azimuth, r.range_rate) for r in s.returns],
                              "limits": s.limits[:3]} for name, s in frame.radars.items()},
            "objects": [], "sides": None}
