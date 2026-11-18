"""Persistent RoadSense history (roadsense.history.v1): hazard updates, safety decisions, stream discontinuities and,
opt-in, dataset samples, in one SQLite database that one background thread writes (docs/history.md).

The recorder observes the live-state hub and is never part of a decision. observe() is handed each published state
on the event loop and only puts a reference to it on a bounded queue: no copying, encoding or I/O there. When the
queue is full, or the store can't be written, the state is dropped and counted. The writer thread owns the only
connection. It reduces states to events (a hazard's material changes, the decision's changes, gaps in the stream),
writes them in batched transactions, and enforces retention and a size limit. Every failure ends in a counted loss and
a reported state, never in an exception for the caller.

Handing the writer a reference is safe because a published state is never mutated: the pipeline builds new dicts for
each snapshot, and live_state.finished() copies. Persistence is off unless ROADSENSE_HISTORY_ENABLE=1, and it is not
lossless: what is queued or batched but not committed when the process dies is lost (at most the queue plus one
flush interval of events), and the next lifetime records that the end was not observed.

Identity: a processor lifetime gets a fresh processor_id. A segment is one producer run seen by one processor
without interruption, with one kind of processing ("edge" or "precomputed"); a new segment starts exactly when
LiveStateHub starts a new pipeline, which restarts the local hazard ids. A historical hazard is (segment_id,
local_id), and an event is (segment_id, sequence) (plus local_id for a hazard update): a genuine duplicate has the
same key and is ignored; different observations never share one.
"""
from __future__ import annotations

import json
import logging
import math
import os
import platform
import queue
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from roadsense import __version__
from roadsense.web.hazard_records import FRAME
from roadsense.web.metrics import distribution

try:
    import fcntl  # POSIX: one process per history directory
except ImportError:  # pragma: no cover - Windows
    fcntl = None

log = logging.getLogger(__name__)

SCHEMA = "roadsense.history.v1"
SCHEMA_VERSION = 1  # PRAGMA user_version
APPLICATION_ID = 0x52534831  # PRAGMA application_id, "RSH1": a RoadSense history database, not another SQLite file
DB_NAME = "roadsense-history.sqlite3"
FEATURES = "roadsense.features.v1"
LABEL_SOURCE = "rule:roadsense.autonomy.safety.decide"  # heuristic labels, not observed outcomes

# What makes a hazard update or a decision worth a row (docs/history.md, "Event policy")
CONFIDENCE_STEP = 0.05  # confidence moved this much since the last row
POSITION_STEP_M = 0.5  # the merged position moved this far since the last row
SETTLE_S = 5.0  # simulation s without a new observation: the hazard is passed; its final count gets a row
TARGET_STEP_KMH = 10.0  # the recommended speed moved this much with the same action
STALL_S = 2.0  # processor-clock gap between two states that is a stall (the dashboard calls the stream stale at 2 s)

BUSY_TIMEOUT_S = 1.0  # the writer waits this long for a lock before the batch fails
HEARTBEAT_S = 10.0  # without new events, the stream's progress is committed this often
RETRY_S = (1.0, 60.0)  # reopening after a failure: first delay, longest delay (doubling)
DELETE_CHUNK = 2000  # rows per retention transaction
MAX_CHUNKS = 20  # retention transactions per maintenance pass
LOG_EVERY_S = 60.0
_TRUE = ("1", "true", "yes", "on")
_WAKE = object()


class Config(NamedTuple):
    enabled: bool = False
    directory: str = ""  # "" = default_directory()
    retention_days: float = 30.0
    max_mb: float = 256.0  # SQLite pages in use; see docs/history.md for what the files on disk can add
    min_free_mb: float = 512.0  # below this much free space on the filesystem, writing pauses
    dataset: bool = False  # also keep ML samples (features and heuristic labels) of the stream
    sample_period_s: float = 0.2  # simulation s between samples: 0.2 keeps every frame of a 5 Hz stream
    queue_size: int = 256  # states waiting for the writer; each is a reference to a state already built
    batch_rows: int = 500
    flush_s: float = 2.0  # longest time an event waits in memory before its transaction
    maintenance_s: float = 60.0
    close_timeout_s: float = 3.0


_NUMBERS = (  # field, variable, type, lowest, highest
    ("retention_days", "ROADSENSE_HISTORY_RETENTION_DAYS", float, 0.01, 3650),
    ("max_mb", "ROADSENSE_HISTORY_MAX_MB", float, 1, 1_000_000),
    ("min_free_mb", "ROADSENSE_HISTORY_MIN_FREE_MB", float, 0, 1_000_000),
    ("sample_period_s", "ROADSENSE_HISTORY_SAMPLE_S", float, 0.05, 3600),
    ("queue_size", "ROADSENSE_HISTORY_QUEUE", int, 16, 100_000),
    ("flush_s", "ROADSENSE_HISTORY_FLUSH_S", float, 0.1, 60),
)


def config_from_environment(environ=os.environ, log=log.warning) -> Config:
    """ROADSENSE_HISTORY_ENABLE=1 turns the store on, ROADSENSE_HISTORY_DATASET=1 adds samples; the other
    ROADSENSE_HISTORY_* variables adjust it. An unusable value is reported and the default used."""
    defaults, changes = Config(), {}
    for field, name, kind, low, high in _NUMBERS:
        if name in environ:
            try:
                value = kind(environ[name])
            except ValueError:
                value = math.nan
            if low <= value <= high:
                changes[field] = value
            else:
                log("ignoring %s=%r, using %s" % (name, environ[name], getattr(defaults, field)))
    directory = os.path.expanduser(environ.get("ROADSENSE_HISTORY_DIR", "").strip())
    if directory and os.path.isabs(directory):
        changes["directory"] = directory
    elif directory:  # relative to the service's working directory, which is the source checkout
        log("ignoring ROADSENSE_HISTORY_DIR=%r (must be an absolute path), using %s"
            % (directory, default_directory(environ)))

    def flag(name):
        return environ.get(name, "").strip().lower() in _TRUE
    return Config(enabled=flag("ROADSENSE_HISTORY_ENABLE"), dataset=flag("ROADSENSE_HISTORY_DATASET"), **changes)


def default_directory(environ=os.environ, system: Optional[str] = None) -> Path:
    """The platform's place for application state: ~/.local/state/roadsense/history (XDG_STATE_HOME) on Linux,
    ~/Library/Application Support/RoadSense/history on macOS. Never the source checkout, never /tmp."""
    if (system or platform.system()) == "Darwin":
        return Path.home() / "Library" / "Application Support" / "RoadSense" / "history"
    base = environ.get("XDG_STATE_HOME", "")
    return Path(base if os.path.isabs(base) else Path.home() / ".local" / "state") / "roadsense" / "history"


# --- the database

_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE processors (
    processor_id TEXT PRIMARY KEY, started_utc REAL NOT NULL, last_commit_utc REAL, ended_utc REAL,
    end_reason TEXT, hostname TEXT, version TEXT, pid INTEGER, counters TEXT);
