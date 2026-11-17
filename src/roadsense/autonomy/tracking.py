"""Nearby-vehicle tracking for EGO_ROADSENSE: radar detections to RoadSense tracks TRACK_001, TRACK_002, ...

Tracks live in the world frame (each detection's world_x, world_y, placed with the ego's GPS and inertial unit), so
the ego's own driving and turning don't move them. Each radar cycle, detections join the nearest track whose
predicted position is within a gate; the others start new tracks. A track seen over CONFIRM_S is confirmed: it gets
its TRACK id and a velocity, the displacement over about the last 1.5 s. A glimpse never confirms, so it is never
reported. Plain Python without Webots imports.

Phase 7A.3 P1, opt-in (Tracker(initiation="range_rate"); the default "legacy" is unchanged): before confirmation a
track has no velocity, so the legacy tracker predicts it standing still and a vehicle moving over 25 m/s (5 m per
cycle) never confirms. With "range_rate" an unconfirmed track is predicted with an estimated velocity, fused from
(a) the radar's range rate made a world radial speed with the ego's velocity, which measures only the component
along the line of sight, (b) the track's own displacement once it has two detections, and (c) a weak prior that
vehicles move along the carriageway. Its covariance sets an elliptical gate. A detection inside the legacy gate is
still accepted, so the new mode never refuses a pairing the legacy one makes. Confirmed tracks are unchanged.

Phase 7A.3 P2, opt-in on top of P1 (Tracker(initiation="range_rate", estimator="kalman"); default "window"): each
track carries a constant-velocity Kalman filter instead of the 1.5 s displacement. Frames (all right-handed, angles
counter-clockwise):
  world  Webots ground plane x, y; the track state s = (x, y, vx, vy) and its 4x4 covariance P live here;
  ego    origin at the rear axle, x forward, y left: world = ego_to_world(ego, pose), pose = ego (x, y, yaw);
  radar  mount (x, y) in the ego frame, line of sight u = unit vector from the mount's world position to the
         detection, n = u turned +90 degrees;
  road   along e_a = (cos h, sin h), h = perception.CARRIAGEWAY_HEADING, across e_c = (-sin h, cos h). In
         roadsense_highway.wbt h is 0 or pi, so vx is the speed along the road and vy the speed across it.
Transition over the actual interval dt (timestamps, not an assumed 0.2 s): x += vx dt, y += vy dt, with continuous
white-noise acceleration of density q_a along and q_c across the road: Q = [[A dt^3/3, A dt^2/2], [A dt^2/2, A dt]],
A = q_a e_a e_a' + q_c e_c e_c'. Measurements, each a scalar update in Joseph form: the detection's position along u
and along n (independent there: range and angle noise), and, when the radar gives one, the world radial speed
u . (vx, vy) = range rate + u . ego velocity (h = (0, 0, ux, uy): the line-of-sight component only, never a 2-D
velocity), skipped beyond 3 sigma. A new track starts at its detection with P1's velocity estimate and covariance
(road prior and radial speed). Association is unchanged (P3 is not part of P2): an unconfirmed track uses P1's
gate with the filter's predicted covariance, a confirmed one the fixed 3 m gate around the filter's prediction.
Missing detections only predict; tracks still expire by the legacy rule (P4 is not part of P2).

Phase 7A.3 P4, an opt-in track lifecycle (Tracker(..., lifecycle="p4"); default "legacy"), on either estimator.
Stage 12 found the remaining failures in persistence, not creation: tracks coasting for up to 3 s beyond the
radars' reach or after their vehicle had gone, stale coasting tracks beside a measured track of the same vehicle,
and stopped vehicles lost close ahead. States: tentative (no id yet; legacy rule, unchanged), confirmed (measured
this cycle), coasting (confirmed but unseen) and expired (removed; the reason is kept in Tracker.expired).

Each cycle, an unseen confirmed track's predicted position is assigned a coverage zone:
  beyond    inside a radar's field of view, past its maximum range. It can no longer be observed, so it expires
            at once.
  near      within P4_NEAR_M of a radar, where a vehicle is detected only about a third of the time.
  occluded  another track lies across the line of sight, nearer the radar.
  blind     outside both fields of view, alongside the ego.
  visible   anywhere else in range.

Only a miss while visible is evidence that the vehicle has gone. The track's existence starts at 1 while measured.
Each visible miss multiplies it by 1 - P(detection) (p4_detection_probability, measured on the development
recordings). The track expires once existence falls below P4_EXISTENCE_MIN. A miss in any other zone carries no
evidence, so coasting there is capped instead: a moving track coasts at most P4_MAX_COAST_S, and a stationary track
(slower than P4_STATIONARY_MPS) is held where it was last seen for up to P4_STATIONARY_HOLD_S.

A coasting track within P4_DUPLICATE_M and P4_DUPLICATE_MPS of a measured confirmed track is treated as a
duplicate of it and expires. While unseen, a Kalman track's across-road speed decays with P4_LATERAL_TAU_S (a lane
change does not go on unseen); the window estimator keeps its velocity. A coasting track keeps the legacy 3 m gate
and also accepts a detection inside an along-road gate that grows with its predicted uncertainty: 3 sigma, at most
P4_ALONG_GATE_M along the road and never wider than P4_ACROSS_GATE_M across it.

P4 does not touch risk or safety. It reports reliability (status, zone, existence) but never uses it to lower a
warning, so decisions change only through the tracks themselves: stale tracks removed, stationary tracks held and
coasting lateral speeds damped. Stage 14 evaluates those effects. Without a pose, P4 falls back to the legacy rule.
"""
import math
from types import SimpleNamespace

