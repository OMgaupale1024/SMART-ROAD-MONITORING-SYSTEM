"""CSV export: three files, readable headers, ISO timestamps, NA preserved."""
import csv
from datetime import datetime

from roadsense.database import Database
from roadsense.export_service import FILE_NAMES, export_session, target_files
from roadsense.models import Packet
from roadsense.repositories import EventRepository, SessionRepository, TelemetryRepository


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_export_writes_three_files(tmp_path):
    db = Database(":memory:")
    sessions = SessionRepository(db.conn)
    tel = TelemetryRepository(db.conn)
    events = EventRepository(db.conn)

    s = sessions.create("Road Test", notes="bumpy")
    tel.add_many(s.id, [
        Packet("T", 100, 1000, 50, 12.0, "NORMAL"),
        Packet("T", 200, 1100, 300, None, "NORMAL"),   # NA distance
    ])
    events.add(s.id, Packet("E", 250, 1200, 5200, 34.8, "POTHOLE"), notes="deep")
    sessions.end(s.id)

    written = export_session(sessions, tel, events, s.id, tmp_path)
    assert [p.name for p in written] == list(FILE_NAMES)
    assert [p.name for p in target_files(tmp_path)] == list(FILE_NAMES)
    for p in written:
        assert p.exists()

    telem = _read(tmp_path / "telemetry.csv")
    assert telem[0] == ["received_at", "arduino_time_ms", "ay", "shock", "distance_cm", "status"]
    assert len(telem) == 3                      # header + 2 rows
    assert telem[2][4] == "NA"                  # NA distance preserved honestly

    evrows = _read(tmp_path / "events.csv")
    assert evrows[0] == ["received_at", "arduino_time_ms", "event_type", "ay", "shock", "distance_cm", "notes"]
    assert evrows[1][2] == "POTHOLE"
    assert evrows[1][6] == "deep"
    datetime.fromisoformat(evrows[1][0])        # timestamp is ISO-8601

    meta = _read(tmp_path / "session_metadata.csv")
    assert meta[0] == ["id", "name", "started_at", "ended_at", "notes", "telemetry_count", "event_count"]
    assert meta[1][1] == "Road Test"
    assert meta[1][5] == "2"                     # telemetry_count
    assert meta[1][6] == "1"                     # event_count
    db.close()