CREATE TABLE segments (
    segment_id TEXT PRIMARY KEY, processor_id TEXT NOT NULL, run_id TEXT NOT NULL, vehicle_id TEXT,
    processing TEXT NOT NULL, first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL,
    first_sim_s REAL NOT NULL, last_sim_s REAL NOT NULL, first_utc REAL NOT NULL, last_utc REAL NOT NULL,
    frames INTEGER NOT NULL, ended INTEGER NOT NULL);
CREATE INDEX segments_run ON segments (run_id, last_utc);
CREATE TABLE hazard_updates (
    id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL, local_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    sim_s REAL NOT NULL, utc REAL NOT NULL, change TEXT NOT NULL, type TEXT, severity TEXT, confidence REAL,
    observations INTEGER, status TEXT, frame TEXT NOT NULL, x_m REAL, y_m REAL, first_seen_s REAL,
    last_seen_s REAL, dimensions TEXT, sensor TEXT, vehicle_id TEXT, simulated INTEGER NOT NULL,
    UNIQUE (segment_id, local_id, sequence));
CREATE INDEX hazard_updates_utc ON hazard_updates (utc);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL, sequence INTEGER NOT NULL, sim_s REAL NOT NULL,
    utc REAL NOT NULL, trigger TEXT NOT NULL, risk TEXT, action TEXT, target_kmh REAL, lane TEXT, reason TEXT,
    threat_type TEXT, threat_id TEXT, threat_distance_m REAL, threat_lon_m REAL, threat_lat_m REAL,
    threat_rel_lon_mps REAL, threat_rel_lat_mps REAL, threat_ttc_s REAL, time_to_hazard_s REAL, min_ttc_s REAL,
    conflicts INTEGER, relevant_hazards INTEGER, ego_speed_mps REAL, ego_lane INTEGER,
    UNIQUE (segment_id, sequence));
CREATE INDEX decisions_utc ON decisions (utc);
CREATE TABLE discontinuities (
    id INTEGER PRIMARY KEY, processor_id TEXT NOT NULL, segment_id TEXT, utc REAL NOT NULL, kind TEXT NOT NULL,
    basis TEXT NOT NULL, run_id TEXT, from_sequence INTEGER, to_sequence INTEGER, missing_sequences INTEGER,
    sim_gap_s REAL, mono_gap_s REAL, count INTEGER, detail TEXT);
CREATE INDEX discontinuities_utc ON discontinuities (utc);
CREATE TABLE samples (
    id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL, sequence INTEGER NOT NULL, sim_s REAL NOT NULL,
    utc REAL NOT NULL, feature_version TEXT NOT NULL, features TEXT NOT NULL, labels TEXT NOT NULL,
    UNIQUE (segment_id, sequence));
CREATE INDEX samples_utc ON samples (utc);
"""

HAZARD_FIELDS = ("type", "severity", "confidence", "observations", "status", "x_m", "y_m", "first_seen_s",
                 "last_seen_s", "dimensions", "sensor", "vehicle_id")
DECISION_FIELDS = ("risk", "action", "target_kmh", "lane", "reason", "threat_type", "threat_id", "threat_distance_m",
                   "threat_lon_m", "threat_lat_m", "threat_rel_lon_mps", "threat_rel_lat_mps", "threat_ttc_s",
                   "time_to_hazard_s", "min_ttc_s", "conflicts", "relevant_hazards", "ego_speed_mps", "ego_lane")
DISCONTINUITY_FIELDS = ("processor_id", "segment_id", "utc", "kind", "basis", "run_id", "from_sequence", "to_sequence",
                        "missing_sequences", "sim_gap_s", "mono_gap_s", "count", "detail")
SEGMENT_FIELDS = ("segment_id", "processor_id", "run_id", "vehicle_id", "processing", "first_sequence", "last_sequence",
                  "first_sim_s", "last_sim_s", "first_utc", "last_utc", "frames", "ended")


def _insert(table, columns):
    return "INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (table, ", ".join(columns), ", ".join("?" * len(columns)))


_INSERTS = {
    "hazard_updates": _insert("hazard_updates", ("segment_id", "local_id", "sequence", "sim_s", "utc", "change")
                              + HAZARD_FIELDS + ("frame", "simulated")),
    "decisions": _insert("decisions", ("segment_id", "sequence", "sim_s", "utc", "trigger") + DECISION_FIELDS),
    "discontinuities": _insert("discontinuities", DISCONTINUITY_FIELDS),
    "samples": _insert("samples", ("segment_id", "sequence", "sim_s", "utc", "feature_version", "features", "labels")),
}
_UPSERT_SEGMENT = (_insert("segments", SEGMENT_FIELDS).replace("INSERT OR IGNORE", "INSERT")
                   + " ON CONFLICT (segment_id) DO UPDATE SET "
                   + ", ".join("%s = excluded.%s" % (f, f) for f in SEGMENT_FIELDS[6:]))
TABLES = tuple(_INSERTS)
# what the size limit deletes first: samples are the bulk and can be recorded again by running the scenario again
SIZE_ORDER = ("samples", "decisions", "hazard_updates", "discontinuities")


class Incompatible(Exception):
    """A file that is not a RoadSense history database of this schema version: it is left exactly as it is."""


class InUse(Exception):
    """Another process has the history directory."""


def open_database(path: Path, connect=sqlite3.connect, create: bool = True) -> sqlite3.Connection:
    """A connection to the history database at path, created if it is new and create is set; raises Incompatible
    for another file, sqlite3.Error or OSError when it can't be opened."""
    conn = connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
    try:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        empty = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        if app_id == 0 and version == 0 and empty and create:
            if conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
                raise sqlite3.OperationalError("cannot use write-ahead logging here")
            conn.execute("BEGIN IMMEDIATE")  # (executescript would commit on its own: one statement at a time)
            for statement in filter(str.strip, _DDL.split(";")):
                conn.execute(statement)
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                             [("schema", SCHEMA), ("created_utc", "%.3f" % time.time()),
                              ("created_by", "RoadSense %s" % __version__)])
            conn.execute("PRAGMA application_id = %d" % APPLICATION_ID)
            conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            conn.execute("COMMIT")
        elif app_id != APPLICATION_ID:
            raise Incompatible("not a RoadSense history database (application_id 0x%x)" % app_id)
        elif version != SCHEMA_VERSION:
            raise Incompatible("history schema version %d; this RoadSense reads version %d only"
                               % (version, SCHEMA_VERSION))
        elif create and conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
            raise sqlite3.OperationalError("cannot use write-ahead logging here")
        if create:
            # WAL + NORMAL: a commit survives a process crash; power loss can lose the last commits, never corrupt
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA journal_size_limit = %d" % (4 << 20))  # WAL truncated to 4 MB after a checkpoint
            conn.execute("PRAGMA wal_autocheckpoint = 1000")  # pages (4 MB); checkpoints run in the writer thread
            conn.execute("PRAGMA cache_size = -2048")  # 2 MB of page cache
        return conn
    except BaseException:
        conn.close()
        raise


