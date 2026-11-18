"""Experimental multi-object tracker (shadow mode only; the authoritative autonomy.tracking.Tracker is unchanged).

State per track: world-frame position and velocity of the object's body centre, [x, y, vx, vy], with covariance
(constant-velocity model, white acceleration noise along and across the ego's heading). Measurements arrive as
per-sensor batches stamped with their capture time and are applied in time order (asynchronous sensors, ego motion
compensated by working in the world frame with the ego pose of each batch):

- radar: range and bearing from the radar's mount (EKF), then range rate as a separate gated scalar update;
- lidar: a cluster's body-centre estimate (visible extent plus the track's extent estimate), linear.

Association per batch: Mahalanobis gating (chi-square, 99.7%) and a global assignment (Hungarian, cost d^2 + ln|S|).
Lidar clusters left over that lie inside a confirmed track's known extent join that track as fragments (a truck's
cab and chassis) instead of starting a second track; clusters outside it never do.

Lifecycle: existence log-odds. A hit raises it. A miss lowers it only when a healthy sensor should have seen the
track: inside its field of view and range, not behind a nearer track (radar), or with lidar beams seen passing
through the spot. Out of view, occluded or sensor outage: no penalty, the track coasts with growing covariance until
MAX_COAST_S or its position uncertainty exceeds MAX_SIGMA_M. Tentative -> confirmed after CONFIRM_HITS hits within
CONFIRM_WINDOW_S, or hits from both radar and lidar. A sensor's miss never cancels another sensor's hit.
"""
import math
from typing import NamedTuple

import numpy as np

from . import radar as radar_model

CHI2_2D = 11.83  # 99.73%, 2 degrees of freedom
CHI2_1D = 9.0
ACCEL_ALONG = 3.0  # m/s^2, white acceleration noise along the ego's heading (vehicles follow the road)
ACCEL_ACROSS = 1.0
INIT_SIGMA_V_ALONG = 15.0
INIT_SIGMA_V_ACROSS = 2.0
SIGMA_RANGE_RATE = 0.30
CONFIRM_HITS, CONFIRM_WINDOW_S = 3, 0.55
CROSS_SENSOR_WINDOW_S = 0.35
TENTATIVE_TIMEOUT_S = 0.35
CONFIRM_SIGMA_V_MPS = 2.0  # a track is confirmed only once its velocity is known this well (lidar-born tracks
# start with none; reporting a velocity guessed from 2-3 centre positions caused false collision courses)
MAX_VEHICLE_WIDTH_M = 2.6
LIDAR_BIRTH_MIN_POINTS = 8  # a vehicle within 40 m returns 20+ points; sparse far objects are radar's to start
MAX_COAST_S = 3.0
MAX_SIGMA_M = 4.0
L_HIT, L_MAX, L_DELETE = 1.5, 6.0, -1.0
PD = {"radar": 0.5, "lidar": 0.97}  # detection probability when expected to see it: a miss costs ln(1 - pd)
PRIOR_LENGTH_M, PRIOR_LENGTH_SIGMA_M = 4.5, 3.0  # the traffic mix: 2 m motorcycles to 10-12 m buses
PRIOR_WIDTH_M, PRIOR_WIDTH_SIGMA_M = 1.8, 0.5
GROUP_SPEED_MPS, GROUP_GAP_M, GROUP_HOLD_S = 0.7, 6.0, 1.0


def assign(cost):
    """Minimum-cost assignment (Hungarian, shortest augmenting paths), rectangular cost -> {row: column}.
    Entries >= 1e8 are forbidden."""
    cost = np.asarray(cost, dtype=float)
    n_r, n_c = cost.shape
    if n_r == 0 or n_c == 0:
        return {}
    n = max(n_r, n_c)
    big = 1e9
    a = np.full((n + 1, n + 1), big)
    a[1:n_r + 1, 1:n_c + 1] = np.minimum(cost, big)
    u, v = np.zeros(n + 1), np.zeros(n + 1)
    p, way = np.zeros(n + 1, dtype=int), np.zeros(n + 1, dtype=int)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv = np.full(n + 1, np.inf)
        used = np.zeros(n + 1, dtype=bool)
        while True:
            used[j0] = True
            i0 = p[j0]
            cur = a[i0, 1:] - u[i0] - v[1:]
            free = ~used[1:]
            upd = free & (cur < minv[1:])
            minv[1:][upd] = cur[upd]
            way[1:][upd] = j0
            cand = np.where(free, minv[1:], np.inf)
            j1 = int(np.argmin(cand)) + 1
            delta = cand[j1 - 1]
            u[p[used]] += delta
            v[used] -= delta
            minv[1:][free] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    out = {}
    for j in range(1, n + 1):
        i = p[j]
        if 1 <= i <= n_r and 1 <= j <= n_c and cost[i - 1, j - 1] < 1e8:
            out[i - 1] = j - 1
    return out