from . import perception

GATE_M = 3.0  # farther from a track's predicted position is another vehicle (neighbouring lanes are 3.75 m apart)
NEW_TRACK_GATE_M = 5.0  # until a track has a velocity: a car moves up to ~3.5 m between two 0.2 s radar cycles
CONFIRM_S = 0.4  # seen over this long (3 radar cycles): confirmed, with a velocity
MAX_COAST_S = 3.0  # a track unseen for longer is dropped (bridges most occlusions by a neighbouring car)
VELOCITY_WINDOW_S = 1.5  # about one SUMO lane change: shorter is noisier, longer lags
_EPS = 1e-6  # simulation times are float sums of 0.01 s steps

INITIATIONS = ("legacy", "range_rate")
RANGE_RATE_SIGMA_MPS = 0.6  # Webots speedNoise 0.12 m/s, the ego-speed error and the radar's yaw-rate lever arm
POSITION_SIGMA_M = 0.5  # where on a vehicle's body a return lands, plus range and angle noise
ALONG_PRIOR_SIGMA_MPS = 40.0  # world speed along the carriageway: anything a road vehicle does
ACROSS_PRIOR_SIGMA_MPS = 3.0  # world speed across it: vehicles mostly follow the road
ACCEL_SIGMA_MPS2 = 4.0  # unmodelled acceleration between two radar cycles
INIT_GATE_CHI2 = 13.8  # 2-D Mahalanobis gate, 99.9 % of a correct pairing
MAX_INIT_GATE_M = 12.0  # never farther than a 60 m/s vehicle moves in one cycle
MAX_WORLD_SPEED_MPS = 60.0  # a faster radial speed is a Webots first-refresh artefact: used as no range rate
MOUNTS = {source: (x, y) for source, x, y, _yaw in perception.RADARS.values()}

ESTIMATORS = ("window", "kalman")
# P2 measurement noise, measured on the Phase 7A.3 recordings (tools/radar_noise.py; truth used offline only)
KF_RANGE_SIGMA_M = 0.30  # along the line of sight: rangeNoise 0.25 m; 0.25 m measured cycle to cycle
KF_CROSS_SIGMA_M = 0.20  # across it: 0.08-0.17 m measured cycle to cycle (angular noise and aspect changes)
KF_RADIAL_SIGMA_MPS = 0.30  # world radial speed: robust sigma 0.17 m/s, p95 0.55 m/s measured
KF_RADIAL_GATE = 9.0  # chi-square, 1 degree of freedom: a radial speed beyond 3 sigma is an outlier, not used
# P2 process noise: white-noise acceleration, given as the acceleration sigma it allows over a nominal 0.2 s cycle
KF_NOMINAL_DT_S = 0.2
KF_ACCEL_ALONG_MPS2 = 3.0  # braking and accelerating (hard braking ~8 m/s^2 still passes the 3 m gate)
KF_ACCEL_ACROSS_MPS2 = 1.0  # a 3.75 m lane change over 3 s peaks at 2.1 m/s^2, over 5 s at 0.7 (tradeoff: §11)