def error_kind(error: BaseException) -> Tuple[str, bool]:
    """(kind, permanent) of a failure: permanent ones are never retried, so the file is never touched again.
    By type and SQLite error name first: a message can contain a path, and a path can contain any word."""
    if isinstance(error, Incompatible):
        return "incompatible", True
    if isinstance(error, InUse):
        return "in_use", False
    if isinstance(error, OSError):
        return "cannot_open", False
    if not isinstance(error, sqlite3.Error):
        return "error", False
    name = getattr(error, "sqlite_errorname", "") or ""
    text = str(error).lower()
    for prefixes, phrases, kind in (
            (("SQLITE_NOTADB", "SQLITE_CORRUPT"), ("not a database", "malformed"), "incompatible"),
            (("SQLITE_FULL",), ("disk is full",), "disk_full"),
            (("SQLITE_READONLY",), ("readonly database",), "read_only"),
            (("SQLITE_BUSY", "SQLITE_LOCKED"), ("database is locked", "database table is locked"), "locked"),
            (("SQLITE_CANTOPEN",), ("unable to open",), "cannot_open"),
            (("SQLITE_IOERR",), ("disk i/o error",), "io_error")):
        if name.startswith(prefixes) or (not name and any(p in text for p in phrases)):
            return kind, kind == "incompatible"
    return "error", False


# --- what a state holds: read defensively, since a precomputed state is only checked at its top level


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _str(value, limit=500):
    return value[:limit] if isinstance(value, str) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def hazard_values(h: Dict[str, Any]) -> Dict[str, Any]:
    """The stored fields of one hazard of a state's hazard map."""
    p, dims = _dict(h.get("world_position")), h.get("dimensions")
    observations = _num(h.get("observation_count"))
    return {"type": _str(h.get("type")), "severity": _str(h.get("severity")), "confidence": _num(h.get("confidence")),
            "observations": int(observations) if observations is not None else None, "status": _str(h.get("status")),
            "x_m": _num(p.get("x")), "y_m": _num(p.get("y")), "first_seen_s": _num(h.get("first_seen_s")),
            "last_seen_s": _num(h.get("last_seen_s")),
            "dimensions": json.dumps(dims, sort_keys=True) if isinstance(dims, dict) else None,
            "sensor": _str(h.get("source")), "vehicle_id": _str(h.get("source_vehicle"))}


def _moved(a, b, step):
    if a is None or b is None:
        return a is not b
    return abs(a - b) >= step


def hazard_change(previous: Optional[Dict[str, Any]], current: Dict[str, Any], t: Optional[float]) -> Optional[str]:
    """Why the hazard needs a row now ("created", "severity+confidence", "settled", ...), or None: the material
    changes since the last row. Only those: a pothole is observed every 200 ms while it is in view."""
    if previous is None:
        return "created"
    kinds = [key for key in ("severity", "status") if current[key] != previous[key]]
    if _moved(previous["confidence"], current["confidence"], CONFIDENCE_STEP):
        kinds.append("confidence")
    if None in (previous["x_m"], previous["y_m"], current["x_m"], current["y_m"]):
        if (previous["x_m"], previous["y_m"]) != (current["x_m"], current["y_m"]):
            kinds.append("position")
    elif math.hypot(current["x_m"] - previous["x_m"], current["y_m"] - previous["y_m"]) >= POSITION_STEP_M:
        kinds.append("position")
    if (not kinds and current["observations"] != previous["observations"] and t is not None
            and current["last_seen_s"] is not None and t - current["last_seen_s"] >= SETTLE_S):
        kinds.append("settled")  # (simulation clock on both sides)
    return "+".join(kinds) or None


def _find(records, key, value):
    return next((r for r in records if isinstance(r, dict) and r.get(key) == value), {})


