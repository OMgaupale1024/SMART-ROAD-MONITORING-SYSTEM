"""Background state and telemetry manager for the RoadSense web server.

Handles:
- Serial port connection and background thread reading
- Simulation mode generation
- SQLite session recording (via SessionService) and CSV/ZIP export
- WebSocket subscriber broadcasting at 10 Hz

Threads: packets arrive on the source thread (serial or simulator) and on request threads
(simulator trigger); recording and queries arrive on request threads. One lock, ``_lock``,
guards the SQLite connection and all recording state, and nothing else is ever acquired
while it is held. Each recording transition (start, stop, flush) runs inside a single
critical section, so buffered rows are always written to the session that owned them.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import random
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Set

import serial
import serial.tools.list_ports

from roadsense.database import Database
from roadsense.export_service import export_session
from roadsense.models import ConnectionStatus, Hello, Packet, distance_str
from roadsense.protocol import MalformedMessage, parse_line
from roadsense.repositories import EventRepository, SessionRepository, TelemetryRepository
from roadsense.session_service import SessionService

log = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 1.0  # longest that recorded telemetry waits in memory before reaching SQLite


class WebTelemetryManager:
    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database(check_same_thread=False)
        self.sessions = SessionRepository(self.db.conn)
        self.telemetry = TelemetryRepository(self.db.conn)
        self.events = EventRepository(self.db.conn)
        self.recorder = SessionService(self.sessions, self.telemetry, self.events)
        # ponytail: one lock serializes DB reads (e.g. a ZIP export) with packet persistence;
        # give exports their own read connection if they ever stall live telemetry.
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

        # Connection & Source state
        self.connection_status: str = ConnectionStatus.DISCONNECTED.value
        self.is_simulator: bool = False
        self.active_port: Optional[str] = None
        self.active_baud: int = 115200

        # Hardware serial thread controls
        self._serial_port: Optional[serial.Serial] = None
        self._serial_thread: Optional[threading.Thread] = None
        self._serial_stop_event = threading.Event()

        # Simulator state
        self._sim_thread: Optional[threading.Thread] = None
        self._sim_stop_event = threading.Event()
        self._sim_rng = random.Random()
        self._sim_profile = "normal"  # normal, bumpy, city_potholes, highway

        # Real-time metrics
        self.latest_packet: Optional[Dict[str, Any]] = None
        self.total_packets_received = 0
        self.total_events_detected = 0
        self.malformed_line_count = 0
        self.recent_raw_lines: List[str] = []
        self.recent_events: List[Dict[str, Any]] = []

        # WebSocket subscribers
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def get_serial_ports(self) -> List[Dict[str, str]]:
        """List available serial hardware COM ports."""
        ports = serial.tools.list_ports.comports()
        return [
            {
                "port": p.device,
                "description": p.description or p.device,
                "hwid": p.hwid or "",
            }
            for p in sorted(ports, key=lambda x: x.device)
        ]

    def connect_serial(self, port: str, baud: int = 115200) -> Dict[str, Any]:
        """Connect to a physical hardware Arduino over USB serial."""
        self.stop_simulator()
        self.disconnect_serial()

        self.connection_status = ConnectionStatus.CONNECTING.value
        self.active_port = port
        self.active_baud = baud
        self._serial_stop_event.clear()

        try:
            ser = serial.Serial(port=port, baudrate=baud, timeout=1.0)
            ser.reset_input_buffer()
            self._serial_port = ser
            self.connection_status = ConnectionStatus.CONNECTED.value
            self.is_simulator = False

            self._serial_thread = threading.Thread(
                target=self._serial_worker_loop, daemon=True, name="RoadSenseSerialWorker"
            )
            self._serial_thread.start()
            return {"status": "ok", "message": f"Connected to {port} at {baud} baud"}
        except Exception as e:
            self.connection_status = ConnectionStatus.ERROR.value
            return {"status": "error", "message": str(e)}

    def disconnect_serial(self) -> Dict[str, Any]:
        """Close physical serial port."""
        self._serial_stop_event.set()
        if self._serial_port and self._serial_port.is_open:
            try:
                self._serial_port.close()
            except Exception:
                pass
        self._serial_port = None
        if not self.is_simulator:
            self.connection_status = ConnectionStatus.DISCONNECTED.value
            self.active_port = None
        return {"status": "ok", "message": "Serial port disconnected"}

    def start_simulator(self, profile: str = "normal") -> Dict[str, Any]:
        """Start the built-in synthetic telemetry generator."""
        self.disconnect_serial()
        self.stop_simulator()

        self.is_simulator = True
        self.connection_status = ConnectionStatus.CONNECTED.value
        self.active_port = "SIMULATOR (Virtual Arduino)"
        self._sim_profile = profile
        self._sim_stop_event.clear()

        self._sim_thread = threading.Thread(
            target=self._simulator_worker_loop, daemon=True, name="RoadSenseSimWorker"
        )
        self._sim_thread.start()
        return {"status": "ok", "message": "Simulator started"}

    def stop_simulator(self) -> Dict[str, Any]:
        """Stop the simulator."""
        self._sim_stop_event.set()
        if self.is_simulator:
            self.is_simulator = False
            self.connection_status = ConnectionStatus.DISCONNECTED.value
            self.active_port = None
        return {"status": "ok", "message": "Simulator stopped"}

    def trigger_sim_event(self, event_type: str) -> Dict[str, Any]:
        """Manually trigger a synthetic POTHOLE or SPEED_BREAKER event."""
        if not self.is_simulator:
            return {"status": "error", "message": "Simulator is not active"}

        t_ms = int(time.monotonic() * 1000) % 1000000
        ay = 4500 if event_type == "SPEED_BREAKER" else -6800
        shock = self._sim_rng.randint(9000, 14000) if event_type == "SPEED_BREAKER" else self._sim_rng.randint(16000, 24000)
        dist = 8.5 if event_type == "SPEED_BREAKER" else 42.0
        status = event_type

        # Dispatch
        self._process_packet_data("T", t_ms, ay, shock, dist, status)
        self._process_packet_data("E", t_ms, ay, shock, dist, status)
        return {"status": "ok", "triggered": event_type}

    @contextmanager
    def _db(self) -> Iterator[None]:
        """Hold the lock for the connection + recording state; undo a half-done write."""
        with self._lock:
            try:
                yield
            except sqlite3.Error:
                self.db.conn.rollback()
                raise

    def start_recording(self, name: str, notes: str = "") -> Dict[str, Any]:
        """Start recording session into SQLite database (ending any current one first)."""
        name = name.strip() or f"Drive_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        with self._db():
            if self.recorder.is_recording:
                self.recorder.stop()
            session = self.recorder.start(name, notes)
            self._last_flush = time.monotonic()

        return {
            "status": "ok",
            "session_id": session.id,
            "session_name": session.name,
            "started_at": session.started_at.isoformat(),
        }

    def stop_recording(self) -> Dict[str, Any]:
        """Stop current recording session, saving its buffered telemetry first."""
        with self._db():
            if not self.recorder.is_recording:
                return {"status": "noop", "message": "No active recording session"}
            finished = self.recorder.stop()
            telemetry_count, event_count = self.recorder.data_point_count, self.recorder.event_count

        return {
            "status": "ok",
            "session_id": finished.id,
            "session_name": finished.name,
            "ended_at": finished.ended_at.isoformat(),
            "telemetry_count": telemetry_count,
            "event_count": event_count,
        }

    def shutdown(self) -> None:
        """Stop the source, save telemetry still buffered for a recording, close the database."""
        self.stop_simulator()
        self.disconnect_serial()
        try:
            self.stop_recording()
        finally:
            self.db.close()

    def get_status(self) -> Dict[str, Any]:
        """Return current status payload."""
        active = self.recorder.active
        rec_duration = (datetime.now() - active.started_at).total_seconds() if active else 0.0

        return {
            "connection_status": self.connection_status,
            "is_simulator": self.is_simulator,
            "active_port": self.active_port,
            "active_baud": self.active_baud,
            "is_recording": active is not None,
            "active_session_id": active.id if active else None,
            "active_session_name": active.name if active else None,
            "session_duration_sec": round(rec_duration, 1),
            "session_telemetry_count": self.recorder.data_point_count,
            "session_event_count": self.recorder.event_count,
            "total_packets_received": self.total_packets_received,
            "total_events_detected": self.total_events_detected,
            "malformed_lines": self.malformed_line_count,
            "latest_packet": self.latest_packet,
        }

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all historical sessions from SQLite."""
        with self._lock:
            session_list = self.sessions.list()
            res = []
            for s in session_list:
                t_count = self.telemetry.count(s.id)
                e_count = self.events.count(s.id)
                duration_str = "--"
                if s.started_at and s.ended_at:
                    sec = (s.ended_at - s.started_at).total_seconds()
                    mins, s_rem = divmod(int(sec), 60)
                    duration_str = f"{mins}m {s_rem}s"

                res.append({
                    "id": s.id,
                    "name": s.name,
                    "notes": s.notes,
                    "started_at": s.started_at.isoformat() if s.started_at else "",
                    "ended_at": s.ended_at.isoformat() if s.ended_at else "",
                    "duration": duration_str,
                    "telemetry_count": t_count,
                    "event_count": e_count,
                })
            return res

    def get_session_events(self, session_id: int) -> List[Dict[str, Any]]:
        """Get all events recorded for a session."""
        with self._lock:
            evts = self.events.list(session_id=session_id, newest_first=True)
            return [
                {
                    "id": e.id,
                    "session_id": e.session_id,
                    "received_at": e.received_at.isoformat() if e.received_at else "",
                    "arduino_time_ms": e.arduino_time_ms,
                    "ay": e.ay,
                    "shock": e.shock,
                    "distance_cm": e.distance_cm,
                    "distance_str": distance_str(e.distance_cm),
                    "status": e.status,
                    "notes": e.notes,
                }
                for e in evts
            ]

    def export_session_zip(self, session_id: int) -> bytes:
        """Export session CSVs as a single in-memory ZIP file."""
        import tempfile
        with self._lock:
            with tempfile.TemporaryDirectory() as tmpdir:
                paths = export_session(self.sessions, self.telemetry, self.events, session_id, tmpdir)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in paths:
                        zf.write(p, arcname=p.name)
                return buf.getvalue()

    # --- Internal Background Loops ---

    def _serial_worker_loop(self) -> None:
        """Reads lines from physical serial port in real time."""
        while not self._serial_stop_event.is_set():
            if not self._serial_port or not self._serial_port.is_open:
                break
            try:
                line_bytes = self._serial_port.readline()
                if not line_bytes:
                    continue
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self._handle_incoming_raw_line(line)
            except Exception as e:
                self.connection_status = ConnectionStatus.ERROR.value
                time.sleep(0.5)

    def _simulator_worker_loop(self) -> None:
        """Generates realistic ~10Hz synthetic telemetry."""
        t0 = time.monotonic()
        counter = 0
        prev_ay = 0

        while not self._sim_stop_event.is_set():
            counter += 1
            t_ms = int((time.monotonic() - t0) * 1000)
            
            # Base vibration & road noise based on profile
            if self._sim_profile == "highway":
                noise = self._sim_rng.gauss(0, 100)
                ay = int(800 * math.sin(counter / 12.0) + noise)
                pothole_thresh = 0.995
                breaker_thresh = 0.985
            elif self._sim_profile == "bumpy":
                noise = self._sim_rng.gauss(0, 500)
                ay = int(2200 * math.sin(counter / 6.0) + noise)
                pothole_thresh = 0.96
                breaker_thresh = 0.91
            else:  # normal
                noise = self._sim_rng.gauss(0, 220)
                ay = int(1400 * math.sin(counter / 8.0) + noise)
                pothole_thresh = 0.985
                breaker_thresh = 0.94

            shock = abs(ay - prev_ay)
            prev_ay = ay

            roll = self._sim_rng.random()
            status = "NORMAL"
            if roll > pothole_thresh:
                status = "POTHOLE"
                shock = self._sim_rng.randint(15000, 23000)
                ay = -abs(ay) * 2 - 3000
            elif roll > breaker_thresh:
                status = "SPEED_BREAKER"
                shock = self._sim_rng.randint(8000, 14000)
                ay = abs(ay) * 2 + 2500

            # Distance reading: occasionally simulate NA (ultrasonic timeout)
            distance = None if self._sim_rng.random() > 0.92 else round(self._sim_rng.uniform(12.0, 65.0), 1)
            dist_field = "NA" if distance is None else distance

            # Dispatch telemetry
            raw_t = f"T,{t_ms},{ay},{shock},{dist_field},{status}"
            self._handle_incoming_raw_line(raw_t)

            # If an anomaly occurred, also send 'E' event packet
            if status != "NORMAL":
                raw_e = f"E,{t_ms},{ay},{shock},{dist_field},{status}"
                self._handle_incoming_raw_line(raw_e)

            time.sleep(0.1)  # 10 Hz rate

    def _handle_incoming_raw_line(self, line: str) -> None:
        """Parses line, updates metrics, records to DB, and broadcasts."""
        now = datetime.now()
        self.recent_raw_lines.append(line)
        if len(self.recent_raw_lines) > 50:
            self.recent_raw_lines.pop(0)

        try:
            parsed = parse_line(line, received_at=now)
        except MalformedMessage:
            self.malformed_line_count += 1
            return

        if parsed is None:
            return

        if isinstance(parsed, Hello):
            # Handshake received
            self._broadcast({
                "type": "hello",
                "version": parsed.version,
                "raw": line,
                "timestamp": now.isoformat(),
            })
            return

        if isinstance(parsed, Packet):
            self.total_packets_received += 1
            payload = {
                "kind": parsed.kind,
                "arduino_time_ms": parsed.arduino_time_ms,
                "ay": parsed.ay,
                "shock": parsed.shock,
                "distance_cm": parsed.distance_cm,
                "distance_str": distance_str(parsed.distance_cm),
                "status": parsed.status,
                "raw": parsed.raw,
                "timestamp": now.isoformat(),
            }
            self.latest_packet = payload

            # Persist while recording: batches of 20 rows (SessionService), at least every second
            recording = False
            try:
                with self._db():
                    recording = self.recorder.is_recording
                    if recording:
                        self.recorder.handle_packet(parsed)
                        if time.monotonic() - self._last_flush >= FLUSH_INTERVAL_S:
                            self.recorder.flush()
                            self._last_flush = time.monotonic()
            except sqlite3.Error:
                log.exception("Recording write failed; live telemetry continues")

            if parsed.kind == "E":
                self.total_events_detected += 1
                self.recent_events.insert(0, payload)
                if len(self.recent_events) > 30:
                    self.recent_events.pop()

            # Broadcast packet to all connected websockets
            self._broadcast({
                "type": "packet",
                "packet": payload,
                "is_recording": recording,
                "session_counts": {
                    "telemetry": self.recorder.data_point_count,
                    "events": self.recorder.event_count,
                },
            })

    def _process_packet_data(self, kind: str, t_ms: int, ay: int, shock: int, distance: Optional[float], status: str) -> None:
        dist_field = "NA" if distance is None else distance
        raw = f"{kind},{t_ms},{ay},{shock},{dist_field},{status}"
        self._handle_incoming_raw_line(raw)

    # --- WebSocket Broadcasting ---

    def register_subscriber(self, queue: asyncio.Queue) -> None:
        self._subscribers.add(queue)

    def remove_subscriber(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self, data: Dict[str, Any]) -> None:
        if not self._subscribers or not self._loop:
            return
        for q in list(self._subscribers):
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, data)
            except Exception:
                pass
