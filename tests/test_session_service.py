"""Recording logic: no writes when idle, batched telemetry, events never dropped."""
from datetime import datetime

import pytest

from roadsense.database import Database
from roadsense.models import Packet
from roadsense.repositories import EventRepository, SessionRepository, TelemetryRepository
from roadsense.session_service import SessionService, default_session_name


def _service(batch_size=20):
    db = Database(":memory:")
    return SessionService(
        SessionRepository(db.conn),
        TelemetryRepository(db.conn),
        EventRepository(db.conn),
        batch_size=batch_size,
    )


def _packet(kind="T", status="NORMAL", distance=12.0):
    return Packet(kind=kind, arduino_time_ms=1, ay=100, shock=5, distance_cm=distance, status=status)


def test_not_recording_persists_nothing():
    svc = _service()
    assert svc.is_recording is False
    assert svc.handle_packet(_packet()) is None
    assert svc.telemetry.count(1) == 0  # no session id 1 even exists


def test_start_creates_session():
    svc = _service()
    session = svc.start("Trip")
    assert svc.is_recording is True
    assert session.name == "Trip"
    with pytest.raises(RuntimeError):
        svc.start("again")  # cannot double-record


def test_telemetry_buffers_then_flushes_on_batch():
    svc = _service(batch_size=3)
    s = svc.start()
    svc.handle_packet(_packet())
    svc.handle_packet(_packet())
    assert svc.telemetry.count(s.id) == 0   # still buffered
    assert svc.data_point_count == 2
    svc.handle_packet(_packet())            # hits batch_size -> flush
    assert svc.telemetry.count(s.id) == 3


def test_manual_flush_persists_remainder():
    svc = _service(batch_size=100)
    s = svc.start()
    svc.handle_packet(_packet())
    assert svc.telemetry.count(s.id) == 0
    svc.flush()
    assert svc.telemetry.count(s.id) == 1


def test_event_persisted_immediately():
    svc = _service()
    s = svc.start()
    ev = svc.handle_packet(_packet(kind="E", status="POTHOLE"))
    assert ev is not None and ev.status == "POTHOLE"
    assert svc.event_count == 1
    assert svc.events.count(s.id) == 1  # written without waiting for a batch


def test_stop_flushes_and_ends():
    svc = _service(batch_size=100)
    s = svc.start()
    svc.handle_packet(_packet())
    finished = svc.stop()
    assert svc.is_recording is False
    assert svc.telemetry.count(s.id) == 1     # remainder flushed on stop
    assert finished.ended_at is not None
    assert svc.stop() is None                 # idempotent when idle


def test_default_session_name_format():
    name = default_session_name(datetime(2026, 8, 9, 14, 32))
    assert name == "Road Test 2026-08-09 14-32"
