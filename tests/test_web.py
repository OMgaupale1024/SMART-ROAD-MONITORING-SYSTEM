"""Web server: lifecycle, REST and WebSocket contracts of the FastAPI app."""
import io
import sqlite3
import threading
import time
import zipfile

import pytest

pytest.importorskip("fastapi")  # web extra not installed
pytest.importorskip("httpx2")  # dev extra not installed (FastAPI's TestClient needs it)

from fastapi.testclient import TestClient
from serial.tools.list_ports_common import ListPortInfo

import roadsense
from roadsense.web import server, service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADSENSE_DATA_DIR", str(tmp_path))  # the app opens its database at startup
    with TestClient(server.app) as c:
        yield c


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


def recv(ws, timeout=5.0):
    """ws.receive_json() with a timeout, so a missing message fails the test instead of hanging it."""
    box = []
    reader = threading.Thread(target=lambda: box.append(ws.receive_json()), daemon=True)
    reader.start()
    reader.join(timeout)
    assert box, f"no WebSocket message within {timeout} s"
    return box[0]


def recv_until(ws, predicate, limit=500):
    seen = [recv(ws)]
    while not predicate(seen[-1]):
        assert len(seen) < limit, "expected WebSocket message never arrived"
        seen.append(recv(ws))
    return seen


def test_api_version_is_package_version():
    assert server.app.version == roadsense.__version__


def test_server_starts_with_no_source_and_shuts_down_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADSENSE_DATA_DIR", str(tmp_path))
    with TestClient(server.app) as client:
        status = client.get("/api/status").json()
        assert (status["source"], status["total_packets_received"]) == ("NONE", 0)
        client.post("/api/simulator/start")
        manager = server.manager

    assert manager.get_status()["source"] == "NONE"  # shutdown stopped the simulator...
    with pytest.raises(sqlite3.ProgrammingError):
        manager.db.conn.execute("SELECT 1")  # ...and closed the database


def test_dashboard_and_read_endpoints_respond(client, monkeypatch):
    port = ListPortInfo("/dev/cu.usbmodem1101")
    port.description, port.hwid = "Arduino Uno", "USB VID:PID=2341:0043"
    monkeypatch.setattr(service.serial.tools.list_ports, "comports", lambda: [port])

    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["version"] == roadsense.__version__
    assert client.get("/api/status").json()["source"] == "NONE"
    assert client.get("/api/sessions").json() == {"sessions": []}
    assert client.get("/api/ports").json() == {"ports": [
        {"port": "/dev/cu.usbmodem1101", "description": "Arduino Uno", "hwid": "USB VID:PID=2341:0043"}]}


def test_simulator_endpoints_switch_the_source(client):
    trigger = {"event_type": "POTHOLE"}
    assert client.post("/api/simulator/trigger", json=trigger).json()["status"] == "error"

    assert client.post("/api/simulator/start", json={"profile": "bumpy"}).json()["status"] == "ok"
    status = client.get("/api/status").json()
    assert (status["source"], status["is_simulator"], status["connection_status"]) == (
        "SIMULATOR", True, "Connected")
    assert client.post("/api/simulator/trigger", json=trigger).json() == {"status": "ok", "triggered": "POTHOLE"}

    client.post("/api/simulator/stop")
    status = client.get("/api/status").json()
    assert (status["source"], status["is_simulator"]) == ("NONE", False)
    assert client.post("/api/simulator/trigger", json=trigger).json()["status"] == "error"


def test_recording_endpoints_save_and_export_a_session(client, monkeypatch):
    monkeypatch.setattr(service, "SIM_PERIOD_S", 60)  # one packet, then the simulator idles
    client.post("/api/simulator/start")
    wait_for(lambda: client.get("/api/status").json()["total_packets_received"] >= 1)

    started = client.post("/api/recording/start", json={"name": "Test drive", "notes": "bumpy"}).json()
    sid = started["session_id"]
    client.post("/api/simulator/trigger", json={"event_type": "POTHOLE"})  # one T + one E packet
    stopped = client.post("/api/recording/stop").json()
    assert (stopped["session_name"], stopped["telemetry_count"], stopped["event_count"]) == ("Test drive", 1, 1)

    [session] = client.get("/api/sessions").json()["sessions"]
    assert (session["id"], session["notes"], session["telemetry_count"], session["event_count"]) == (
        sid, "bumpy", 1, 1)
    events = client.get(f"/api/sessions/{sid}/events").json()["events"]
    assert [e["status"] for e in events] == ["POTHOLE"]

    export = client.get(f"/api/sessions/{sid}/export/zip")
    assert export.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(export.content))
    assert sorted(archive.namelist()) == ["events.csv", "session_metadata.csv", "telemetry.csv"]
    assert len(archive.read("telemetry.csv").decode().splitlines()) == 2  # header + 1 row
    assert client.get("/api/sessions/999/export/zip").status_code == 404


def test_websocket_reports_source_and_recording_changes(client, monkeypatch):
    monkeypatch.setattr(service, "SIM_PERIOD_S", 0.01)
    with client.websocket_connect("/ws/telemetry") as ws:
        init = recv(ws)
        assert (init["type"], init["status"]["source"]) == ("init", "NONE")

        client.post("/api/simulator/start")
        started = recv_until(ws, lambda m: m["type"] == "packet")
        assert [m["status"]["source"] for m in started if m["type"] == "status"] == ["SIMULATOR"]

        client.post("/api/simulator/stop")
        stopped = recv_until(ws, lambda m: m["type"] == "status")  # live packets until the stop
        assert stopped[-1]["status"]["source"] == "NONE"

        client.post("/api/recording/start")  # no source: the UI still learns recording began
        assert recv(ws)["status"]["is_recording"] is True
        client.post("/api/recording/stop")
        assert recv(ws)["status"]["is_recording"] is False


def test_api_is_not_shared_with_other_origins(client):
    response = client.get("/api/status", headers={"Origin": "http://example.com"})
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        "/api/recording/start",
        headers={"Origin": "http://example.com", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers
