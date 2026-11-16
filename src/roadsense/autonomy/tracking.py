"""Nearby-vehicle tracking for EGO_ROADSENSE: radar detections to RoadSense tracks TRACK_001, TRACK_002, ...

Tracks live in the world frame (each detection's world_x, world_y, placed with the ego's GPS and inertial unit), so
the ego's own driving and turning don't move them. Each radar cycle, detections join the nearest track whose
predicted position is within a gate; the others start new tracks. A track seen over CONFIRM_S is confirmed: it gets
its TRACK id and a velocity, the displacement over about the last 1.5 s. A glimpse never confirms, so it is never
reported. Plain Python without Webots imports.
"""
import math

GATE_M = 3.0  # farther from a track's predicted position is another vehicle (neighbouring lanes are 3.75 m apart)
NEW_TRACK_GATE_M = 5.0  # until a track has a velocity: a car moves up to ~3.5 m between two 0.2 s radar cycles
CONFIRM_S = 0.4  # seen over this long (3 radar cycles): confirmed, with a velocity
MAX_COAST_S = 3.0  # a track unseen for longer is dropped (bridges most occlusions by a neighbouring car)
VELOCITY_WINDOW_S = 1.5  # about one SUMO lane change: shorter is noisier, longer lags
_EPS = 1e-6  # simulation times are float sums of 0.01 s steps


class Track:
    """One vehicle followed over time; positions and velocity in the world frame."""

    def __init__(self, detection):
        self.track_id = None  # "TRACK_001"... once confirmed
        self.first_seen = detection.timestamp
        self.history = []  # (t, x, y) of the detections over about the last VELOCITY_WINDOW_S
        self.velocity = None  # (vx, vy) once seen over CONFIRM_S
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


class Tracker:
    """The tracks; call update once per radar cycle, also when nothing was detected."""

    def __init__(self):
        self.tracks = []
        self._next_id = 1

    @property
    def confirmed(self):
        """The tracks to report: seen over CONFIRM_S, so with an id and a velocity."""
        return [tr for tr in self.tracks if tr.track_id is not None]

    def update(self, t, detections):
        """Expire unseen tracks, then match this cycle's detections (all radars) to tracks, nearest pairs first."""
        # a track coasts unseen at most as long as it was seen: glimpses go soon, established tracks bridge gaps
        self.tracks = [tr for tr in self.tracks
                       if t - tr.last_seen <= min(MAX_COAST_S, max(CONFIRM_S, tr.last_seen - tr.first_seen)) + _EPS]
        pairs = []
        for i, track in enumerate(self.tracks):
            x, y = track.position_at(t)
            gate = GATE_M if track.velocity is not None else NEW_TRACK_GATE_M
            for j, d in enumerate(detections):
                gap = math.hypot(d.world_x - x, d.world_y - y)
                if gap <= gate:
                    pairs.append((gap, i, j))
        matched_tracks, matched_detections = set(), set()
        for _, i, j in sorted(pairs):
            if i not in matched_tracks and j not in matched_detections:
                self.tracks[i].update(detections[j])
                matched_tracks.add(i)
                matched_detections.add(j)
        self.tracks += [Track(d) for j, d in enumerate(detections) if j not in matched_detections]
        for track in self.tracks:
            if track.track_id is None and track.velocity is not None:
                track.track_id = "TRACK_%03d" % self._next_id
                self._next_id += 1