class EgoState(NamedTuple):
    t: float
    x: float
    y: float
    yaw: float
    vx: float
    vy: float

    def to_world(self, lon, lat):
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return self.x + lon * c - lat * s, self.y + lon * s + lat * c

    def to_ego(self, x, y):
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        dx, dy = x - self.x, y - self.y
        return dx * c + dy * s, dy * c - dx * s

    def rot(self):
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return np.array([[c, -s], [s, c]])


class LidarCluster(NamedTuple):
    """One lidar cluster in the ego frame (lidar.process output, reduced)."""
    t: float
    near: tuple
    x: tuple
    y: tuple
    n: int
    clearance: tuple  # (lo, hi) m from the ego's body (lo includes structure hidden between layers)
    sigma: float  # 1-sigma of the visible surface position


def lidar_centre(c, length, width, sigma_length, sigma_width, ego_box=radar_model.EGO_BOX):
    """Body-centre estimate (ego frame) and its 2x2 covariance from a cluster's visible extent and an extent
    estimate: ahead, the visible near face is the body's rear; behind, its front; alongside, its near side."""
    rear, front, half = ego_box
    (x0, x1), (y0, y1) = c.x, c.y
    vis_l, vis_w = x1 - x0, y1 - y0
    s = c.sigma
    if x0 > front - 0.5 and vis_l < length:
        cx, sx = x0 + length / 2, math.hypot(s, sigma_length / 2)
    elif x1 < rear + 0.5 and vis_l < length:
        cx, sx = x1 - length / 2, math.hypot(s, sigma_length / 2)
    else:
        cx, sx = (x0 + x1) / 2, math.hypot(s, max(0.0, length - vis_l) / math.sqrt(12) + 0.05)
    if y0 > half and vis_w < width:
        cy, sy = y0 + width / 2, math.hypot(s, sigma_width / 2)
    elif y1 < -half and vis_w < width:
        cy, sy = y1 - width / 2, math.hypot(s, sigma_width / 2)
    else:
        cy, sy = (y0 + y1) / 2, math.hypot(s, max(0.0, width - vis_w) / math.sqrt(12) + 0.05)
    return np.array([cx, cy]), np.diag([sx * sx, sy * sy])


