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
"""
import math

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


def initial_velocity(track):
    """((vx, vy), 2x2 covariance) of an unconfirmed track: range rate, displacement and road prior, fused."""
    heading = perception.CARRIAGEWAY_HEADING
    ex, ey = math.cos(heading), math.sin(heading)
    ia, ic = 1 / ALONG_PRIOR_SIGMA_MPS ** 2, 1 / ACROSS_PRIOR_SIGMA_MPS ** 2  # prior information, mean 0
    info = [[ia * ex * ex + ic * ey * ey, (ia - ic) * ex * ey], [(ia - ic) * ex * ey, ia * ey * ey + ic * ex * ex]]
    vec = [0.0, 0.0]
    if track.radial is not None:
        (ux, uy), z = track.radial
        w = 1 / RANGE_RATE_SIGMA_MPS ** 2
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


class Tracker:
    """The tracks; call update once per radar cycle, also when nothing was detected."""

    def __init__(self, initiation="legacy"):
        if initiation not in INITIATIONS:
            raise ValueError("unknown initiation %r (known: %s)" % (initiation, ", ".join(INITIATIONS)))
        self.initiation = initiation
        self.tracks = []
        self._next_id = 1

    @property
    def confirmed(self):
        """The tracks to report: seen over CONFIRM_S, so with an id and a velocity."""
        return [tr for tr in self.tracks if tr.track_id is not None]

    def update(self, t, detections, pose=None, ego_velocity=None):
        """Expire unseen tracks, then match this cycle's detections (all radars) to tracks, nearest pairs first.
        pose (world x, y, yaw) and ego_velocity (world vx, vy) are used by the "range_rate" initiation only."""
        ranged = self.initiation == "range_rate"
        radial = [radial_measurement(d, pose, ego_velocity) for d in detections] if ranged else None
        # a track coasts unseen at most as long as it was seen: glimpses go soon, established tracks bridge gaps
        self.tracks = [tr for tr in self.tracks
                       if t - tr.last_seen <= min(MAX_COAST_S, max(CONFIRM_S, tr.last_seen - tr.first_seen)) + _EPS]
        pairs = []
        for i, track in enumerate(self.tracks):
            x, y = track.position_at(t)
            if ranged and track.velocity is None:
                pairs += self._initiation_pairs(i, track, t, detections)
                continue
            gate = GATE_M if track.velocity is not None else NEW_TRACK_GATE_M
            for j, d in enumerate(detections):
                gap = math.hypot(d.world_x - x, d.world_y - y)
                if gap <= gate:
                    pairs.append((gap, i, j))
        matched_tracks, matched_detections = set(), set()
        for _, i, j in sorted(pairs):
            if i not in matched_tracks and j not in matched_detections:
                self.tracks[i].update(detections[j])
                if ranged:
                    self.tracks[i].radial = radial[j]
                matched_tracks.add(i)
                matched_detections.add(j)
        for j, d in enumerate(detections):
            if j not in matched_detections:
                self.tracks.append(Track(d))
                if ranged:
                    self.tracks[-1].radial = radial[j]
        for track in self.tracks:
            if track.track_id is None and track.velocity is not None:
                track.track_id = "TRACK_%03d" % self._next_id
                self._next_id += 1

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