LIFECYCLES = ("legacy", "p4")
# P4 coverage, from roadsense_highway.wbt; detection probabilities measured on the development recordings (§12)
P4_RANGE_M = {"radar_front": 150.0, "radar_rear": 100.0}  # maxRange
P4_HALF_FOV_RAD = 1.3 - perception.EDGE_MARGIN_RAD  # horizontalFieldOfView 2.6; nearer its edge detections are dropped
P4_NEAR_M = 3.0  # nearer a radar, a vehicle ahead is detected 0.32 of the time (0.96+ beyond): no evidence there
P4_OCCLUDER_M = 1.2  # a track this close to the line of sight and 2 m+ nearer hides one behind (detected 0-0.5)
P4_EXISTENCE_MIN = 0.01  # a real vehicle in view is missed that many cycles running with under 1 % probability
P4_MAX_COAST_S = 4.0  # a moving track coasts at most this long where its misses carry no evidence
P4_STATIONARY_MPS = 0.5  # slower: held where it was seen...
P4_STATIONARY_HOLD_S = 30.0  # ...for at most this long, unless a miss in view says it has gone
P4_LATERAL_TAU_S = 1.5  # an unseen Kalman track's across-road speed decays: a lane change ends
P4_DUPLICATE_M = 2.5  # a coasting track this close to a measured confirmed one...
P4_DUPLICATE_MPS = 3.0  # ...and this close in velocity follows the same vehicle
P4_ALONG_GATE_M = 5.0  # a coasting track's gate grows along the road with its uncertainty, at most this...
P4_ACROSS_GATE_M = 1.5  # ...and never across it beyond this: neighbouring lanes are 3.75 m apart
P4_WINDOW_SPEED_SIGMA_MPS = 0.5  # the window estimator's velocity uncertainty (it has no covariance), for its gate
P4_ONCOMING_MPS = -2.0  # a world speed along the ego's carriageway below this is oncoming traffic


def p4_detection_probability(source, distance, oncoming):
    """P(detection) per cycle of a vehicle with a clear line of sight, rounded down from the development recordings:
    same direction 0.97-0.99 within 100 m (0.85 the farther lanes), 0.69-0.81 beyond; oncoming 0.5 within 100 m,
    0.23 beyond, and 0.05 for the rear radar, which is taken as no evidence."""
    if source == "radar_rear":
        return 0.0 if oncoming else 0.85
    if oncoming:
        return 0.45 if distance <= 100.0 else 0.2
    return 0.85 if distance <= 100.0 else 0.65


def p4_zone(lon, lat, others):
    """(zone, radar source, distance) of an ego-frame position: "beyond", "near", "occluded" or "visible" inside a
    radar's field of view, else "blind" (None, None). others: the ego-frame positions of the other tracks."""
    for source, mx, my, myaw in perception.RADARS.values():
        dx, dy = lon - mx, lat - my
        r = math.hypot(dx, dy)
        if r < _EPS or abs(math.remainder(math.atan2(dy, dx) - myaw, math.tau)) > P4_HALF_FOV_RAD:
            continue
        if r > P4_RANGE_M[source]:
            return "beyond", source, r
        if r < P4_NEAR_M:
            return "near", source, r
        ux, uy = dx / r, dy / r
        for ox, oy in others:
            ex, ey = ox - mx, oy - my
            if 0.0 < ex * ux + ey * uy < r - 2.0 and abs(ey * ux - ex * uy) < P4_OCCLUDER_M:
                return "occluded", source, r
        return "visible", source, r
    return "blind", None, None


