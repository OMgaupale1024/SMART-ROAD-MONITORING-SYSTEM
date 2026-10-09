"""Web server: lifecycle, REST and WebSocket contracts of the FastAPI app."""
import io
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import pytest

pytest.importorskip("fastapi")  # web extra not installed
pytest.importorskip("httpx2")  # dev extra not installed (FastAPI's TestClient needs it)

from fastapi.testclient import TestClient
from serial.tools.list_ports_common import ListPortInfo

import roadsense
from roadsense.database import Database
from roadsense.repositories import SessionRepository, TelemetryRepository
from roadsense.web import __main__ as cli
from roadsense.web import server, service

SRC = Path(__file__).resolve().parents[1] / "src"


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
    ports = []
    for device, description in [
        ("COM3", "Arduino Uno (COM3)"),  # Windows
        ("/dev/ttyUSB0", "USB2.0-Serial"),  # Linux: CH340-based UNO clone
        ("/dev/ttyACM0", "Arduino Uno"),  # Linux (Raspberry Pi): genuine UNO
        ("/dev/cu.usbmodem1101", "Arduino Uno"),  # macOS
    ]:
        port = ListPortInfo(device)
        port.description, port.hwid = description, "USB VID:PID=2341:0043"
        ports.append(port)
    monkeypatch.setattr(service.serial.tools.list_ports, "comports", lambda: ports)

    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["version"] == roadsense.__version__
    assert client.get("/api/status").json()["source"] == "NONE"
    assert client.get("/api/sessions").json() == {"sessions": []}
    listed = client.get("/api/ports").json()["ports"]
    assert [(p["port"], p["description"]) for p in listed] == [
        ("/dev/cu.usbmodem1101", "Arduino Uno"), ("/dev/ttyACM0", "Arduino Uno"),
        ("/dev/ttyUSB0", "USB2.0-Serial"), ("COM3", "Arduino Uno (COM3)")]
    assert {p["hwid"] for p in listed} == {"USB VID:PID=2341:0043"}


class AssetRefs(HTMLParser):
    """Collect what the browser must fetch to render the page: scripts and stylesheets."""

    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.refs.append(attrs["src"])
        elif tag == "link" and attrs.get("href") and "stylesheet" in attrs.get("rel", "").split():
            self.refs.append(attrs["href"])


def is_remote(url):
    return bool(urlsplit(url).netloc)  # http://, https:// and protocol-relative //host


def test_dashboard_loads_every_asset_from_this_server(client):
    """No CDN: the page, its scripts, stylesheets and the fonts they load all come from RoadSense.
    Plain hyperlinks (e.g. to /docs) are navigation, not dependencies, so they are not checked."""
    parser = AssetRefs()
    parser.feed(client.get("/").text)
    assert "/static/vendor/chart.js/chart.umd.js" in parser.refs
    assert not [ref for ref in parser.refs if is_remote(ref)]

    fetched = []
    for ref in parser.refs:
        response = client.get(ref)
        assert response.status_code == 200, ref
        fetched.append(ref)
        if ref.endswith(".css"):  # fonts and @imports a stylesheet pulls in
            for url in re.findall(r"url\(['\"]?([^'\")]+)", response.text):
                if not url.startswith("data:"):
                    assert not is_remote(url), url
                    assert client.get(urljoin(ref, url)).status_code == 200, url
                    fetched.append(url)
    assert any(url.endswith(".woff2") for url in fetched)  # the icon font
    assert not re.search(r"https?://", client.get("/static/app.js").text)


def test_autonomy_page_and_every_module_it_imports_come_from_this_server(client):
    """The 3D page's scripts are ES modules: follow their imports (and three.js's own) as a browser would."""
    page = client.get("/autonomy")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    parser = AssetRefs()
    parser.feed(page.text)
    assert "/static/autonomy/main.js" in parser.refs
    pending, fetched = list(parser.refs), set()
    while pending:
        ref = pending.pop()
        assert not is_remote(ref), ref
        if ref in fetched:
            continue
        response = client.get(ref)
        assert response.status_code == 200, ref
        fetched.add(ref)
        if ref.endswith(".js"):
            assert "javascript" in response.headers["content-type"], ref  # browsers refuse modules served otherwise
            for spec in re.findall(r"""\bfrom\s*["']([^"']+)["']|\bimport\s*\(?\s*["']([^"']+)["']""", response.text):
                spec = "".join(spec)
                assert spec.startswith((".", "/")) or is_remote(spec), f"{ref} imports {spec!r}: no import map here"
                pending.append(spec if is_remote(spec) else urljoin(ref, spec))
    assert {"/static/autonomy/live.js", "/static/autonomy/view.js", "/static/autonomy/scene.js",
            "/static/vendor/three/three.module.min.js", "/static/vendor/three/three.core.min.js"} <= fetched
    for ref in fetched:
        if ref.startswith("/static/autonomy/"):
            assert not re.search(r"https?://", client.get(ref).text), ref


def test_dashboard_and_autonomy_page_link_to_each_other(client):
    assert 'href="/autonomy"' in client.get("/").text
    assert 'href="/"' in client.get("/autonomy").text


