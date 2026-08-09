"""Line-based serial protocol parser.

Treats serial input as untrusted: blank lines are ignored, malformed lines raise
MalformedMessage (caught by the reader, counted, logged, and skipped) and never crash.

Wire format (newline-terminated CSV, 115200 baud):
    HELLO,ROADSENSE,<version>                 startup handshake
    T,<arduino_time_ms>,<ay>,<shock>,<distance_cm|NA>,<status>   telemetry
    E,<arduino_time_ms>,<ay>,<shock>,<distance_cm|NA>,<status>   detected event
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

from .models import Hello, Packet, VALID_STATUSES


class MalformedMessage(ValueError):
    """Raised for a line that cannot be parsed into a valid message."""

    def __init__(self, raw: str, reason: str):
        super().__init__(f"{reason}: {raw!r}")
        self.raw = raw
        self.reason = reason


def _to_int(field_name: str, value: str, raw: str) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        raise MalformedMessage(raw, f"{field_name} is not an integer ({value!r})")


def _to_distance(value: str, raw: str) -> Optional[float]:
    if value == "NA":
        return None
    try:
        distance = float(value)
    except (ValueError, TypeError):
        raise MalformedMessage(raw, f"distance is not a number ({value!r})")
    # Reject NaN / inf and physically impossible negatives; keep NA as the only "unknown".
    if distance != distance or distance in (float("inf"), float("-inf")):
        raise MalformedMessage(raw, f"distance is not finite ({value!r})")
    if distance < 0:
        raise MalformedMessage(raw, f"distance is negative ({value!r})")
    return distance


def parse_line(line: Optional[str], received_at: Optional[datetime] = None) -> Union[Packet, Hello, None]:
    """Parse one serial line.

    Returns a Packet ('T'/'E'), a Hello, or None for a blank line that should be ignored.
    Raises MalformedMessage for anything invalid.
    """
    if received_at is None:
        received_at = datetime.now()
    if line is None:
        return None
    raw = line.strip()
    if not raw:
        return None

    parts = raw.split(",")
    tag = parts[0]

    if tag == "HELLO":
        if len(parts) >= 3 and parts[1] == "ROADSENSE":
            return Hello(version=parts[2], raw=raw, received_at=received_at)
        raise MalformedMessage(raw, "malformed HELLO handshake")

    if tag not in ("T", "E"):
        raise MalformedMessage(raw, f"unknown message type {tag!r}")

    if len(parts) != 6:
        raise MalformedMessage(raw, f"expected 6 fields, got {len(parts)}")

    _, t_ms, ay, shock, distance, status = parts
    arduino_time_ms = _to_int("arduino_time_ms", t_ms, raw)
    ay_value = _to_int("ay", ay, raw)
    shock_value = _to_int("shock", shock, raw)
    distance_cm = _to_distance(distance, raw)
    if status not in VALID_STATUSES:
        raise MalformedMessage(raw, f"unknown status {status!r}")

    return Packet(
        kind=tag,
        arduino_time_ms=arduino_time_ms,
        ay=ay_value,
        shock=shock_value,
        distance_cm=distance_cm,
        status=status,
        raw=raw,
        received_at=received_at,
    )