class Track:
    """One vehicle followed over time; positions and velocity in the world frame."""

    def __init__(self, detection):
        self.track_id = None  # "TRACK_001"... once confirmed
        self.first_seen = detection.timestamp
        self.history = []  # (t, x, y) of the detections over about the last VELOCITY_WINDOW_S
        self.velocity = None  # (vx, vy) once seen over CONFIRM_S
        self.radial = None  # (unit line of sight, world radial speed) of the last detection; "range_rate" mode only
        self.update(detection)

    def update(self, detection):
        t = detection.timestamp
        self.history.append((t, detection.world_x, detection.world_y))
        while len(self.history) > 2 and self.history[1][0] <= t - VELOCITY_WINDOW_S + _EPS:
            self.history.pop(0)
        (t0, x0, y0), (t1, x1, y1) = self.history[0], self.history[-1]
        if t1 - t0 >= CONFIRM_S - _EPS:
            self.velocity = ((x1 - x0) / (t1 - t0), (y1 - y0) / (t1 - t0))
        self.last_seen = t
        self.source = detection.source
        self.range_rate = detection.range_rate_mps

    def position_at(self, t):
        """The last detected position, moved on at the track's velocity to time t."""
        _, x, y = self.history[-1]
        if self.velocity is None:
            return x, y
        dt = t - self.last_seen
        return x + self.velocity[0] * dt, y + self.velocity[1] * dt


def _inverse(m):
    (a, b), (c, d) = m
    det = a * d - b * c
    return ((d / det, -b / det), (-c / det, a / det))


def initial_velocity(track, radial_sigma=RANGE_RATE_SIGMA_MPS):
    """((vx, vy), 2x2 covariance) of an unconfirmed track: range rate, displacement and road prior, fused."""
    heading = perception.CARRIAGEWAY_HEADING
    ex, ey = math.cos(heading), math.sin(heading)
    ia, ic = 1 / ALONG_PRIOR_SIGMA_MPS ** 2, 1 / ACROSS_PRIOR_SIGMA_MPS ** 2  # prior information, mean 0
    info = [[ia * ex * ex + ic * ey * ey, (ia - ic) * ex * ey], [(ia - ic) * ex * ey, ia * ey * ey + ic * ex * ex]]
    vec = [0.0, 0.0]
    if track.radial is not None:
        (ux, uy), z = track.radial
        w = 1 / radial_sigma ** 2
        info = [[info[0][0] + w * ux * ux, info[0][1] + w * ux * uy], [info[1][0] + w * ux * uy, info[1][1] + w * uy * uy]]
        vec = [vec[0] + w * ux * z, vec[1] + w * uy * z]
    (t0, x0, y0), (t1, x1, y1) = track.history[0], track.history[-1]
    if t1 - t0 > _EPS:
        w = (t1 - t0) ** 2 / (2 * POSITION_SIGMA_M ** 2)
        info = [[info[0][0] + w, info[0][1]], [info[1][0], info[1][1] + w]]
        vec = [vec[0] + w * (x1 - x0) / (t1 - t0), vec[1] + w * (y1 - y0) / (t1 - t0)]
    cov = _inverse(info)
    return (cov[0][0] * vec[0] + cov[0][1] * vec[1], cov[1][0] * vec[0] + cov[1][1] * vec[1]), cov


def radial_measurement(detection, pose, ego_velocity):
    """(unit line of sight, the target's world speed along it) of a detection, or None without a range rate.
    The range rate is the change of the distance, so it measures the relative velocity along the line of sight only;
    adding the ego's velocity along it gives the target's. ponytail: the radar's own velocity is taken as the ego
    origin's; the yaw-rate lever arm (up to ~0.4 m/s in a lane change) is left to RANGE_RATE_SIGMA_MPS."""
    if detection.range_rate_mps is None or pose is None or ego_velocity is None:
        return None
    rx, ry = perception.ego_to_world(*MOUNTS[detection.source], pose)
    dx, dy = detection.world_x - rx, detection.world_y - ry
    norm = math.hypot(dx, dy)
    if norm < 1.0:
        return None
    ux, uy = dx / norm, dy / norm
    speed = detection.range_rate_mps + ego_velocity[0] * ux + ego_velocity[1] * uy
    return ((ux, uy), speed) if abs(speed) <= MAX_WORLD_SPEED_MPS else None


def line_of_sight(detection, pose):
    """Unit vector from the detecting radar's mount to the detection, world frame; None without a pose."""
    if pose is None:
        return None
    rx, ry = perception.ego_to_world(*MOUNTS[detection.source], pose)
    dx, dy = detection.world_x - rx, detection.world_y - ry
    norm = math.hypot(dx, dy)
    return (dx / norm, dy / norm) if norm > _EPS else None


