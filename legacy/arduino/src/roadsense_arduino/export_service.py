"""CSV export for a session: telemetry.csv, events.csv, session_metadata.csv.

Standard-library csv only. Timestamps are already ISO-8601; NA distances stay NA.
The UI is responsible for the save dialog and any overwrite confirmation.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Union

from .repositories import EventRepository, SessionRepository, TelemetryRepository

FILE_NAMES = ("telemetry.csv", "events.csv", "session_metadata.csv")


def target_files(out_dir: Union[str, Path]) -> List[Path]:
    """The three files export_session would write, so the UI can check for overwrites."""
    out = Path(out_dir)
    return [out / name for name in FILE_NAMES]


def _distance(value) -> Union[str, float]:
    return "NA" if value is None else value


def export_session(
    sessions: SessionRepository,
    telemetry: TelemetryRepository,
    events: EventRepository,
    session_id: int,
    out_dir: Union[str, Path],
) -> List[Path]:
    """Write the three CSVs for one session. Returns the paths written."""
    session = sessions.get(session_id)
    if session is None:
        raise ValueError(f"session {session_id} not found")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    telemetry_path, events_path, metadata_path = target_files(out)

    with telemetry_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["received_at", "arduino_time_ms", "ay", "shock", "distance_cm", "status"])
        for row in telemetry.iter_for_session(session_id):
            writer.writerow([
                row["received_at"],
                row["arduino_time_ms"],
                row["ay"],
                row["shock"],
                _distance(row["distance_cm"]),
                row["status"],
            ])

    with events_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["received_at", "arduino_time_ms", "event_type", "ay", "shock", "distance_cm", "notes"]
        )
        for event in events.list(session_id=session_id, newest_first=False):
            writer.writerow([
                event.received_at.isoformat(),
                event.arduino_time_ms,
                event.status,
                event.ay,
                event.shock,
                _distance(event.distance_cm),
                event.notes,
            ])

    with metadata_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["id", "name", "started_at", "ended_at", "notes", "telemetry_count", "event_count"]
        )
        writer.writerow([
            session.id,
            session.name,
            session.started_at.isoformat() if session.started_at else "",
            session.ended_at.isoformat() if session.ended_at else "",
            session.notes,
            telemetry.count(session_id),
            events.count(session_id),
        ])

    return [telemetry_path, events_path, metadata_path]
