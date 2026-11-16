"""Live RoadSense state on the web server, passed on to WebSocket clients.

The hub computes snapshots from Webots sensor frames and also accepts precomputed snapshots.
It keeps the latest state in memory and offers it to each client on the server's event loop.
It times the stages of each sensor frame (metrics.EdgeMetrics) and tells its listeners about each
published state; a listener must return at once, and nothing it does can fail the frame.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Callable, Dict, Iterable, Optional

from pydantic import ValidationError

from roadsense.autonomy.live_state import SCHEMA
from roadsense.web import edge
from roadsense.web.metrics import EdgeMetrics

log = logging.getLogger(__name__)

MAX_BYTES = 1_000_000  # a snapshot with a dozen tracks and a few hazards is about 20 kB
_REQUIRED = ("schema", "timestamp", "ego", "road", "tracks", "hazards", "unified_safety", "simulation")


class LiveStateError(ValueError):
    """A snapshot the hub refuses; status is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _bad(message: str) -> LiveStateError:
    return LiveStateError(400, message)


def _reject_constant(token: str):
    raise _bad("%s is not a JSON number: live state must be plain JSON, with null for unavailable values" % token)


def _check_finite(value: Any, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise _bad("%s is not a finite number" % path)  # e.g. 1e999, which Python's JSON reads as infinity
    if isinstance(value, dict):
        for key, item in value.items():
            _check_finite(item, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check_finite(item, "%s[%d]" % (path, i))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse(body: bytes) -> Dict[str, Any]:
    """The roadsense.live.v1 snapshot in body; raises LiveStateError if it isn't one."""
    if len(body) > MAX_BYTES:
        raise LiveStateError(413, "live state over %d bytes" % MAX_BYTES)
    try:
        state = json.loads(body, parse_constant=_reject_constant)
    except LiveStateError:
        raise
    except ValueError as error:  # bad JSON or bad UTF-8
        raise _bad("not JSON: %s" % error)
    if not isinstance(state, dict):
        raise _bad("live state must be a JSON object")
    _check_finite(state, "state")
    missing = [key for key in _REQUIRED if key not in state]
    if missing:
        raise _bad("missing %s" % ", ".join(missing))
    if state["schema"] != SCHEMA:
        raise _bad("schema must be %r, not %r" % (SCHEMA, state["schema"]))
    if not _is_number(state["timestamp"]) or state["timestamp"] < 0:
        raise _bad("timestamp must be the simulation time in seconds")
    for key in ("ego", "road", "simulation"):
        if not isinstance(state[key], dict):
            raise _bad("%s must be an object" % key)
    if state["unified_safety"] is not None and not isinstance(state["unified_safety"], dict):
        raise _bad("unified_safety must be an object or null")
    for key, id_key in (("tracks", "track_id"), ("hazards", "hazard_id")):
        if not isinstance(state[key], list):
            raise _bad("%s must be an array" % key)
        for item in state[key]:
            if not isinstance(item, dict) or not isinstance(item.get(id_key), str) or not item[id_key]:
                raise _bad("every entry of %s needs a %s" % (key, id_key))
    simulation = state["simulation"]
    if not isinstance(simulation.get("run_id"), str) or not simulation["run_id"]:
        raise _bad("simulation.run_id must be a non-empty string")
    sequence = simulation.get("sequence")
    if not _is_number(sequence) or isinstance(sequence, float) or sequence < 1:
        raise _bad("simulation.sequence must be an integer from 1")
    if not isinstance(simulation.get("active"), bool):
        raise _bad("simulation.active must be true or false")
    return state


class LiveStateHub:
    """The latest snapshot, and the WebSocket clients waiting for the next one."""

    def __init__(self, listeners: Iterable[Callable[[Dict[str, Any]], None]] = ()) -> None:
        self.latest_json: Optional[str] = None
        self._run_id: Optional[str] = None
        self._sequence = 0
        self._edge_run: Optional[edge.EdgeRun] = None
        self._clients: Dict[asyncio.Queue, int] = {}  # each client's queue -> snapshots skipped for it
        self._listeners = tuple(listeners)  # told each published state, e.g. the AI explainer
        self._listener_failed = False
        self.metrics = EdgeMetrics()
        self.dropped = 0

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def ingest(self, body: bytes) -> Dict[str, Any]:
        """Check body and make it the latest state. A snapshot that isn't newer than the latest one of its run
        (same run_id, sequence not higher) is refused with 409; a new run_id (a restarted simulation) replaces it."""
        try:
            state = parse(body)
            run_id, sequence = state["simulation"]["run_id"], state["simulation"]["sequence"]
            self._check_sequence(run_id, sequence)
        except LiveStateError as error:
            self.metrics.reject(error.status)
            raise
        self._edge_run = None
        # Only sensor ingestion can attest that this server executed the pipeline.
        state["simulation"].pop("processing", None)
        return self._publish(state, run_id, sequence, "precomputed")

    def ingest_sensors(self, body: bytes, received: Optional[float] = None, read_s: float = 0.0) -> Dict[str, Any]:
        """Compute and publish a dashboard snapshot from one Webots sensor frame. received is the perf_counter()
        time the request handler started at and read_s how long it then took to read the body (for the metrics)."""
        started = time.perf_counter()
        try:
            if len(body) > MAX_BYTES:
                raise LiveStateError(413, "sensor frame over %d bytes" % MAX_BYTES)
            frame = edge.SensorFrame.model_validate_json(body)
            if any(radar.limits[0] >= radar.limits[1] for radar in frame.radars.values()):
                raise _bad("radar minimum range must be below maximum range")
            run_id, sequence = frame.simulation.run_id, frame.simulation.sequence
            self._check_sequence(run_id, sequence)
            run = self._edge_run if run_id == self._run_id else None
            if run is None:
                run = edge.EdgeRun(frame.ego.vehicle_id)
            state = run.update(frame)
        except ValidationError as error:
            self.metrics.reject(400)
            issue = error.errors(include_url=False, include_input=False)[0]
            location = ".".join(map(str, issue["loc"]))
            raise _bad("%s: %s" % (location, issue["msg"])) from error
        except edge.FrameConflict as error:
            self.metrics.reject(409)
            raise LiveStateError(409, str(error)) from error
        except LiveStateError as error:
            self.metrics.reject(error.status)
            raise
        self._edge_run = run
        stamps = None
        if run.processed is not None:  # an end marker repeats the last state: nothing was processed
            stamps = (started if received is None else received, read_s, started) + run.processed
        return self._publish(state, run_id, sequence, "edge", stamps)

    def _check_sequence(self, run_id, sequence) -> None:
        if run_id == self._run_id and sequence <= self._sequence:
            raise LiveStateError(409, "sequence %d of run %s is not newer than %d" % (
                sequence, run_id, self._sequence))

    def _publish(self, state, run_id, sequence, mode, stamps=None) -> Dict[str, Any]:
        self.latest_json = json.dumps(state, allow_nan=False, separators=(",", ":"))
        self._run_id, self._sequence = run_id, sequence
        for queue in self._clients:
            self._offer(queue, self.latest_json)
        published = time.perf_counter()
        for listener in self._listeners:
            try:
                listener(state)
            except Exception:  # a listener is never worth a sensor frame
                if not self._listener_failed:
                    log.exception("A live-state listener failed; the state was published all the same")
                self._listener_failed = True
        timing = None
        if stamps is not None:
            received, read_s, started, process_started, process_ended = stamps
            # read: receiving the body; validate: parsing and checking it; process: Pipeline.process();
            # publish: building the snapshot, encoding it, offering it to the clients; handler: all of the request
            timing = (read_s, process_started - started, process_ended - process_started,
                      published - process_ended, time.perf_counter() - received)
        self.metrics.accept(run_id, sequence, state["timestamp"], state["simulation"]["active"], mode, timing)
        return {"accepted": True, "run_id": run_id, "sequence": sequence}

    def subscribe(self) -> asyncio.Queue:
        """A queue that receives the latest state at once, if there is one, then each new one."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        if self.latest_json is not None:
            queue.put_nowait(self.latest_json)
        self._clients[queue] = 0
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        skipped = self._clients.pop(queue, 0)
        if skipped:
            log.info("Live-state client left; %d snapshots were skipped while it was behind", skipped)

    def _offer(self, queue: asyncio.Queue, text: str) -> None:
        """Give the client the newest snapshot. One it hasn't taken yet is out of date, so it is replaced: a slow
        client gets fewer snapshots, never old ones, and never holds anyone else up."""
        if queue.full():
            queue.get_nowait()
            self._clients[queue] += 1
            self.dropped += 1
        queue.put_nowait(text)
