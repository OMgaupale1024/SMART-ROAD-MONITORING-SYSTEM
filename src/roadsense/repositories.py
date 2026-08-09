"""Data access over the SQLite connection. Timestamps stored as ISO-8601 text."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Iterator, List, Optional

from .models import Event, Packet, Session


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


class SessionRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, name: str, notes: str = "", started_at: Optional[datetime] = None) -> Session:
        started_at = started_at or datetime.now()
        cur = self.conn.execute(
            "INSERT INTO sessions(name, notes, started_at) VALUES (?, ?, ?)",
            (name, notes, started_at.isoformat()),
        )
        self.conn.commit()
        return Session(id=cur.lastrowid, name=name, notes=notes, started_at=started_at)

    def end(self, session_id: int, ended_at: Optional[datetime] = None) -> None:
        ended_at = ended_at or datetime.now()
        self.conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?", (ended_at.isoformat(), session_id)
        )
        self.conn.commit()

    def update_notes(self, session_id: int, notes: str) -> None:
        self.conn.execute("UPDATE sessions SET notes = ? WHERE id = ?", (notes, session_id))
        self.conn.commit()

    def get(self, session_id: int) -> Optional[Session]:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row(row) if row else None

    def list(self) -> List[Session]:
        rows = self.conn.execute("SELECT * FROM sessions ORDER BY id DESC").fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            name=row["name"],
            notes=row["notes"],
            started_at=_dt(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
        )


class TelemetryRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add_many(self, session_id: int, packets: List[Packet]) -> None:
        """Insert a batch of telemetry rows in one transaction."""
        if not packets:
            return
        rows = [
            (
                session_id,
                p.received_at.isoformat(),
                p.arduino_time_ms,
                p.ay,
                p.shock,
                p.distance_cm,
                p.status,
            )
            for p in packets
        ]
        self.conn.executemany(
            "INSERT INTO telemetry(session_id, received_at, arduino_time_ms, ay, shock, distance_cm, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def count(self, session_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

    def iter_for_session(self, session_id: int) -> Iterator[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT received_at, arduino_time_ms, ay, shock, distance_cm, status"
            " FROM telemetry WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        yield from cur


class EventRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, session_id: int, packet: Packet, notes: str = "") -> Event:
        cur = self.conn.execute(
            "INSERT INTO events(session_id, received_at, arduino_time_ms, ay, shock, distance_cm, status, notes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                packet.received_at.isoformat(),
                packet.arduino_time_ms,
                packet.ay,
                packet.shock,
                packet.distance_cm,
                packet.status,
                notes,
            ),
        )
        self.conn.commit()
        return Event(
            id=cur.lastrowid,
            session_id=session_id,
            received_at=packet.received_at,
            arduino_time_ms=packet.arduino_time_ms,
            ay=packet.ay,
            shock=packet.shock,
            distance_cm=packet.distance_cm,
            status=packet.status,
            notes=notes,
        )

    def list(
        self,
        session_id: Optional[int] = None,
        status: Optional[str] = None,
        newest_first: bool = True,
    ) -> List[Event]:
        order = "DESC" if newest_first else "ASC"
        rows = self.conn.execute(
            "SELECT e.*, s.name AS session_name FROM events e JOIN sessions s ON e.session_id = s.id"
            " WHERE (:sid IS NULL OR e.session_id = :sid)"
            " AND (:status IS NULL OR e.status = :status)"
            f" ORDER BY e.received_at {order}, e.id {order}",
            {"sid": session_id, "status": status},
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, event_id: int) -> Optional[Event]:
        row = self.conn.execute(
            "SELECT e.*, s.name AS session_name FROM events e JOIN sessions s ON e.session_id = s.id"
            " WHERE e.id = ?",
            (event_id,),
        ).fetchone()
        return self._row(row) if row else None

    def update_notes(self, event_id: int, notes: str) -> None:
        self.conn.execute("UPDATE events SET notes = ? WHERE id = ?", (notes, event_id))
        self.conn.commit()

    def delete(self, event_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def count(self, session_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()[0]

    @staticmethod
    def _row(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            session_id=row["session_id"],
            received_at=_dt(row["received_at"]),
            arduino_time_ms=row["arduino_time_ms"],
            ay=row["ay"],
            shock=row["shock"],
            distance_cm=row["distance_cm"],
            status=row["status"],
            notes=row["notes"],
            session_name=row["session_name"] if "session_name" in row.keys() else "",
        )