def _scalar_update(s, p, h, z, r, gate=None):
    """Kalman update of state s and covariance p (4, 4x4, in place) by the scalar measurement z = h . s with
    variance r, in Joseph form (stays symmetric positive semi-definite). False if outside the chi-square gate."""
    ph = [p[i][0] * h[0] + p[i][1] * h[1] + p[i][2] * h[2] + p[i][3] * h[3] for i in range(4)]
    var = h[0] * ph[0] + h[1] * ph[1] + h[2] * ph[2] + h[3] * ph[3] + r
    innovation = z - (h[0] * s[0] + h[1] * s[1] + h[2] * s[2] + h[3] * s[3])
    if gate is not None and innovation * innovation > gate * var:
        return False
    k = [v / var for v in ph]
    for i in range(4):
        s[i] += k[i] * innovation
    a = [[p[i][j] - k[i] * ph[j] for j in range(4)] for i in range(4)]  # (I - k h) p, as p is symmetric
    ah = [a[i][0] * h[0] + a[i][1] * h[1] + a[i][2] * h[2] + a[i][3] * h[3] for i in range(4)]
    for i in range(4):
        for j in range(i, 4):
            p[i][j] = p[j][i] = 0.5 * ((a[i][j] - ah[i] * k[j] + r * k[i] * k[j]) + (a[j][i] - ah[j] * k[i] + r * k[j] * k[i]))
    return True


class KalmanTrack:
    """Phase 7A.3 P2: one vehicle followed by a constant-velocity Kalman filter (see the module docstring). Has the
    legacy Track's interface: history (for confirmation timing), velocity (None until confirmed), position_at."""

    def __init__(self, detection, radial, los):
        self.track_id = None
        self.first_seen = detection.timestamp
        self.history = []
        self.velocity = None
        self.radial = radial
        self._start(detection, los)
        self._record(detection)

    def _start(self, detection, los):
        """State at the detection, velocity from the road prior and the radial speed (P1's estimate)."""
        birth = SimpleNamespace(radial=self.radial, history=[(detection.timestamp, detection.world_x, detection.world_y)])
        (vx, vy), cov = initial_velocity(birth, KF_RADIAL_SIGMA_MPS)
        (ux, uy) = los or (1.0, 0.0)
        ra, rc = (KF_RANGE_SIGMA_M ** 2, KF_CROSS_SIGMA_M ** 2) if los else (KF_RANGE_SIGMA_M ** 2,) * 2
        pxx, pxy, pyy = ra * ux * ux + rc * uy * uy, (ra - rc) * ux * uy, ra * uy * uy + rc * ux * ux
        self.state = [detection.world_x, detection.world_y, vx, vy]
        self.cov = [[pxx, pxy, 0.0, 0.0], [pxy, pyy, 0.0, 0.0],
                    [0.0, 0.0, cov[0][0], cov[0][1]], [0.0, 0.0, cov[1][0], cov[1][1]]]
        self.t = detection.timestamp

    def _record(self, detection):
        t = detection.timestamp
        self.history.append((t, detection.world_x, detection.world_y))
        while len(self.history) > 2 and self.history[1][0] <= t - VELOCITY_WINDOW_S + _EPS:
            self.history.pop(0)
        if self.velocity is not None or self.history[-1][0] - self.history[0][0] >= CONFIRM_S - _EPS:
            self.velocity = (self.state[2], self.state[3])
        self.last_seen = t
        self.source = detection.source
        self.range_rate = detection.range_rate_mps

    def predicted(self, t):
        """(state, covariance) moved on to time t; nothing is stored. Never backwards in time."""
        dt = max(0.0, t - self.t)
        x, y, vx, vy = self.state
        p = [row[:] for row in self.cov]
        if dt == 0.0:
            return [x, y, vx, vy], p
        for j in range(4):  # F p
            p[0][j] += dt * p[2][j]
            p[1][j] += dt * p[3][j]
        for i in range(4):  # (F p) F'
            p[i][0] += dt * p[i][2]
            p[i][1] += dt * p[i][3]
        heading = perception.CARRIAGEWAY_HEADING
        ex, ey = math.cos(heading), math.sin(heading)
        qa = KF_ACCEL_ALONG_MPS2 ** 2 * KF_NOMINAL_DT_S
        qc = KF_ACCEL_ACROSS_MPS2 ** 2 * KF_NOMINAL_DT_S
        a = ((qa * ex * ex + qc * ey * ey, (qa - qc) * ex * ey), ((qa - qc) * ex * ey, qa * ey * ey + qc * ex * ex))
        d3, d2 = dt * dt * dt / 3.0, dt * dt / 2.0
        for i in range(2):
            for j in range(2):
                p[i][j] += a[i][j] * d3
                p[i][j + 2] += a[i][j] * d2
                p[i + 2][j] += a[i][j] * d2
                p[i + 2][j + 2] += a[i][j] * dt
        return [x + vx * dt, y + vy * dt, vx, vy], p

    def update(self, detection, radial=None, los=None):
        self.state, self.cov = self.predicted(detection.timestamp)
        self.t = max(self.t, detection.timestamp)
        x, y = detection.world_x, detection.world_y
        if los is not None:  # range and angle noise are independent along and across the line of sight
            ux, uy = los
            _scalar_update(self.state, self.cov, (ux, uy, 0.0, 0.0), ux * x + uy * y, KF_RANGE_SIGMA_M ** 2)
            _scalar_update(self.state, self.cov, (-uy, ux, 0.0, 0.0), -uy * x + ux * y, KF_CROSS_SIGMA_M ** 2)
        else:
            _scalar_update(self.state, self.cov, (1.0, 0.0, 0.0, 0.0), x, KF_RANGE_SIGMA_M ** 2)
            _scalar_update(self.state, self.cov, (0.0, 1.0, 0.0, 0.0), y, KF_RANGE_SIGMA_M ** 2)
        if radial is not None:
            (rx, ry), z = radial
            _scalar_update(self.state, self.cov, (0.0, 0.0, rx, ry), z, KF_RADIAL_SIGMA_MPS ** 2, KF_RADIAL_GATE)
        self.radial = radial
        if not all(math.isfinite(v) for v in self.state) or \
                not all(math.isfinite(v) for row in self.cov for v in row) or min(self.cov[i][i] for i in range(4)) <= 0:
            self._start(detection, los)  # a numerical failure restarts the filter at the detection, keeping the id
        self._record(detection)

    def position_at(self, t):
        """The filter's position, moved on at its velocity to time t."""
        x, y, vx, vy = self.state
        dt = t - self.t
        return x + vx * dt, y + vy * dt