class Track:
    def __init__(self, tid, t, xy, P_xy, ego, kind):
        self.id, self.t, self.status = tid, t, "tentative"
        self.x = np.array([xy[0], xy[1], ego.vx, ego.vy])
        R = ego.rot()
        Pv = R @ np.diag([INIT_SIGMA_V_ALONG ** 2, INIT_SIGMA_V_ACROSS ** 2]) @ R.T
        self.P = np.zeros((4, 4))
        self.P[:2, :2], self.P[2:, 2:] = P_xy, Pv
        self.first, self.last_hit = t, t
        self.hits = [(t, kind)]
        self.last_by = {kind: t}
        self.existence = L_HIT
        self.length, self.width = 0.0, 0.0  # largest visible extents (lidar)
        self.seen_alongside = False
        self.clearance = {}  # kind -> (t, lo, hi)
        self.expected_misses = 0
        self.group = tid
        self.suspect_only = False
        self.rr_rejected = 0

    def predict(self, t, yaw):
        dt = t - self.t
        if dt <= 0:
            return
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        c, s = math.cos(yaw), math.sin(yaw)
        R = np.array([[c, -s], [s, c]])
        qa = R @ np.diag([ACCEL_ALONG ** 2, ACCEL_ACROSS ** 2]) @ R.T
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + G @ qa @ G.T
        self.t = t

    def sigma_pos(self):
        return math.sqrt(max(np.linalg.eigvalsh(self.P[:2, :2])))

    def extent(self):
        """(length, width, sigma_length, sigma_width) used to place lidar centres."""
        if self.seen_alongside:
            length, sl = max(self.length, 0.5), 0.3
        else:
            length, sl = max(self.length, PRIOR_LENGTH_M), PRIOR_LENGTH_SIGMA_M
        width = max(self.width, PRIOR_WIDTH_M if self.width < 0.5 else self.width)
        return length, width, sl, PRIOR_WIDTH_SIGMA_M if self.width < 0.5 else 0.2

    def hit(self, t, kind):
        self.last_hit = t
        self.last_by[kind] = t
        self.hits.append((t, kind))
        self.hits = [h for h in self.hits if t - h[0] <= max(CONFIRM_WINDOW_S, CROSS_SENSOR_WINDOW_S) + 1e-6]
        self.existence = min(L_MAX, self.existence + L_HIT)
        self.expected_misses = 0
        if self.status == "tentative" and not self.suspect_only:
            recent = [h for h in self.hits if t - h[0] <= CONFIRM_WINDOW_S + 1e-6]
            kinds = {k for tt, k in self.hits if t - tt <= CROSS_SENSOR_WINDOW_S + 1e-6}
            settled = math.sqrt(max(np.linalg.eigvalsh(self.P[2:, 2:]))) < CONFIRM_SIGMA_V_MPS
            if settled and (len(recent) >= CONFIRM_HITS or len(kinds) >= 2):
                self.status = "confirmed"


DUPLICATE_M = 1.0  # centre distance below which two tracks are the same vehicle
DUPLICATE_SPEED_MPS = 2.0
PREGATE_M = 12.0  # farther than this (Euclidean) from a track's predicted position: never the same object


def _d2(y, S):
    """y' S^-1 y and ln|S| for a 2x2 S."""
    det = S[0, 0] * S[1, 1] - S[0, 1] * S[1, 0]
    if det <= 0:
        return float("inf"), 0.0
    return float((S[1, 1] * y[0] * y[0] - 2 * S[0, 1] * y[0] * y[1] + S[0, 0] * y[1] * y[1]) / det), math.log(det)


def _update(track, z, h, H, R, angle_index=None):
    y = z - h
    if angle_index is not None:
        y[angle_index] = math.remainder(y[angle_index], 2 * math.pi)
    S = H @ track.P @ H.T + R
    K = track.P @ H.T @ np.linalg.inv(S)
    track.x = track.x + K @ y
    I_KH = np.eye(4) - K @ H
    track.P = I_KH @ track.P @ I_KH.T + K @ R @ K.T  # Joseph form


def _polar(track, mount_w):
    dx, dy = track.x[0] - mount_w[0], track.x[1] - mount_w[1]
    r2 = max(dx * dx + dy * dy, 1e-6)
    r = math.sqrt(r2)
    h = np.array([r, math.atan2(dy, dx)])
    H = np.array([[dx / r, dy / r, 0, 0], [-dy / r2, dx / r2, 0, 0]])
    return h, H


class Tracker:
    def __init__(self, radar_specs=(), lidar_enabled=True):
        self.specs = {s.name: s for s in radar_specs}
        self.lidar_enabled = lidar_enabled
        self.tracks = []
        self.next_id = 1
        self.stats = {"late_batches": 0, "fragments": 0, "rr_rejected": 0, "born": 0, "deleted": 0}

    def _new(self, t, xy, P, ego, kind, suspect=False):
        tr = Track("X%d" % self.next_id, t, xy, P, ego, kind)
        tr.suspect_only = suspect
        self.next_id += 1
        self.tracks.append(tr)
        self.stats["born"] += 1
        return tr

    # -- radar -----------------------------------------------------------------------------------------------
    def radar_batch(self, t, ego, sensor, measurements, healthy=True):
        """measurements: radar.RadarMeasurement list from one radar at time t."""
        live = self._predict_all(t, ego)
        spec = self.specs[sensor]
        mount_w = ego.to_world(*spec.mount[:2])
        cost = np.full((len(live), len(measurements)), 1e9)
        zs = []
        for j, m in enumerate(measurements):
            z = np.array([m.range, ego.yaw + m.bearing])
            R = np.diag([m.sigma_range ** 2, m.sigma_bearing ** 2])
            zs.append((z, R))
            mw = ego.to_world(m.lon, m.lat)
            for i, tr in enumerate(live):
                if abs(tr.x[0] - mw[0]) > PREGATE_M or abs(tr.x[1] - mw[1]) > PREGATE_M:
                    continue
                h, H = _polar(tr, mount_w)
                y = z - h
                y[1] = math.remainder(y[1], 2 * math.pi)
                d2, logdet = _d2(y, H @ tr.P @ H.T + R)
                if d2 < CHI2_2D:
                    cost[i, j] = d2 + logdet
        pairs = assign(cost)
        used = set()
        for i, j in pairs.items():
            tr, m = live[i], measurements[j]
            z, R = zs[j]
            h, H = _polar(tr, mount_w)
            _update(tr, z, h, H, R, angle_index=1)
            self._range_rate(tr, m, mount_w, ego)
            if "self_suspect" not in m.flags:
                tr.suspect_only = False
            tr.hit(t, "radar")
            tr.clearance["radar"] = (t,) + m.clearance
            used.add(j)
        for j, m in enumerate(measurements):
            if j in used or "range_clamped" in m.flags:
                continue
            xy = np.array(ego.to_world(m.lon, m.lat))
            c, s = math.cos(ego.yaw + m.bearing), math.sin(ego.yaw + m.bearing)
            rot = np.array([[c, -s], [s, c]])
            P = rot @ np.diag([m.sigma_range ** 2, (m.range * m.sigma_bearing) ** 2]) @ rot.T
            tr = self._new(t, xy, P, ego, "radar", suspect="self_suspect" in m.flags)
            self._range_rate(tr, m, mount_w, ego, gate=False)
            tr.clearance["radar"] = (t,) + m.clearance
        self._misses(t, ego, live, {live[i].id for i in pairs}, "radar", sensor if healthy else None)

    def _range_rate(self, tr, m, mount_w, ego, gate=True):
        if m.range_rate is None:
            return
        dx, dy = tr.x[0] - mount_w[0], tr.x[1] - mount_w[1]
        r = max(math.hypot(dx, dy), 0.5)
        u = np.array([dx, dy]) / r
        rel = tr.x[2:] - np.array([ego.vx, ego.vy])
        h = float(u @ rel)
        dpos = (rel - u * h) / r
        H = np.array([[dpos[0], dpos[1], u[0], u[1]]])
        R = np.array([[SIGMA_RANGE_RATE ** 2]])
        S = float((H @ tr.P @ H.T + R)[0, 0])
        if gate and (m.range_rate - h) ** 2 / S > CHI2_1D:
            tr.rr_rejected += 1
            self.stats["rr_rejected"] += 1  # an inconsistent range rate (e.g. a first refresh): position kept
            return
        _update(tr, np.array([m.range_rate]), np.array([h]), H, R)

    # -- lidar -----------------------------------------------------------------------------------------------
    def lidar_batch(self, t, ego, clusters, visibility=None, healthy=True):
        """clusters: LidarCluster list (ego frame). visibility(lon, lat) -> lidar.free_space verdict, or None."""
        live = self._predict_all(t, ego)
        Rw = ego.rot()
        cost = np.full((len(live), len(clusters)), 1e9)
        for j, c in enumerate(clusters):
            cw = ego.to_world((c.x[0] + c.x[1]) / 2, (c.y[0] + c.y[1]) / 2)
            for i, tr in enumerate(live):
                if abs(tr.x[0] - cw[0]) > PREGATE_M or abs(tr.x[1] - cw[1]) > PREGATE_M:
                    continue
                zc, Rc = lidar_centre(c, *tr.extent())
                z = np.array(ego.to_world(*zc))
                d2, logdet = _d2(z - tr.x[:2], tr.P[:2, :2] + Rw @ Rc @ Rw.T)
                if d2 < CHI2_2D:
                    cost[i, j] = d2 + logdet
        pairs = assign(cost)
        used = set(pairs.values())
        hit_ids = set()
        for i, j in pairs.items():
            self._lidar_update(live[i], clusters[j], t, ego, Rw)
            hit_ids.add(live[i].id)
        for j, c in enumerate(clusters):
            if j in used:
                continue
            owner = self._fragment_owner(c, ego)
            if owner is not None:  # part of a known body (e.g. a truck's chassis): evidence, not a new object
                owner.clearance["lidar"] = self._merge_lidar_clearance(owner, t, c.clearance)
                self.stats["fragments"] += 1
                continue
            if c.n < LIDAR_BIRTH_MIN_POINTS:  # a sliver: may update a track, too little evidence to start one
                self.stats["slivers"] = self.stats.get("slivers", 0) + 1
                continue
            L, W, sl, sw = PRIOR_LENGTH_M, PRIOR_WIDTH_M, PRIOR_LENGTH_SIGMA_M, PRIOR_WIDTH_SIGMA_M
            zc, Rc = lidar_centre(c, L, W, sl, sw)
            tr = self._new(t, np.array(ego.to_world(*zc)), Rw @ Rc @ Rw.T, ego, "lidar")
            self._extent(tr, c)
            tr.clearance["lidar"] = (t,) + c.clearance
        self._misses(t, ego, live, hit_ids, "lidar", "lidar" if healthy else None, visibility)

    def _lidar_update(self, tr, c, t, ego, Rw):
        self._extent(tr, c)
        zc, Rc = lidar_centre(c, *tr.extent())
        H = np.zeros((2, 4))
        H[0, 0] = H[1, 1] = 1
        _update(tr, np.array(ego.to_world(*zc)), tr.x[:2].copy(), H, Rw @ Rc @ Rw.T)
        tr.suspect_only = False
        tr.hit(t, "lidar")
        tr.clearance["lidar"] = self._merge_lidar_clearance(tr, t, c.clearance)

    @staticmethod
    def _merge_lidar_clearance(tr, t, interval):
        prev = tr.clearance.get("lidar")
        if prev and abs(prev[0] - t) < 1e-6:  # several clusters of one body this scan: the nearest surface counts
            return (t, min(prev[1], interval[0]), min(prev[2], interval[1]))
        return (t,) + tuple(interval)

    @staticmethod
    def _extent(tr, c):
        rear, front, _half = radar_model.EGO_BOX
        tr.length = max(tr.length, c.x[1] - c.x[0])
        if c.y[1] - c.y[0] <= MAX_VEHICLE_WIDTH_M:  # wider: two objects in one cluster, not this one's width
            tr.width = max(tr.width, c.y[1] - c.y[0])
        beside = c.x[0] < front and c.x[1] > rear and min(abs(c.y[0]), abs(c.y[1])) > _half + 0.3
        if beside and c.n >= 20:  # level with the ego, beside it: the roof lidar sees its whole near side
            tr.seen_alongside = True

    def _fragment_owner(self, c, ego):
        for tr in self.tracks:
            if tr.status != "confirmed" or tr.length < 1.0:
                continue
            lon, lat = ego.to_ego(tr.x[0], tr.x[1])
            length, width, _sl, _sw = tr.extent()
            if (lon - length / 2 - 0.5 <= c.x[0] and c.x[1] <= lon + length / 2 + 0.5
                    and lat - width / 2 - 0.5 <= c.y[0] and c.y[1] <= lat + width / 2 + 0.5):
                return tr
        return None

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def _predict_all(self, t, ego):
        live = []
        for tr in self.tracks:
            if tr.t > t + 1e-9:
                self.stats["late_batches"] += 1  # older than the track's state: not retrodicted
                continue
            tr.predict(t, ego.yaw)
            live.append(tr)
        return live

    def _expected(self, tr, ego, kind, sensor, visibility):
        lon, lat = ego.to_ego(tr.x[0], tr.x[1])
        if kind == "lidar":
            return visibility is not None and visibility(lon, lat) == "seen_through"
        spec = self.specs[sensor]
        x, y, _z, yaw = spec.mount
        r = math.hypot(lon - x, lat - y)
        b = math.remainder(math.atan2(lat - y, lon - x) - yaw, 2 * math.pi)
        if not (spec.min_range + 1.0 < r < spec.max_range - 5.0 and abs(b) < spec.hfov / 2 - 0.05):
            return False
        for other in self.tracks:  # a nearer confirmed track on (about) the same bearing hides it from the radar
            if other is tr or other.status != "confirmed":
                continue
            olon, olat = ego.to_ego(other.x[0], other.x[1])
            ro = math.hypot(olon - x, olat - y)
            if ro < r - 2.0 and abs(math.remainder(math.atan2(olat - y, olon - x) - math.atan2(lat - y, lon - x),
                                                   2 * math.pi)) < math.atan2(1.2, ro):
                return False
        return True

    def _misses(self, t, ego, live, hit_ids, kind, sensor, visibility=None):
        for tr in live:
            if tr.id in hit_ids or tr.last_hit >= t - 1e-9:
                continue
            if sensor is not None and self._expected(tr, ego, kind, sensor, visibility):
                tr.existence += math.log(1 - PD[kind])
                tr.expected_misses += 1

    def prune(self, t):
        keep = []
        for tr in self.tracks:
            unseen = t - tr.last_hit
            dead = (tr.existence < L_DELETE or unseen > MAX_COAST_S or tr.sigma_pos() > MAX_SIGMA_M
                    or (tr.status == "tentative" and unseen > TENTATIVE_TIMEOUT_S))
            if dead:
                self.stats["deleted"] += 1
            else:
                keep.append(tr)
        self.tracks = keep
        self._merge_duplicates()

    def _merge_duplicates(self):
        """Two tracks whose centres are within DUPLICATE_M with the same velocity are one vehicle (two vehicles'
        centres cannot be that close): typically one born from radar and one from lidar in the same instant. The
        older track is kept and takes the other's hits, the newer is deleted (counted as stats["merged"])."""
        gone = set()
        for a in self.tracks:
            for b in self.tracks:
                if a is b or a.id in gone or b.id in gone or (a.first, a.id) > (b.first, b.id):
                    continue
                if (np.linalg.norm(a.x[:2] - b.x[:2]) < DUPLICATE_M
                        and np.linalg.norm(a.x[2:] - b.x[2:]) < DUPLICATE_SPEED_MPS):
                    a.hits = sorted(a.hits + b.hits)
                    a.last_hit = max(a.last_hit, b.last_hit)
                    a.existence = max(a.existence, b.existence)
                    gone.add(b.id)
        if gone:
            self.stats["merged"] = self.stats.get("merged", 0) + len(gone)
            self.tracks = [tr for tr in self.tracks if tr.id not in gone]

    def group(self, t, ego):
        """Confirmed tracks moving together, overlapping across and within GROUP_GAP_M along (a truck seen as cab
        and chassis): one group id, the lower track id; held only after GROUP_HOLD_S together. Never merged."""
        conf = [tr for tr in self.tracks if tr.status == "confirmed"]
        for tr in conf:
            tr.group = tr.id
            if not hasattr(tr, "together"):
                tr.together = {}
        for a in conf:
            for b in conf:
                if a.id >= b.id:
                    continue
                al, aa = ego.to_ego(a.x[0], a.x[1])
                bl, ba = ego.to_ego(b.x[0], b.x[1])
                la, wa = a.extent()[:2]
                lb, wb = b.extent()[:2]
                gap = abs(al - bl) - (la + lb) / 2
                overlap = min(aa + wa / 2, ba + wb / 2) - max(aa - wa / 2, ba - wb / 2)
                close = (np.linalg.norm(a.x[2:] - b.x[2:]) < GROUP_SPEED_MPS and gap < GROUP_GAP_M
                         and overlap > 0.5 * min(wa, wb))
                key = b.id
                since = a.together.get(key)
                a.together[key] = (t if since is None else since) if close else None
                if close and t - a.together[key] >= GROUP_HOLD_S:
                    b.group = min(a.group, b.group, key=lambda g: int(g[1:]))

    def confirmed(self):
        return [tr for tr in self.tracks if tr.status == "confirmed"]