def decision_values(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The deterministic decision of a state with the facts about its primary threat; None without a decision.
    Taken from the state as published, never computed again."""
    us = state.get("unified_safety")
    if not isinstance(us, dict):
        return None
    primary, road = _dict(us.get("primary_threat")), _dict(us.get("road_safety"))
    kind, threat_id = _str(primary.get("type")), _str(primary.get("id"))
    values = dict.fromkeys(DECISION_FIELDS)
    if kind == "vehicle":
        track = _find(_list(state.get("tracks")), "track_id", threat_id)
        p, v = _dict(track.get("relative_position")), _dict(track.get("relative_velocity"))
        values.update(threat_distance_m=_num(track.get("distance_m")), threat_lon_m=_num(p.get("longitudinal_m")),
                      threat_lat_m=_num(p.get("lateral_m")), threat_rel_lon_mps=_num(v.get("longitudinal_mps")),
                      threat_rel_lat_mps=_num(v.get("lateral_mps")),
                      threat_ttc_s=_num(_dict(track.get("collision")).get("ttc_s")))
    elif kind == "road_hazard":
        h = _find(_list(road.get("hazards")), "hazard_id", threat_id)
        values.update(threat_distance_m=_num(h.get("distance_m")), threat_lon_m=_num(h.get("longitudinal_m")),
                      threat_lat_m=_num(h.get("lateral_m")), time_to_hazard_s=_num(h.get("time_to_hazard_s")))
    vehicle, ego = _dict(us.get("vehicle_safety")), _dict(state.get("ego"))
    values.update(risk=_str(us.get("overall_risk")), action=_str(us.get("recommended_action")),
                  target_kmh=_num(us.get("recommended_speed_kmh")), lane=_str(us.get("recommended_lane")),
                  reason=_str(us.get("reason")), threat_type=kind, threat_id=threat_id,
                  min_ttc_s=_num(vehicle.get("minimum_ttc_s")), conflicts=_num(vehicle.get("active_conflicts")),
                  relevant_hazards=_num(road.get("relevant_hazards")), ego_speed_mps=_num(ego.get("speed_mps")),
                  ego_lane=_num(ego.get("lane")))
    return values


def decision_key(values: Dict[str, Any]) -> tuple:
    return tuple(values[k] for k in ("risk", "action", "lane", "threat_type", "threat_id"))


# --- ML samples: roadsense.features.v1, frozen. Every feature is of the state's own time: no later information.

_SLOTS = (  # slot, track lane relation, ahead (True), behind (False) or either (None)
    ("lead", "ego_lane", True), ("follower", "ego_lane", False), ("left", "left_lane", None),
    ("right", "right_lane", None))
_SLOT_FIELDS = (
    ("present", "0/1", "a confirmed track is in the slot (the other slot fields are null when not)"),
    ("lon_m", "m", "longitudinal position relative to the ego rear axle, positive ahead"),
    ("lat_m", "m", "lateral position, positive left"),
    ("v_lon_mps", "m/s", "relative longitudinal velocity, negative closing from ahead"),
    ("v_lat_mps", "m/s", "relative lateral velocity"),
    ("range_rate_mps", "m/s", "radar range rate; null when the radar gave none"),
    ("age_s", "s", "time since the track was first seen"),
    ("unseen_s", "s", "time since the track was last measured (0 when measured in this frame)"))
FEATURE_COLUMNS = (
    ("ego_speed_mps", "m/s", "ego speed"),
    ("ego_acceleration_mps2", "m/s^2", "speed change since the previous processed frame; null on a run's first"),
    ("ego_heading_deg", "deg", "ego heading, counter-clockwise from the world +x axis"),
    ("ego_steering_rad", "rad", "steering angle, positive right"),
    ("ego_lane", "index", "lane index reported by the producer (Webots: 0 the kerb-side driving lane, 2 next to the median)"),
    ("ego_lane_change", "0/1", "the producer reports a lane change in progress"),
    ("left_lane_driving", "0/1", "a driving lane exists to the ego's left (road geometry)"),
    ("right_lane_driving", "0/1", "a driving lane exists to the ego's right (road geometry)"),
    ("track_count", "count", "confirmed vehicle tracks"),
) + tuple(("%s_%s" % (slot, name), unit, "%s vehicle: %s" % (slot, text))
          for slot, _, _ in _SLOTS for name, unit, text in _SLOT_FIELDS) + (
    ("hazard_ahead_present", "0/1", "an ACTIVE mapped hazard ahead in the ego lane"),
    ("hazard_ahead_distance_m", "m", "longitudinal distance to the nearest one"),
    ("hazard_ahead_severity", "0-2", "its severity: 0 LOW, 1 MEDIUM, 2 HIGH"),
    ("hazard_ahead_confidence", "0-1", "its map confidence"),
    ("hazards_ahead_100m", "count", "ACTIVE mapped hazards ahead within 100 m in the ego or an adjacent lane"),
    ("derived_min_ttc_s", "s", "the pipeline's minimum time to collision over all tracks (rule-derived: a model "
                               "given it can copy the rule)"),
    ("derived_conflicts", "count", "tracks the pipeline predicts a conflict with (rule-derived)"),
    ("sequence_gap_before", "count", "sequence numbers between this state and the previous state the history saw "
                                     "that the processor never accepted; null for a segment's first"),
    ("history_dropped_before", "count", "states between them that the history layer itself dropped"),
    ("sim_dt_s", "s", "simulation time since the previous state the history saw; null for a segment's first"),
)
LABEL_COLUMNS = (
    ("overall_risk", "SAFE, CAUTION, HIGH or CRITICAL from the deterministic safety decision"),
    ("recommended_action", "MAINTAIN, MONITOR, SLOW_DOWN, BRAKE, EMERGENCY_BRAKE or CONSIDER_LANE_CHANGE"),
    ("recommended_speed_kmh", "the decision's target speed"),
    ("recommended_lane", "left_lane, right_lane or null"),
    ("primary_threat_type", "vehicle, road_hazard or null"),
)
_SEVERITY = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _flag(value):
    return None if value is None else int(bool(value))


def features(state: Dict[str, Any], gap: Optional[int], dropped: int, sim_dt: Optional[float]) -> Dict[str, Any]:
    """The roadsense.features.v1 row of a state (FEATURE_COLUMNS, in order); null where the state has no value."""
    t = _num(state.get("timestamp"))
    ego = _dict(state.get("ego"))
    lanes = [_dict(lane) for lane in _list(_dict(state.get("road")).get("lanes"))]
    row = {"ego_speed_mps": _num(ego.get("speed_mps")), "ego_acceleration_mps2": _num(ego.get("acceleration_mps2")),
           "ego_heading_deg": _num(ego.get("heading_deg")), "ego_steering_rad": _num(ego.get("steering_rad")),
           "ego_lane": _num(ego.get("lane")),
           "ego_lane_change": _flag(ego["maneuver"] == "lane_change") if isinstance(ego.get("maneuver"), str) else None,
           "left_lane_driving": int(any(l.get("relation") == "left_lane" and l.get("driving") for l in lanes)),
           "right_lane_driving": int(any(l.get("relation") == "right_lane" and l.get("driving") for l in lanes))}
    tracks = [tr for tr in _list(state.get("tracks")) if isinstance(tr, dict)]
    row["track_count"] = len(tracks)
    for slot, lane, ahead in _SLOTS:
        candidates = []
        for tr in tracks:
            lon = _num(_dict(tr.get("relative_position")).get("longitudinal_m"))
            if tr.get("lane_relation") == lane and lon is not None and (ahead is None or (lon >= 0) == ahead):
                candidates.append((abs(lon), tr.get("track_id") or "", tr))
        tr = min(candidates, key=lambda c: c[:2])[2] if candidates else None
        p, v = _dict((tr or {}).get("relative_position")), _dict((tr or {}).get("relative_velocity"))
        last_seen = _num((tr or {}).get("last_seen"))
        values = {"present": int(tr is not None), "lon_m": _num(p.get("longitudinal_m")),
                  "lat_m": _num(p.get("lateral_m")), "v_lon_mps": _num(v.get("longitudinal_mps")),
                  "v_lat_mps": _num(v.get("lateral_mps")), "range_rate_mps": _num((tr or {}).get("relative_speed_mps")),
                  "age_s": _num((tr or {}).get("age_s")),
                  "unseen_s": None if t is None or last_seen is None else round(t - last_seen, 3)}
        row.update(("%s_%s" % (slot, k), val) for k, val in values.items())
    ahead_hazards = []
    for h in _list(state.get("hazards")):
        r = _dict(_dict(h).get("relative"))
        lon = _num(r.get("longitudinal_m"))
        if _dict(h).get("status") == "ACTIVE" and r.get("direction") == "ahead" and lon is not None and lon >= 0:
            ahead_hazards.append((lon, r.get("lane_relation"), h))
    own = min(((lon, h) for lon, lane, h in ahead_hazards if lane == "ego_lane"), key=lambda lh: lh[0], default=None)
    row.update(hazard_ahead_present=int(own is not None), hazard_ahead_distance_m=own and own[0],
               hazard_ahead_severity=own and _SEVERITY.get(own[1].get("severity")),
               hazard_ahead_confidence=own and _num(own[1].get("confidence")),
               hazards_ahead_100m=sum(1 for lon, lane, _ in ahead_hazards
                                      if lon <= 100 and lane in ("ego_lane", "left_lane", "right_lane")))
    vehicle = _dict(_dict(state.get("unified_safety")).get("vehicle_safety"))
    row.update(derived_min_ttc_s=_num(vehicle.get("minimum_ttc_s")),
               derived_conflicts=_num(vehicle.get("active_conflicts")),
               sequence_gap_before=gap, history_dropped_before=dropped if gap is not None else None, sim_dt_s=sim_dt)
    return {name: row[name] for name, _, _ in FEATURE_COLUMNS}  # exactly the frozen columns, in their order


def labels(state: Dict[str, Any]) -> Dict[str, Any]:
    us = _dict(state.get("unified_safety"))
    return {"overall_risk": _str(us.get("overall_risk")), "recommended_action": _str(us.get("recommended_action")),
            "recommended_speed_kmh": _num(us.get("recommended_speed_kmh")),
            "recommended_lane": _str(us.get("recommended_lane")),
            "primary_threat_type": _str(_dict(us.get("primary_threat")).get("type"))}


# --- the recorder


class _Segment:
    """One producer run as one processor saw it, without interruption (see the module docstring)."""

    def __init__(self, segment_id, run_id, vehicle_id, processing, sequence, t, utc, mono):
        self.segment_id, self.run_id, self.vehicle_id, self.processing = segment_id, run_id, vehicle_id, processing
        self.first_sequence = self.last_sequence = sequence
        self.first_sim = self.last_sim = t
        self.first_utc = self.last_utc = utc
        self.last_mono = mono
        self.frames, self.ended = 0, False
        self.hazards: Dict[str, Dict[str, Any]] = {}  # local id -> the values of its last row
        self.decision: Optional[Tuple[tuple, Optional[float], int]] = None  # key, target, sequence of the last row
        self.last_state: Optional[Dict[str, Any]] = None
        self.next_sample_s = -math.inf
        self.resync = False  # rows were lost: the next state writes every hazard and the decision again

    def row(self, processor_id):
        return (self.segment_id, processor_id, self.run_id, self.vehicle_id, self.processing, self.first_sequence,
                self.last_sequence, self.first_sim, self.last_sim, round(self.first_utc, 3), round(self.last_utc, 3),
                self.frames, int(self.ended))


class HistoryRecorder:
    """The store's two sides: observe() on the event loop (a queue put), and one writer thread that owns the
    database. status() reads counters only, never the database, so it answers while the store is failing."""

    def __init__(self, config: Config = Config(), clock=time.monotonic, wall=time.time, connect=sqlite3.connect,
                 disk_usage=shutil.disk_usage) -> None:
        self.config = config
        self._clock, self._wall, self._connect, self._disk_usage = clock, wall, connect, disk_usage
        self.processor_id = uuid.uuid4().hex[:12]
        self.directory = Path(config.directory) if config.directory else default_directory()
        self.path = self.directory / DB_NAME
        self._queue: "queue.Queue" = queue.Queue(maxsize=config.queue_size)
        # event-loop side: written there only
        self._accepting = config.enabled
        self._closed = False
        self._observed = self._dropped_full = self._dropped_inactive = 0
        self._high_water = 0
        # writer side: written there only
        self._state = "starting" if config.enabled else "disabled"
        self._reason: Optional[str] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._lock_file = None
        self._opened = False
        self._permanent = False
        self._paused = False  # low disk space
        self._retry_at, self._retry_delay = 0.0, RETRY_S[0]
        self._last_flush = self._last_maintenance = -math.inf
        self._pending: Dict[str, List[tuple]] = {table: [] for table in TABLES}
        self._dirty: Dict[str, _Segment] = {}
        self._segment: Optional[_Segment] = None
        self._ordinal = 0
        self._seen_full = 0  # the queue-full count as of the last state taken
        self._unrecorded: Counter = Counter()  # states dropped since the last absorbed one, by reason
        self._rows_lost_unrecorded = 0
        self.counters: Counter = Counter()
        self._batch_ms: deque = deque(maxlen=200)  # (list() of it in status() is one C call: no torn read)
        self._storage: Dict[str, Any] = {}
        self._last_error: Optional[Dict[str, Any]] = None
        self._logged: Dict[str, float] = {}
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if config.enabled:
            self._thread = threading.Thread(target=self._run, name="RoadSenseHistory", daemon=True)
            self._thread.start()

    # -- the event loop's side

    def observe(self, state: Dict[str, Any]) -> None:
        """Queue a published state for the writer, or drop and count it. Never blocks, never raises."""
        if not self._accepting:
            if self.config.enabled:
                self._dropped_inactive += 1
            return
        self._observed += 1
        try:
            self._queue.put_nowait((state, self._wall(), self._clock(), self._dropped_full))
        except queue.Full:
            self._dropped_full += 1
            return
        size = self._queue.qsize()
        if size > self._high_water:
            self._high_water = size

    def close(self) -> None:
        """Stop the writer: what is queued is written while the close timeout allows, the rest counted as lost."""
        if self._thread is None or self._closed:
            return
        self._closed, self._accepting = True, False
        self._stopping.set()
        try:
            self._queue.put_nowait(_WAKE)
        except queue.Full:
            pass  # the writer is busy and sees _stopping after its current state
        self._thread.join(self.config.close_timeout_s + 1.0)
        if self._thread.is_alive():
            log.warning("History writer did not stop within %.1f s; history not yet committed is lost",
                        self.config.close_timeout_s)

    def status(self) -> Dict[str, Any]:
        """The store's health for GET /api/system, from memory only."""
        c = self.counters
        writer_alive = self._thread is not None and self._thread.is_alive()
        state = (self._state if writer_alive or self._closed or self._state in ("disabled", "incompatible")
                 else "stopped")
        lost = {"queue_full": self._dropped_full, "inactive": self._dropped_inactive,
                "unavailable": c["dropped_unavailable"], "extract_error": c["extract_errors"],
                "shutdown": c["dropped_shutdown"], "uncommitted_rows": c["rows_lost"]}
        return {
            "schema": "roadsense.history.status.v1",
            "enabled": self.config.enabled,
            "state": state,
            "reason": self._reason if state != "stopped" or self._reason else "writer thread ended",
            "processor_id": self.processor_id if self.config.enabled else None,
            "dataset": self.config.dataset,
            "degraded": self.config.enabled and state != "recording",  # now
            "losses_observed": any(lost.values()),  # at any time in this lifetime: its history has holes
            "queue": {"size": self._queue.qsize(), "capacity": self.config.queue_size, "high_water": self._high_water},
            "states": {"observed": self._observed, "absorbed": c["absorbed"]},
            "lost": lost,
            "rows": {table: c["rows_" + table] for table in TABLES},
            "batches": {"committed": c["batches"], "failed": c["batch_failures"],
                        "latency_ms": distribution(list(self._batch_ms), 2)},
            "retention": {"deleted_by_age": c["deleted_by_age"], "deleted_by_size": c["deleted_by_size"],
                          "retention_days": self.config.retention_days, "limit_mb": self.config.max_mb,
                          "min_free_mb": self.config.min_free_mb},
            "storage": dict(self._storage),
            "last_error": self._last_error,
        }

    # -- the writer's side

    def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    self._step()
                except Exception as error:  # a bug or an injected fault: count it, keep the thread
                    self._fail(error)
            self._shutdown()
        except BaseException as error:
            self._reason = "writer thread ended: %s" % _describe(error)
            log.error("History writer stopped: %s; the safety pipeline is unaffected", _describe(error))
        finally:
            self._accepting = False
            self._state = "stopped" if self._state not in ("incompatible",) else self._state
            self._close_connection()
            self._release_lock()

    def _step(self) -> None:
        now = self._clock()
        if self._conn is None and not self._permanent and now >= self._retry_at:
            self._try_open()
        try:
            item = self._queue.get(timeout=min(1.0, max(0.0, self._wake_at(now) - now)))
        except queue.Empty:
            item = None
        taken = 0
        while item is not None:
            if item is not _WAKE:
                self._take(item)
            taken += 1
            if taken >= self.config.batch_rows or self._stopping.is_set():
                break
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                item = None
        if self._conn is None:
            return
        now = self._clock()
        since = now - self._last_flush
        if (self._has_rows() and (self._rows() >= self.config.batch_rows or since >= self.config.flush_s)
                or self._dirty and since >= HEARTBEAT_S):
            self._flush()
        if now >= self._maintenance_due():
            self._maintain()

    def _maintenance_due(self) -> float:
        return self._last_maintenance + (10.0 if self._paused else self.config.maintenance_s)

    def _wake_at(self, now: float) -> float:
        """When the writer next has work it can do: a flush while something waits to be written, maintenance while
        the database is open, a retry while one is possible; with none of these, in a second (close() wakes it at
        once). A deadline that nothing moves, such as the heartbeat with nothing to write, would leave a zero
        timeout and the thread spinning until the next state arrives (Phase 7A.3: 100% of a Pi core when idle)."""
        due = []
        if self._conn is not None:
            due.append(self._maintenance_due())
            if self._has_rows() or self._dirty:
                due.append(self._last_flush + (self.config.flush_s if self._has_rows() else HEARTBEAT_S))
        elif not self._permanent:
            due.append(self._retry_at)
        return min(due, default=now + 1.0)

    def _take(self, item) -> None:
        state, utc, mono, dropped_full = item
        if dropped_full > self._seen_full:
            self._unrecorded["queue_full"] += dropped_full - self._seen_full
            self._seen_full = dropped_full
        if self._conn is None or self._paused:
            self.counters["dropped_unavailable"] += 1
            self._unrecorded["unavailable" if self._conn is None else "disk_low"] += 1
            return
        try:
            self._absorb(state, utc, mono)
        except sqlite3.Error:  # the store failed, not the state: _step's caller reports it
            self.counters["dropped_unavailable"] += 1
            self._unrecorded["unavailable"] += 1
            raise
        except Exception as error:
            self.counters["extract_errors"] += 1
            self._unrecorded["extract_error"] += 1
            self._log("extract", "History skipped a state it could not read: %s" % _describe(error))
            return
        self.counters["absorbed"] += 1

    def _absorb(self, state, utc, mono) -> None:
        """Turn one state into rows: everything is worked out first, then added to the batch."""
        sim = _dict(state.get("simulation"))
        run_id, sequence, t = _str(sim.get("run_id"), 64), sim.get("sequence"), _num(state.get("timestamp"))
        if not run_id or not isinstance(sequence, int) or t is None:
            raise ValueError("state without run_id, sequence or timestamp")
        processing = "edge" if sim.get("processing") == "edge" else "precomputed"
        rows: Dict[str, List[tuple]] = {table: [] for table in TABLES}

        def discontinuity(kind, basis, segment_id=None, **values):
            rows["discontinuities"].append(self._discontinuity(kind, basis, utc, run_id, segment_id, **values))

        lost = sum(self._unrecorded.values())
        seg = self._segment
        new = seg is None or seg.run_id != run_id or seg.processing != processing
        gap = sim_dt = None
        if new:
            previous_here = seg
            if seg is not None:
                self._close_segment(seg, utc)
            self._ordinal += 1
            seg = _Segment("%s-%04d" % (self.processor_id, self._ordinal), run_id,
                           _str(_dict(state.get("ego")).get("vehicle_id"), 64), processing, sequence, t, utc, mono)
            if previous_here is not None:
                discontinuity("run_change", "observed", seg.segment_id, to_sequence=sequence,
                              detail={"previous_run_id": previous_here.run_id,
                                      "previous_segment_id": previous_here.segment_id,
                                      "previous_processing": previous_here.processing})
            before = self._conn.execute(
                "SELECT segment_id, processor_id, last_sequence, ended FROM segments WHERE run_id = ? "
                "ORDER BY last_utc DESC, segment_id DESC LIMIT 1", (run_id,)).fetchone()
            if before is None:
                discontinuity("run_observed", "observed", seg.segment_id, to_sequence=sequence)
            else:
                # The other segment's last *committed* sequence: what that lifetime processed after its last commit
                # is unknown, so the missing count is an upper bound for this processor's view, not a frame count.
                other_lifetime = before[1] != self.processor_id
                discontinuity("run_resumed" if other_lifetime else "run_reentered",
                              "inferred" if other_lifetime else "observed", seg.segment_id,
                              from_sequence=before[2], to_sequence=sequence,
                              missing_sequences=sequence - before[2] - 1 if sequence > before[2] else None,
                              detail={"previous_segment_id": before[0], "previous_processor_id": before[1],
                                      "previous_ended": bool(before[3]),
                                      "note": "local hazard ids restart in a new segment"})
        else:
            missing = sequence - seg.last_sequence - 1
            gap = max(0, missing - lost)
            if gap > 0:
                discontinuity("sequence_gap", "observed", seg.segment_id, from_sequence=seg.last_sequence,
                              to_sequence=sequence, missing_sequences=gap,
                              detail={"note": "sequence numbers this processor did not accept; not a count of lost "
                                              "sensor observations"})
            if mono - seg.last_mono >= STALL_S:
                discontinuity("stream_stall", "measured", seg.segment_id, from_sequence=seg.last_sequence,
                              to_sequence=sequence, mono_gap_s=round(mono - seg.last_mono, 3),
                              sim_gap_s=round(t - seg.last_sim, 3))
            sim_dt = round(t - seg.last_sim, 3)
        if lost:
            discontinuity("history_loss", "measured", seg.segment_id,
                          from_sequence=None if new else seg.last_sequence, to_sequence=sequence, count=lost,
                          detail=dict(self._unrecorded))
        hazard_rows, hazard_values_now = [], {}
        for h in _list(state.get("hazards")):
            local_id = _str(_dict(h).get("hazard_id"), 64)
            if not local_id:
                continue
            values = hazard_values(h)
            change = hazard_change(seg.hazards.get(local_id), values, t)
            if change == "created" and seg.resync:
                change = "resync"
            if change:
                hazard_rows.append(self._hazard_row(seg, local_id, sequence, t, utc, change, values))
                hazard_values_now[local_id] = values
        rows["hazard_updates"] = hazard_rows
        decision = decision_values(state)
        decision_row = None
        if decision is not None:
            key, target = decision_key(decision), decision["target_kmh"]
            last = seg.decision
            trigger = ("first" if last is None and not seg.resync else "resync" if last is None
                       else "change" if key != last[0] or _moved(last[1], target, TARGET_STEP_KMH) else None)
            if trigger:
                decision_row = (seg.segment_id, sequence, t, round(utc, 3), trigger) + tuple(
                    decision[f] for f in DECISION_FIELDS)
                rows["decisions"].append(decision_row)
        sample = None
        if self.config.dataset and t >= seg.next_sample_s - 1e-3:
            sample = (seg.segment_id, sequence, t, round(utc, 3), FEATURES,
                      json.dumps(features(state, gap, 0 if new else lost, sim_dt), separators=(",", ":")),
                      json.dumps(labels(state), separators=(",", ":")))
            rows["samples"].append(sample)
        if sim.get("active") is False:
            discontinuity("run_ended", "observed", seg.segment_id, to_sequence=sequence)
        # nothing below can fail: the state's rows and the memory of them are taken together
        for table, values in rows.items():
            self._pending[table].extend(values)
        seg.hazards.update(hazard_values_now)
        if decision_row is not None:
            seg.decision = (decision_key(decision), decision["target_kmh"], sequence)
        if sample is not None:
            seg.next_sample_s = t + self.config.sample_period_s
        self._unrecorded.clear()
        seg.resync = False
        seg.last_sequence, seg.last_sim, seg.last_utc, seg.last_mono = sequence, t, utc, mono
        seg.frames += 1
        seg.last_state = state
        self._segment = seg
        self._dirty[seg.segment_id] = seg
        if sim.get("active") is False:
            seg.ended = True
            self._finals(seg, utc)

    def _discontinuity(self, kind, basis, utc, run_id=None, segment_id=None, detail=None, **values) -> tuple:
        row = dict(dict.fromkeys(DISCONTINUITY_FIELDS), processor_id=self.processor_id, segment_id=segment_id,
                   utc=round(utc, 3), kind=kind, basis=basis, run_id=run_id,
                   detail=None if detail is None else json.dumps(detail, sort_keys=True), **values)
        return tuple(row[f] for f in DISCONTINUITY_FIELDS)

    def _hazard_row(self, seg, local_id, sequence, t, utc, change, values):
        return (seg.segment_id, local_id, sequence, t, round(utc, 3), change) + tuple(
            values[f] for f in HAZARD_FIELDS) + (FRAME, 1)  # every hazard today comes from the Webots world

    def _finals(self, seg: _Segment, utc: float) -> None:
        """Rows for what changed since the last ones without being material: final counts, the last decision."""
        state = seg.last_state
        if state is None:
            return
        for h in _list(state.get("hazards")):
            local_id = _str(_dict(h).get("hazard_id"), 64)
            if not local_id:
                continue
            values = hazard_values(h)
            if seg.hazards.get(local_id) != values:
                self._pending["hazard_updates"].append(
                    self._hazard_row(seg, local_id, seg.last_sequence, seg.last_sim, utc, "final", values))
                seg.hazards[local_id] = values
        decision = decision_values(state)
        if decision is not None and (seg.decision is None or seg.decision[2] != seg.last_sequence):
            self._pending["decisions"].append((seg.segment_id, seg.last_sequence, seg.last_sim, round(utc, 3), "final")
                                              + tuple(decision[f] for f in DECISION_FIELDS))
            seg.decision = (decision_key(decision), decision["target_kmh"], seg.last_sequence)

    def _close_segment(self, seg: _Segment, utc: float) -> None:
        self._finals(seg, utc)
        self._dirty[seg.segment_id] = seg
        self._flush()  # so the next segment's lookup of earlier segments of its run sees this one

    def _has_rows(self) -> bool:
        return any(self._pending.values())

    def _rows(self) -> int:
        return sum(len(rows) for rows in self._pending.values())

    def _flush(self) -> None:
        if self._conn is None or not (self._has_rows() or self._dirty):
            return
        pending, dirty = self._pending, list(self._dirty.values())
        started = time.perf_counter()
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, rows in pending.items():
                if rows:
                    conn.executemany(_INSERTS[table], rows)
            conn.executemany(_UPSERT_SEGMENT, [seg.row(self.processor_id) for seg in dirty])
            conn.execute("UPDATE processors SET last_commit_utc = ? WHERE processor_id = ?",
                         (round(self._wall(), 3), self.processor_id))
            conn.execute("COMMIT")
        except BaseException:
            self._rollback()
            self.counters["batch_failures"] += 1
            raise  # (_fail counts the batch's rows as lost)
        for table, rows in pending.items():
            self.counters["rows_" + table] += len(rows)
        self._pending = {table: [] for table in TABLES}
        self._dirty = {}
        self.counters["batches"] += 1
        self._batch_ms.append(1000.0 * (time.perf_counter() - started))
        self._last_flush = self._clock()

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except (sqlite3.Error, AttributeError):
            pass

    def _try_open(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if fcntl is not None and self._lock_file is None:
                lock = open(self.directory / (DB_NAME + ".lock"), "a+")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    lock.close()
                    raise InUse("another RoadSense process is writing history in %s" % self.directory)
                lock.truncate(0)
                lock.write("%d\n" % os.getpid())
                lock.flush()
                self._lock_file = lock
            conn = open_database(self.path, self._connect)
            try:
                self._register(conn)
            except BaseException:
                conn.close()
                raise
        except Exception as error:
            self._fail(error)
            return
        self._conn = conn
        self._state, self._reason = "recording", None
        self._retry_delay = RETRY_S[0]
        self._opened = True
        self._last_flush = self._clock()
        self._last_maintenance = -math.inf  # sizes and free space at once
        log.info("History recording to %s (processor %s)", self.path, self.processor_id)

    def _register(self, conn) -> None:
        """This lifetime's row; the lifetimes whose end nobody saw; and, after a failure, how much was lost."""
        now = round(self._wall(), 3)
        reopened = self._opened
        conn.execute("BEGIN IMMEDIATE")
        try:
            unseen = conn.execute(
                "SELECT processor_id, last_commit_utc FROM processors WHERE ended_utc IS NULL AND end_reason IS NULL "
                "AND processor_id != ? ORDER BY started_utc", (self.processor_id,)).fetchall()
            previous = conn.execute(
                "SELECT processor_id, end_reason, last_commit_utc FROM processors WHERE processor_id != ? "
                "ORDER BY started_utc DESC LIMIT 1", (self.processor_id,)).fetchone()
            conn.executemany("UPDATE processors SET end_reason = 'not_observed' WHERE processor_id = ?",
                             [(p,) for p, _ in unseen])
            conn.execute("INSERT OR IGNORE INTO processors (processor_id, started_utc, last_commit_utc, hostname, "
                         "version, pid) VALUES (?, ?, ?, ?, ?, ?)",
                         (self.processor_id, now, now, socket.gethostname(), __version__, os.getpid()))
            rows = [(p, None, now, "processor_end_not_observed", "inferred", None, None, None, None, None, None, None,
                     json.dumps({"last_commit_utc": last, "note": "no clean shutdown was recorded: events queued or "
                                 "batched after the last commit were lost; how many is unknown"}, sort_keys=True))
                    for p, last in unseen]
            if reopened:
                rows.append((self.processor_id, None, now, "history_resumed", "measured", None, None, None, None,
                             None, None, self._rows_lost_unrecorded,
                             json.dumps({"after": (self._last_error or {}).get("kind"),
                                         "note": "count is the rows of failed batches"}, sort_keys=True)))
            else:
                rows.append((self.processor_id, None, now, "processor_start", "observed", None, None, None, None,
                             None, None, None, json.dumps({
                                 "previous_processor_id": previous and previous[0],
                                 "previous_end": previous and (previous[1] or "not_observed"),
                                 "previous_last_commit_utc": previous and previous[2]}, sort_keys=True)))
            conn.executemany(_INSERTS["discontinuities"], rows)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self._rows_lost_unrecorded = 0

    def _fail(self, error: BaseException) -> None:
        kind, permanent = error_kind(error)
        self._last_error = {"kind": kind, "message": _describe(error)[:300], "utc": round(self._wall(), 3)}
        self._close_connection()
        lost = self._rows()  # rows of a failed or unwritten batch: whether any reached the disk, SQLite rolled back
        self.counters["rows_lost"] += lost
        self._rows_lost_unrecorded += lost
        self._pending = {table: [] for table in TABLES}
        for seg in [self._segment] + list(self._dirty.values()):
            if seg is not None:  # what reached the database is unknown: write everything again on recovery
                seg.hazards.clear()
                seg.decision = None
                seg.resync = True
        if permanent:
            self._permanent, self._accepting = True, False
            self._state, self._reason = "incompatible", "%s: %s (the file is left untouched)" % (
                self.path.name, _describe(error))
            log.error("History disabled: %s; move the file aside to start a new history", self._reason)
            self._release_lock()
            return
        self._state = "unavailable"
        self._reason = "%s: %s; retrying in %.0f s" % (kind, _describe(error), self._retry_delay)
        self._retry_at = self._clock() + self._retry_delay
        self._retry_delay = min(RETRY_S[1], self._retry_delay * 2)
        self._log(kind, "History unavailable (%s); the safety pipeline is unaffected" % self._reason)

    def _maintain(self) -> None:
        """Retention, the size limit, and the sizes and free space for status(): bounded work, in this thread."""
        self._last_maintenance = self._clock()
        conn = self._conn
        cutoff = self._wall() - self.config.retention_days * 86400.0
        chunks = 0
        for table in SIZE_ORDER:
            while chunks < MAX_CHUNKS:
                chunks += 1
                deleted = self._delete(table, "WHERE utc < ?", (cutoff,))
                self.counters["deleted_by_age"] += deleted
                if deleted < DELETE_CHUNK:
                    break
        limit = self.config.max_mb * 1048576.0
        used = self._used_bytes()
        while chunks < MAX_CHUNKS and used > limit:
            counts = [(t, conn.execute("SELECT count(*) FROM %s" % t).fetchone()[0]) for t in SIZE_ORDER]
            table, count = next(((t, n) for t, n in counts if n), (None, 0))
            if table is None:
                break
            chunks += 1
            # down to 90% of the limit, so that the next rows do not start this again at once: the share of the
            # rows that the excess is, as if the table were all of the file (it is re-measured after each chunk)
            share = (used - 0.9 * limit) / used
            self.counters["deleted_by_size"] += self._delete(
                table, "", (), min(DELETE_CHUNK, max(1, math.ceil(count * share))))
            used = self._used_bytes()
        try:  # what is left of lifetimes older than the retention period
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM segments WHERE last_utc < ? AND segment_id NOT IN (SELECT segment_id FROM "
                "hazard_updates UNION SELECT segment_id FROM decisions UNION SELECT segment_id FROM samples "
                "UNION SELECT segment_id FROM discontinuities WHERE segment_id IS NOT NULL)", (cutoff,))
            conn.execute(
                "DELETE FROM processors WHERE processor_id != ? AND coalesce(last_commit_utc, started_utc) < ? "
                "AND processor_id NOT IN (SELECT processor_id FROM segments UNION SELECT processor_id FROM "
                "discontinuities)", (self.processor_id, cutoff))
            conn.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]

        def size(path):
            try:
                return os.stat(path).st_size
            except OSError:
                return 0
        try:
            free = self._disk_usage(str(self.directory)).free
        except OSError:
            free = None
        self._storage = {"db_bytes": size(self.path), "wal_bytes": size(str(self.path) + "-wal"),
                         "used_bytes": (pages - free_pages) * page_size, "free_page_bytes": free_pages * page_size,
                         "filesystem_free_bytes": free, "measured_utc": round(self._wall(), 3)}
        low = free is not None and free < self.config.min_free_mb * 1048576.0
        if low and not self._paused:
            self._paused, self._state = True, "paused"
            self._reason = "filesystem free space below %.0f MB" % self.config.min_free_mb
            self._log("disk_low", "History paused: %s" % self._reason)
        elif not low and self._paused:
            self._paused, self._state, self._reason = False, "recording", None

    def _used_bytes(self) -> int:
        conn = self._conn
        return (conn.execute("PRAGMA page_count").fetchone()[0]
                - conn.execute("PRAGMA freelist_count").fetchone()[0]) * conn.execute("PRAGMA page_size").fetchone()[0]

    def _delete(self, table: str, where: str, args: tuple, limit: int = 0) -> int:
        """Delete up to limit (DELETE_CHUNK) of the oldest rows of table matching where, in one short transaction."""
        limit = limit or DELETE_CHUNK
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute("DELETE FROM %s WHERE id IN (SELECT id FROM %s %s ORDER BY utc LIMIT %d)"
                                   % (table, table, where, limit), args).rowcount
            conn.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return deleted

    def _shutdown(self) -> None:
        deadline = self._clock() + max(0.0, self.config.close_timeout_s - 0.5)
        while self._clock() < deadline:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _WAKE:
                try:
                    self._take(item)
                except Exception as error:
                    self._fail(error)
        while True:  # what there was no time for
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _WAKE:
                self.counters["dropped_shutdown"] += 1
        if self._conn is None:
            return
        try:
            seg = self._segment
            # states lost after the last one written (observe() is closed by now, so these counts are final)
            lost = Counter(self._unrecorded)
            lost["queue_full"] += self._dropped_full - self._seen_full
            lost["shutdown"] += self.counters["dropped_shutdown"]
            lost = +lost
            if lost:
                self._pending["discontinuities"].append(self._discontinuity(
                    "history_loss", "measured", self._wall(), seg and seg.run_id, seg and seg.segment_id,
                    from_sequence=seg and seg.last_sequence, count=sum(lost.values()), detail=dict(lost)))
            if seg is not None:
                self._finals(seg, self._wall())
                self._dirty[seg.segment_id] = seg
            self._flush()
            counters = dict(self.status()["lost"], absorbed=self.counters["absorbed"], observed=self._observed)
            self._conn.execute("UPDATE processors SET ended_utc = ?, end_reason = 'clean', counters = ? "
                               "WHERE processor_id = ?", (round(self._wall(), 3), json.dumps(counters, sort_keys=True),
                                                          self.processor_id))
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._state, self._reason = "stopped", "closed"
        except Exception as error:
            self._fail(error)

    def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _release_lock(self) -> None:
        lock, self._lock_file = self._lock_file, None
        if lock is not None:
            lock.close()  # (closing releases the flock)

    def _log(self, key: str, message: str) -> None:
        """At most one line per kind of problem per minute."""
        now = self._clock()
        if now - self._logged.get(key, -math.inf) >= LOG_EVERY_S:
            self._logged[key] = now
            log.warning(message)


def _describe(error: BaseException) -> str:
    return "%s: %s" % (type(error).__name__, error) if str(error) else type(error).__name__
