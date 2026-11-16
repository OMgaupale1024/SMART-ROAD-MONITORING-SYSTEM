"""Recording business logic: buffer telemetry, persist events immediately, never lose one.

Pure Python (no Qt) so it is unit-testable. The app feeds it every packet and drives
flush() on a timer; it only writes to storage while a session is active.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from .models import Event, Packet, Session
from .repositories import EventRepository, SessionRepository, TelemetryRepository


def default_session_name(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return now.strftime("Road Test %Y-%m-%d %H-%M")


class SessionService:
    def __init__(
        self,
        sessions: SessionRepository,
        telemetry: TelemetryRepository,
        events: EventRepository,
        batch_size: int = 20,
    ):
        self.sessions = sessions
        self.telemetry = telemetry
        self.events = events
        self.batch_size = batch_size
        self.active: Optional[Session] = None
        self._buffer: List[Packet] = []
        self.data_point_count = 0
        self.event_count = 0

    @property
    def is_recording(self) -> bool:
        return self.active is not None

    def start(self, name: Optional[str] = None, notes: str = "") -> Session:
        if self.active is not None:
            raise RuntimeError("a session is already recording")
        self.active = self.sessions.create(name or default_session_name(), notes)
        self._buffer.clear()
        self.data_point_count = 0
        self.event_count = 0
        return self.active

    def handle_packet(self, packet: Packet) -> Optional[Event]:
        """Persist a packet if recording. Returns the stored Event for 'E' messages
        (so the UI can alert), else None. Telemetry is buffered; events are written
        immediately so a crash can never drop a recorded event."""
        if self.active is None:
            return None
        if packet.is_event:
            self.flush()  # keep telemetry ordered before the event that referenced it
            event = self.events.add(self.active.id, packet)
            self.event_count += 1
            return event
        self._buffer.append(packet)
        self.data_point_count += 1
        if len(self._buffer) >= self.batch_size:
            self.flush()
        return None

    def flush(self) -> None:
        if self.active is not None and self._buffer:
            self.telemetry.add_many(self.active.id, self._buffer)
            self._buffer.clear()

    def update_notes(self, notes: str) -> None:
        if self.active is not None:
            self.sessions.update_notes(self.active.id, notes)
            self.active.notes = notes

    def stop(self) -> Optional[Session]:
        if self.active is None:
            return None
        self.flush()
        self.sessions.end(self.active.id)
        finished = self.sessions.get(self.active.id) or self.active
        self.active = None
        return finished