class Tracker:
    """The tracks; call update once per radar cycle, also when nothing was detected."""

    def __init__(self, initiation="legacy", estimator="window", lifecycle="legacy"):
        if initiation not in INITIATIONS:
            raise ValueError("unknown initiation %r (known: %s)" % (initiation, ", ".join(INITIATIONS)))
        if estimator not in ESTIMATORS:
            raise ValueError("unknown estimator %r (known: %s)" % (estimator, ", ".join(ESTIMATORS)))
        if estimator == "kalman" and initiation != "range_rate":
            raise ValueError("the kalman estimator starts from the range_rate initiation's estimate")
        if lifecycle not in LIFECYCLES:
            raise ValueError("unknown lifecycle %r (known: %s)" % (lifecycle, ", ".join(LIFECYCLES)))
        self.initiation, self.estimator, self.lifecycle = initiation, estimator, lifecycle
        self.tracks = []
        self.expired = []  # P4: (track, reason) for each track the last update expired
        self._next_id = 1

    @property
    def confirmed(self):
        """The tracks to report: seen over CONFIRM_S, so with an id and a velocity."""
        return [tr for tr in self.tracks if tr.track_id is not None]

    def update(self, t, detections, pose=None, ego_velocity=None):
        """Expire unseen tracks, then match this cycle's detections (all radars) to tracks, nearest pairs first.
        pose (world x, y, yaw) and ego_velocity (world vx, vy) are used by the "range_rate" initiation only."""
        ranged = self.initiation == "range_rate"
        kalman = self.estimator == "kalman"
        radial = [radial_measurement(d, pose, ego_velocity) for d in detections] if ranged else None
        los = [line_of_sight(d, pose) for d in detections] if kalman else None
        p4 = self.lifecycle == "p4" and pose is not None
        # a track coasts unseen at most as long as it was seen: glimpses go soon, established tracks bridge gaps
        # (P4: tentative tracks only; confirmed ones expire by the evidence of their misses, at the end of the cycle)
        self.tracks = [tr for tr in self.tracks if (p4 and tr.track_id is not None) or
                       t - tr.last_seen <= min(MAX_COAST_S, max(CONFIRM_S, tr.last_seen - tr.first_seen)) + _EPS]
        heading = perception.CARRIAGEWAY_HEADING
        ea = (math.cos(heading), math.sin(heading))
        pairs = []
        for i, track in enumerate(self.tracks):
            x, y = track.position_at(t)
            if kalman and track.velocity is None:
                pairs += self._kalman_initiation_pairs(i, track, t, detections)
                continue
            if ranged and track.velocity is None:
                pairs += self._initiation_pairs(i, track, t, detections)
                continue
            gate = GATE_M if track.velocity is not None else NEW_TRACK_GATE_M
            along_gate = self._p4_along_gate(track, t, ea) if p4 and getattr(track, "status", None) == "coasting" else 0
            for j, d in enumerate(detections):
                dx, dy = d.world_x - x, d.world_y - y
                gap = math.hypot(dx, dy)
                if gap <= gate or (along_gate and abs(dx * ea[0] + dy * ea[1]) <= along_gate and
                                   abs(dy * ea[0] - dx * ea[1]) <= P4_ACROSS_GATE_M):
                    pairs.append((gap, i, j))
        matched_tracks, matched_detections = set(), set()
        for _, i, j in sorted(pairs):
            if i not in matched_tracks and j not in matched_detections:
                if kalman:
                    self.tracks[i].update(detections[j], radial[j], los[j])
                else:
                    self.tracks[i].update(detections[j])
                if ranged:
                    self.tracks[i].radial = radial[j]
                matched_tracks.add(i)
                matched_detections.add(j)
        for j, d in enumerate(detections):
            if j not in matched_detections:
                self.tracks.append(KalmanTrack(d, radial[j], los[j]) if kalman else Track(d))
                if ranged:
                    self.tracks[-1].radial = radial[j]
        for track in self.tracks:
            if track.track_id is None and track.velocity is not None:
                track.track_id = "TRACK_%03d" % self._next_id
                self._next_id += 1
        if p4:
            self._p4_lifecycle(t, pose, ea)

    @staticmethod
    def _p4_along_gate(track, t, ea):
        """Half-length along the road of a coasting track's P4 gate: 3 sigma of its predicted position there."""
        if isinstance(track, KalmanTrack):
            p = track.predicted(t)[1]
            var = p[0][0] * ea[0] ** 2 + 2 * p[0][1] * ea[0] * ea[1] + p[1][1] * ea[1] ** 2 + KF_RANGE_SIGMA_M ** 2
        else:
            dt = t - track.last_seen
            var = POSITION_SIGMA_M ** 2 + (P4_WINDOW_SPEED_SIGMA_MPS * dt) ** 2 + \
                KF_ACCEL_ALONG_MPS2 ** 2 * KF_NOMINAL_DT_S * dt ** 3 / 3
        return min(P4_ALONG_GATE_M, 3 * math.sqrt(var))

    def _p4_lifecycle(self, t, pose, ea):
        """P4 (module docstring): place each unseen confirmed track in a zone, weigh its miss, then expire it (with
        the reason) or coast it on."""
        where = [tr.position_at(t) for tr in self.tracks]
        ego = [perception.world_to_ego(x, y, pose) for x, y in where]
        measured = [abs(tr.last_seen - t) <= _EPS for tr in self.tracks]
        kept, self.expired = [], []
        for i, tr in enumerate(self.tracks):
            if measured[i]:
                tr.existence, tr.zone = 1.0, "measured"
            if measured[i] or tr.track_id is None:
                tr.status = "confirmed" if tr.track_id is not None else "tentative"
                kept.append(tr)
                continue
            tr.status = "coasting"
            tr.zone, source, r = p4_zone(*ego[i], ego[:i] + ego[i + 1:])
            vx, vy = tr.velocity
            if tr.zone == "visible":
                oncoming = vx * ea[0] + vy * ea[1] < P4_ONCOMING_MPS
                tr.existence = getattr(tr, "existence", 1.0) * (1.0 - p4_detection_probability(source, r, oncoming))
            stationary = math.hypot(vx, vy) < P4_STATIONARY_MPS
            reason = None
            if tr.zone == "beyond":
                reason = "beyond sensor range"
            elif getattr(tr, "existence", 1.0) < P4_EXISTENCE_MIN:
                reason = "missed while visible"
            elif any(measured[k] and other.track_id is not None and
                     math.hypot(where[k][0] - where[i][0], where[k][1] - where[i][1]) < P4_DUPLICATE_M and
                     math.hypot(other.velocity[0] - vx, other.velocity[1] - vy) < P4_DUPLICATE_MPS
                     for k, other in enumerate(self.tracks) if k != i):
                reason = "duplicate of a measured track"
            elif t - tr.last_seen > (P4_STATIONARY_HOLD_S if stationary else P4_MAX_COAST_S) + _EPS:
                reason = "coast limit"
            if reason:
                tr.status = "expired"
                self.expired.append((tr, reason))
                continue
            self._p4_coast(tr, t, stationary, ea)
            kept.append(tr)
        self.tracks = kept

    @staticmethod
    def _p4_coast(tr, t, stationary, ea):
        """Move an unseen track on: a stationary one stays where it was seen; a Kalman track's across-road speed
        decays (its covariance is the constant-velocity prediction's, an upper bound)."""
        if not isinstance(tr, KalmanTrack):
            if stationary:
                tr.velocity = (0.0, 0.0)
            return
        state, cov = tr.predicted(t)
        if stationary:
            state[2] = state[3] = 0.0
        else:
            f = math.exp(-max(0.0, t - tr.t) / P4_LATERAL_TAU_S)
            along = state[2] * ea[0] + state[3] * ea[1]
            state[2] = along * ea[0] + f * (state[2] - along * ea[0])
            state[3] = along * ea[1] + f * (state[3] - along * ea[1])
        tr.state, tr.cov, tr.t = state, cov, max(tr.t, t)
        tr.velocity = (state[2], state[3])

    def _initiation_pairs(self, i, track, t, detections):
        """(gap, i, j) for the detections an unconfirmed track may take: inside its elliptical gate around the
        position predicted with its estimated velocity, or inside the legacy gate around its last position."""
        (vx, vy), cov = initial_velocity(track)
        _, x0, y0 = track.history[-1]
        dt = t - track.last_seen
        x, y = x0 + vx * dt, y0 + vy * dt
        noise = 2 * POSITION_SIGMA_M ** 2 + (ACCEL_SIGMA_MPS2 * dt * dt) ** 2
        inverse = _inverse(((noise + cov[0][0] * dt * dt, cov[0][1] * dt * dt),
                            (cov[1][0] * dt * dt, noise + cov[1][1] * dt * dt)))
        pairs = []
        for j, d in enumerate(detections):
            ex, ey = d.world_x - x, d.world_y - y
            gap = math.hypot(ex, ey)
            d2 = ex * (inverse[0][0] * ex + inverse[0][1] * ey) + ey * (inverse[1][0] * ex + inverse[1][1] * ey)
            if (d2 <= INIT_GATE_CHI2 and gap <= MAX_INIT_GATE_M) or \
                    math.hypot(d.world_x - x0, d.world_y - y0) <= NEW_TRACK_GATE_M:
                pairs.append((gap, i, j))
        return pairs

    def _kalman_initiation_pairs(self, i, track, t, detections):
        """P1's gate for an unconfirmed track, with the filter's prediction and covariance instead of P1's estimate."""
        (x, y, _, _), p = track.predicted(t)
        _, x0, y0 = track.history[-1]
        r = KF_RANGE_SIGMA_M ** 2  # the larger of the two measurement variances, in every direction
        inverse = _inverse(((p[0][0] + r, p[0][1]), (p[1][0], p[1][1] + r)))
        pairs = []
        for j, d in enumerate(detections):
            ex, ey = d.world_x - x, d.world_y - y
            gap = math.hypot(ex, ey)
            d2 = ex * (inverse[0][0] * ex + inverse[0][1] * ey) + ey * (inverse[1][0] * ex + inverse[1][1] * ey)
            if (d2 <= INIT_GATE_CHI2 and gap <= MAX_INIT_GATE_M) or \
                    math.hypot(d.world_x - x0, d.world_y - y0) <= NEW_TRACK_GATE_M:
                pairs.append((gap, i, j))
        return pairs