def test_dashboard_starts_even_if_charts_fail():
    """A browser can't run here, so this pins the guard itself: a chart error is logged and
    startup goes on to open the WebSocket. (Checked in a real browser for v0.2.1.)"""
    app_js = (server.STATIC_DIR / "app.js").read_text()
    assert re.search(r"try \{\s*initCharts\(\);\s*\} catch \(err\) \{[^}]*console\.error", app_js)
    assert app_js.index("initCharts();") < app_js.index("initWebSocket();\n")


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


def test_server_stops_promptly_after_an_idle_dashboard_disconnects(tmp_path, monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    from websockets.sync.client import connect

    monkeypatch.setenv("ROADSENSE_DATA_DIR", str(tmp_path))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    real_server = uvicorn.Server(uvicorn.Config(server.app, log_level="warning"))
    thread = threading.Thread(target=real_server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: real_server.started)

    with connect(f"ws://127.0.0.1:{port}/ws/telemetry") as ws:  # no source, so nothing streams
        assert json.loads(ws.recv(timeout=5))["type"] == "init"
    real_server.should_exit = True  # what Ctrl+C does
    thread.join(5)
    assert not thread.is_alive(), "server still waiting on the closed dashboard's WebSocket"


def test_api_is_not_shared_with_other_origins(client):
    response = client.get("/api/status", headers={"Origin": "http://example.com"})
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        "/api/recording/start",
        headers={"Origin": "http://example.com", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


def test_cli_listens_on_this_computer_only_unless_told_otherwise(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_web", lambda **kwargs: calls.append(kwargs))
    cli.main([])
    cli.main(["--host", "0.0.0.0", "--port", "8080", "--no-browser"])  # Raspberry Pi on the LAN
    assert calls == [
        {"host": "127.0.0.1", "port": 8000, "open_browser": True},
        {"host": "0.0.0.0", "port": 8080, "open_browser": False},
    ]


def test_run_web_opens_a_browser_only_when_asked_and_warns_about_lan_access(monkeypatch, capsys):
    import uvicorn

    served, opened = [], []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append((kwargs["host"], kwargs["port"])))
    monkeypatch.setattr(server.threading, "Timer", lambda _delay, open_page: SimpleNamespace(start=open_page))
    monkeypatch.setattr(server.webbrowser, "open", opened.append)

    server.run_web(host="0.0.0.0", port=8080, open_browser=False)  # headless edge gateway
    server.run_web(host="0.0.0.0", port=8081)
    lan = capsys.readouterr().out
    server.run_web()
    local = capsys.readouterr().out

    assert served == [("0.0.0.0", 8080), ("0.0.0.0", 8081), ("127.0.0.1", 8000)]
    assert opened == ["http://127.0.0.1:8081", "http://127.0.0.1:8000"]  # never http://0.0.0.0
    assert "There is no login" in lan and "There is no login" not in local


def subprocess_env(**extra):
    """The environment for running this checkout's roadsense in a child process."""
    path = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))
    return dict(os.environ, PYTHONPATH=path, **extra)


def test_web_runtime_never_imports_the_desktop_gui():
    """A Raspberry Pi installs only the web extra, so the web runtime must not need PySide6 or
    pyqtgraph, even where they happen to be installed."""
    code = ("import sys\n"
            "sys.modules.update(PySide6=None, pyqtgraph=None)\n"  # importing either now fails
            "import roadsense.web.__main__, roadsense.web.server\n")
    result = subprocess.run([sys.executable, "-c", code], env=subprocess_env(),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def api(port, path, body=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{path}", data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with no_proxy.open(request, timeout=5) as response:
        return json.load(response)


def answers(port):
    try:
        return bool(api(port, "status"))
    except OSError:
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_signal_stops_the_server_and_saves_the_recording(tmp_path, sig):
    """Ctrl+C, and `systemctl stop` (SIGTERM), on the real process: the source stops, buffered
    telemetry reaches SQLite, the session is ended and the process exits."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "roadsense.web", "--port", str(port), "--no-browser"],
        env=subprocess_env(ROADSENSE_DATA_DIR=str(tmp_path)),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: proc.poll() is not None or answers(port), timeout=60)  # a Pi starts slower
        assert proc.poll() is None, proc.communicate()[0]
        api(port, "simulator/start", {})
        sid = api(port, "recording/start", {"name": "drive"})["session_id"]
        wait_for(lambda: api(port, "status")["session_telemetry_count"] >= 3)
        proc.send_signal(sig)
        output = proc.communicate(timeout=30)[0]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    # uvicorn re-raises SIGTERM once shut down; systemd counts that as a clean stop
    assert proc.returncode == (0 if sig == signal.SIGINT else -signal.SIGTERM), output
    assert "Application shutdown complete" in output and "Traceback" not in output, output
    with Database(tmp_path / "roadsense.db") as db:
        assert SessionRepository(db.conn).get(sid).ended_at is not None
        assert TelemetryRepository(db.conn).count(sid) >= 3
