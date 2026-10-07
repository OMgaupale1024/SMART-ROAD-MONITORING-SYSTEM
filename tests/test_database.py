"""Storage round-trips, survives restart, cascades cleanly, and reports NA as NULL."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from roadsense.database import Database
from roadsense.models import Packet
from roadsense.repositories import EventRepository, SessionRepository, TelemetryRepository


def _packet(kind="T", distance=12.0, status="NORMAL", t=100):
    return Packet(kind=kind, arduino_time_ms=t, ay=1000, shock=50, distance_cm=distance, status=status)


def test_schema_created_in_memory():
    db = Database(":memory:")
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "telemetry", "events"} <= tables
    db.close()


def test_session_crud():
    db = Database(":memory:")
    sessions = SessionRepository(db.conn)
    s = sessions.create("My Test", notes="hi")
    assert s.id > 0 and s.ended_at is None
    assert sessions.get(s.id).name == "My Test"
    sessions.update_notes(s.id, "updated")
    assert sessions.get(s.id).notes == "updated"
    sessions.end(s.id)
    assert sessions.get(s.id).ended_at is not None
    assert len(sessions.list()) == 1
    db.close()


def test_telemetry_batch_and_na():
    db = Database(":memory:")
    session = SessionRepository(db.conn).create("s")
    tel = TelemetryRepository(db.conn)
    tel.add_many(session.id, [_packet(distance=10.0), _packet(distance=None)])
    assert tel.count(session.id) == 2
    rows = list(tel.iter_for_session(session.id))
    assert rows[0]["distance_cm"] == 10.0
    assert rows[1]["distance_cm"] is None  # NA persisted honestly as NULL
    db.close()


def test_event_add_list_get_delete():
    db = Database(":memory:")
    session = SessionRepository(db.conn).create("s")
    events = EventRepository(db.conn)
    ev = events.add(session.id, _packet(kind="E", status="POTHOLE", distance=None))
    assert ev.id > 0 and ev.session_name == ""  # add() doesn't join
    listed = events.list(session_id=session.id)
    assert len(listed) == 1 and listed[0].session_name == "s"  # list() joins the name
    assert listed[0].distance_cm is None
    assert events.get(ev.id).status == "POTHOLE"
    assert events.count(session.id) == 1
    assert events.delete(ev.id) is True
    assert events.count(session.id) == 0
    assert events.delete(ev.id) is False  # already gone
    db.close()


def test_event_filters():
    db = Database(":memory:")
    sessions = SessionRepository(db.conn)
    events = EventRepository(db.conn)
    s1 = sessions.create("s1")
    s2 = sessions.create("s2")
    events.add(s1.id, _packet(kind="E", status="POTHOLE"))
    events.add(s2.id, _packet(kind="E", status="SPEED_BREAKER"))
    assert len(events.list()) == 2
    assert len(events.list(session_id=s1.id)) == 1
    assert len(events.list(status="SPEED_BREAKER")) == 1
    db.close()


def test_foreign_key_cascade():
    db = Database(":memory:")
    sessions = SessionRepository(db.conn)
    tel = TelemetryRepository(db.conn)
    events = EventRepository(db.conn)
    s = sessions.create("s")
    tel.add_many(s.id, [_packet()])
    events.add(s.id, _packet(kind="E", status="POTHOLE"))
    db.conn.execute("DELETE FROM sessions WHERE id = ?", (s.id,))
    db.conn.commit()
    assert tel.count(s.id) == 0
    assert events.count(s.id) == 0
    db.close()


def test_restart_persists(tmp_path):
    path = tmp_path / "roadsense.db"
    db = Database(path)
    s = SessionRepository(db.conn).create("persist me")
    EventRepository(db.conn).add(s.id, _packet(kind="E", status="POTHOLE"))
    db.close()

    db2 = Database(path)
    assert SessionRepository(db2.conn).get(s.id).name == "persist me"
    assert EventRepository(db2.conn).count(s.id) == 1
    db2.close()


def test_connection_is_thread_bound_unless_shared():
    desktop = Database(":memory:")  # default: UI-thread only, sqlite3's safe default
    web = Database(":memory:", check_same_thread=False)  # web server serializes access itself
    with ThreadPoolExecutor(max_workers=1) as other_thread:
        with pytest.raises(sqlite3.ProgrammingError):
            other_thread.submit(desktop.conn.execute, "SELECT 1").result()
        other_thread.submit(web.conn.execute, "SELECT 1").result()
    desktop.close()
    web.close()
