"""Domain data types shared across the app. Pure data — no Qt, no serial, no SQL."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class RoadStatus(str, Enum):
    """Road condition classifications. The Arduino is the authority for these."""
    NORMAL = "NORMAL"
    SPEED_BREAKER = "SPEED_BREAKER"
    POTHOLE = "POTHOLE"


VALID_STATUSES = frozenset(s.value for s in RoadStatus)


class ConnectionStatus(str, Enum):
    """Serial connection lifecycle states shown in the UI."""
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    NO_DATA = "No data received"
    LOST = "Connection lost"
    ERROR = "Error"


@dataclass
class Packet:
    """A parsed telemetry ('T') or event ('E') message."""
    kind: str                       # 'T' or 'E'
    arduino_time_ms: int
    ay: int
    shock: int
    distance_cm: Optional[float]    # None == ultrasonic NA / unavailable
    status: str
    raw: str = ""
    received_at: datetime = field(default_factory=datetime.now)

    @property
    def is_event(self) -> bool:
        return self.kind == "E"


@dataclass
class Hello:
    """The startup handshake: HELLO,ROADSENSE,<version>."""
    version: str
    raw: str = ""
    received_at: datetime = field(default_factory=datetime.now)


@dataclass
class ParseError:
    """A rejected line kept in the bounded in-memory diagnostic log."""
    raw: str
    error: str
    received_at: datetime = field(default_factory=datetime.now)


@dataclass
class Session:
    id: int
    name: str
    notes: str
    started_at: datetime
    ended_at: Optional[datetime] = None


@dataclass
class Event:
    """A persisted 'E' message, optionally joined with its session name."""
    id: int
    session_id: int
    received_at: datetime
    arduino_time_ms: int
    ay: int
    shock: int
    distance_cm: Optional[float]
    status: str
    notes: str = ""
    session_name: str = ""


def distance_str(distance_cm: Optional[float]) -> str:
    """Human-readable distance, honestly reporting unavailable readings as NA."""
    if distance_cm is None:
        return "NA"
    return f"{distance_cm:.1f} cm"
