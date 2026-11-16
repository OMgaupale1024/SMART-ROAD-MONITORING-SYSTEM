"""Web server: the one dashboard, its assets, the routes that exist (and those that no longer do), and the process's
start and stop."""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import pytest

pytest.importorskip("fastapi")  # web extra not installed
pytest.importorskip("httpx2")  # dev extra not installed (FastAPI's TestClient needs it)

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import roadsense
from roadsense.web import __main__ as cli
from roadsense.web import server
from test_web_live import sensor_frame

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
STATIC = SRC / "roadsense" / "web" / "static"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ROADSENSE_AI_EXPLAIN", raising=False)
    with TestClient(server.app) as c:
        yield c


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.005)


class Page(HTMLParser):
    """What a browser takes from the page: the scripts and stylesheets to fetch, the links, and the ids."""

    def __init__(self, text):
        super().__init__()
        self.assets, self.links, self.ids = [], [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "script" and attrs.get("src"):
            self.assets.append(attrs["src"])
        elif tag == "link" and attrs.get("href") and "stylesheet" in attrs.get("rel", "").split():
            self.assets.append(attrs["href"])
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


def is_remote(url):
    return bool(urlsplit(url).netloc)  # http://, https:// and protocol-relative //host


def dashboard_files(client):
    """Every file the dashboard loads, as {path: text}: the page's assets and, as a browser would, what its ES
    modules import (three.js's own imports too)."""
    pending, fetched = list(Page(client.get("/").text).assets), {}
    while pending:
        ref = pending.pop()
        assert not is_remote(ref), ref
        if ref in fetched:
            continue
        response = client.get(ref)
        assert response.status_code == 200, ref
        fetched[ref] = response.text
        if ref.endswith(".js"):
            assert "javascript" in response.headers["content-type"], ref  # browsers refuse modules served otherwise
            for spec in re.findall(r"""\bfrom\s*["']([^"']+)["']|\bimport\s*\(?\s*["']([^"']+)["']""", response.text):
                spec = "".join(spec)
                assert spec.startswith((".", "/")) or is_remote(spec), f"{ref} imports {spec!r}: no import map here"
                pending.append(spec if is_remote(spec) else urljoin(ref, spec))
        if ref.endswith(".css"):
            for url in re.findall(r"url\(['\"]?([^'\")]+)", response.text):
                if not url.startswith("data:"):
                    pending.append(urljoin(ref, url))
    return fetched


def test_api_version_is_package_version():
    assert server.app.version == roadsense.__version__


# --- one dashboard


def test_the_dashboard_is_the_homepage(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "<title>RoadSense</title>" in page.text
    for section in ("System status", "Next move", "Explanation", "Vehicle telemetry", "Road hazards", "Edge processing",
                    "Recent events"):
        assert section in page.text, section


def test_the_old_autonomy_address_redirects_to_the_dashboard_instead_of_being_a_second_one(client):
    response = client.get("/autonomy", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/"
    assert client.get("/autonomy").text == client.get("/").text


def test_only_the_edge_routes_exist(client):
    routes = {route.path: sorted(getattr(route, "methods", None) or ["WS"]) for route in server.app.routes}
    assert routes == {
        "/": ["GET"], "/autonomy": ["GET"], "/static": ["WS"],  # (a mount has no methods of its own)
        "/api/sensor-frame": ["POST"], "/api/live-state": ["POST"], "/ws/live": ["WS"],
        "/api/system": ["GET"], "/api/ai-explanation": ["GET"], "/api/hazards": ["GET"],
        "/openapi.json": ["GET", "HEAD"],
    } or {path for path in routes} == {
        "/", "/autonomy", "/static", "/api/sensor-frame", "/api/live-state", "/ws/live", "/api/system",
        "/api/ai-explanation", "/api/hazards", "/openapi.json"}
    paths = client.get("/openapi.json").json()["paths"]
    # nothing but the two ingestion routes changes anything: the diagnostics and the explanation are read-only
    assert {path for path, methods in paths.items() if set(methods) - {"get"}} == {"/api/sensor-frame", "/api/live-state"}
    assert set(paths["/api/live-state"]) == {"get", "post"} and set(paths["/api/sensor-frame"]) == {"post"}


@pytest.mark.parametrize("path", [
    "/api/status", "/api/ports", "/api/sessions", "/api/diagnostics/raw", "/api/sessions/1/events",
    "/api/sessions/1/export/zip", "/docs", "/redoc",
    "/static/app.js", "/static/styles.css", "/static/autonomy/index.html", "/static/autonomy/main.js",
    "/static/vendor/chart.js/chart.umd.js", "/static/vendor/fontawesome/css/fontawesome.min.css",
])
def test_the_arduino_dashboard_its_api_and_its_assets_are_gone(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", [
    "/api/connect", "/api/disconnect", "/api/simulator/start", "/api/simulator/stop", "/api/simulator/trigger",
    "/api/recording/start", "/api/recording/stop",
])
def test_the_arduino_controls_are_gone(client, path):
    assert client.post(path, json={}).status_code == 404


def test_the_arduino_telemetry_socket_is_gone(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/telemetry"):
            pass


def test_the_page_has_no_remnant_of_the_retired_interfaces(client):
    files = dashboard_files(client)
    files["/"] = client.get("/").text
    ours = {ref: text for ref, text in files.items() if "/vendor/" not in ref}
    for ref, text in ours.items():
        for word in ("arduino", "com port", "serial", "/api/simulator", "/api/recording", "/ws/telemetry",
                     "bumpy", "fontawesome", "chart.umd", "/autonomy"):
            assert word not in text.lower(), f"{ref} still mentions {word!r}"
    page = Page(files["/"])
    assert page.links == []  # one page: nowhere else to go, and no tab bar
    assert not re.search(r"\bhidden\b[^>]*class=\"[^\"]*\btab", files["/"]) and "role=\"tab\"" not in files["/"]


# --- assets


def test_dashboard_and_every_module_it_imports_come_from_this_server(client):
    files = dashboard_files(client)
    assert set(files) == {
        "/static/dashboard.css", "/static/js/main.js", "/static/js/live.js", "/static/js/view.js",
        "/static/js/scene.js", "/static/js/system.js", "/static/js/charts.js",
        "/static/vendor/three/three.module.min.js", "/static/vendor/three/three.core.min.js"}
    for ref, text in files.items():
        if "/vendor/" not in ref:
            text = text.replace("http://www.w3.org/2000/svg", "")  # the SVG namespace: a name, never fetched
            assert not re.search(r"https?://", text), ref  # nothing is fetched from the internet
    page = client.get("/").text
    assert not re.search(r"""(src|href)=["'](https?:)?//""", page)


def test_the_page_loads_each_asset_once(client):
    assets = Page(client.get("/").text).assets
    assert assets == ["/static/dashboard.css", "/static/js/main.js"]
    assert client.get("/").text.count("<script") == 1


def test_static_directory_holds_only_what_the_dashboard_uses(client):
    served = {"/static/" + path.relative_to(STATIC).as_posix() for path in STATIC.rglob("*") if path.is_file()}
    used = set(dashboard_files(client)) | {"/static/index.html", "/static/vendor/three/LICENSE"}
    assert served == used  # no orphaned or duplicated asset


def test_every_element_the_script_looks_up_is_on_the_page(client):
    files = dashboard_files(client)
    page = Page(client.get("/").text)
    assert len(page.ids) == len(set(page.ids)), "duplicate ids"
    wanted = set(re.findall(r"\$\('([\w-]+)'\)", files["/static/js/main.js"]))
    assert len(wanted) > 60 and not wanted - set(page.ids), sorted(wanted - set(page.ids))


def test_assets_are_revalidated_so_an_update_is_never_mixed_with_cached_files(client):
    for path in ("/", "/static/js/main.js", "/static/dashboard.css"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


# --- what the page promises


def test_network_text_is_only_ever_put_on_the_page_as_text(client):
    """Ids, reasons and the model's explanation come from the network: no script may write markup with them."""
    files = dashboard_files(client)
    for ref, text in files.items():
        if "/vendor/" in ref or not ref.endswith(".js"):
            continue
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment",
                     "srcdoc", "eval("):
            assert sink not in text, f"{ref} uses {sink}"
    main = files["/static/js/main.js"]
    assert "setText($('aiText')" in main and "element.textContent" in main


def test_recommendations_are_labelled_as_simulation_only_and_the_explanation_as_wording_only(client):
    page = " ".join(client.get("/").text.split())
    assert "Recommendations for driving in the simulation." in page
    assert "not validated commands for a real vehicle" in page
    assert "The explanation only words the decision above. It cannot change it." in page
    assert "Real GPS mapping is not configured" in page  # no geographic position is made up for simulated hazards


def test_all_six_actions_and_all_six_connection_states_are_known_to_the_page(client):
    files = dashboard_files(client)
    for action in ("MAINTAIN", "MONITOR", "SLOW_DOWN", "BRAKE", "EMERGENCY_BRAKE", "CONSIDER_LANE_CHANGE"):
        assert re.search(r"\b%s\b" % action, files["/static/js/view.js"]), action
    for state in ("LIVE", "CONNECTING", "STALE", "DISCONNECTED", "NO PRODUCER", "SIMULATION ENDED"):
        assert "'%s'" % state in files["/static/js/live.js"], state
    # a state that is not LIVE dims what shows it
    assert 'body:not([data-status="LIVE"])' in files["/static/dashboard.css"]


def test_page_adapts_to_tablets_and_survives_without_webgl(client):
    files = dashboard_files(client)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in client.get("/").text
    widths = sorted(int(w) for w in re.findall(r"@media \(max-width: (\d+)px\)", files["/static/dashboard.css"]))
    assert len(widths) >= 2 and widths[0] <= 820 and widths[-1] >= 1024  # a phone-width and a tablet-width layout
    assert "prefers-reduced-motion" in files["/static/dashboard.css"]
    main = files["/static/js/main.js"]
    # the 3D module is loaded on its own; if it cannot start, the failure is caught and the panels carry on
    assert re.search(r"import\('\./scene\.js'\)\s*\.then\(.*?\)\s*\.catch\(", main, re.S)
    assert "from './scene.js'" not in main and "3D view unavailable" in main


def test_the_frontend_decides_nothing_about_safety(client):
    """The page draws RoadSense's state: no thresholds of its own. The TTC chart's reference levels come from the
    server, which reads them from the safety processor."""
    files = dashboard_files(client)
    assert server.SAFETY == {"critical_ttc_s": 1.5, "high_ttc_s": 3.0, "horizon_s": 5.0, "nominal_speed_kmh": 80.0}
    assert "safety.high_ttc_s" in files["/static/js/main.js"] and "safety.critical_ttc_s" in files["/static/js/main.js"]
    for ref in ("/static/js/main.js", "/static/js/view.js", "/static/js/scene.js", "/static/js/charts.js"):
        assert not re.search(r"ttc\s*[<>]=?\s*\d", files[ref], re.I), ref


# --- the process


def test_api_is_not_shared_with_other_origins(client):
    response = client.get("/api/system", headers={"Origin": "http://example.com"})
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        "/api/sensor-frame", headers={"Origin": "http://example.com", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in preflight.headers


def test_cli_listens_on_this_computer_only_unless_told_otherwise(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_web", lambda **kwargs: calls.append(kwargs))
    cli.main([])
    cli.main(["--host", "0.0.0.0", "--port", "8001", "--no-browser"])  # the Raspberry Pi on the LAN
    assert calls == [
        {"host": "127.0.0.1", "port": 8000, "open_browser": True},
        {"host": "0.0.0.0", "port": 8001, "open_browser": False},
    ]


def test_run_web_opens_a_browser_only_when_asked_and_warns_about_lan_access(monkeypatch, capsys):
    import uvicorn

    served, opened = [], []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append((kwargs["host"], kwargs["port"])))
    monkeypatch.setattr(server.threading, "Timer", lambda _delay, open_page: SimpleNamespace(start=open_page))
    monkeypatch.setattr(server.webbrowser, "open", opened.append)

    server.run_web(host="0.0.0.0", port=8001, open_browser=False)  # headless edge device
    server.run_web(host="0.0.0.0", port=8081)
    lan = capsys.readouterr().out
    server.run_web()
    local = capsys.readouterr().out

    assert served == [("0.0.0.0", 8001), ("0.0.0.0", 8081), ("127.0.0.1", 8000)]
    assert opened == ["http://127.0.0.1:8081", "http://127.0.0.1:8000"]  # never http://0.0.0.0
    assert "There is no login" in lan and "There is no login" not in local


def subprocess_env(**extra):
    """The environment for running this checkout's roadsense in a child process."""
    path = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))
    env = {key: value for key, value in os.environ.items() if not key.startswith("ROADSENSE_")}
    return dict(env, PYTHONPATH=path, **extra)


def test_edge_server_needs_only_the_web_extra():
    """The Pi has the web dependencies and nothing else: no Qt, no serial port library, no model client. The
    Arduino stack is archived under legacy/, outside the package."""
    code = ("import sys\n"
            "sys.modules.update(PySide6=None, pyqtgraph=None, serial=None, ollama=None, psutil=None)\n"  # importing any now fails
            "import roadsense.web.__main__, roadsense.web.server\n"
            "import roadsense, pkgutil\n"
            "print(sorted(m.name for m in pkgutil.iter_modules(roadsense.__path__)))\n")
    result = subprocess.run([sys.executable, "-c", code], env=subprocess_env(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['autonomy', 'web']"


SYSTEM_PYTHON = "/usr/bin/python3"  # what Webots runs controllers with on this Mac: Python 3.9


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON), reason="no system Python")
def test_webots_interpreter_can_still_import_the_shared_autonomy_package():
    """The Webots controller imports roadsense.autonomy from src/ with its own, older Python: the package (and
    roadsense/__init__.py on the way) must stay within what that Python understands."""
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from roadsense.autonomy import hazard_map, live_publisher, live_state, perception, pipeline, risk, safety, telemetry\n"
            "print(sys.version_info[:2])\n" % str(SRC))
    result = subprocess.run([SYSTEM_PYTHON, "-c", code], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert result.returncode == 0, result.stderr


def test_server_stops_promptly_after_an_idle_dashboard_disconnects():
    uvicorn = pytest.importorskip("uvicorn")
    from websockets.sync.client import connect

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    real_server = uvicorn.Server(uvicorn.Config(server.app, log_level="warning"))
    thread = threading.Thread(target=real_server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_for(lambda: real_server.started)

    with connect(f"ws://127.0.0.1:{port}/ws/live"):  # no producer, so nothing streams; then the tab closes
        pass
    real_server.should_exit = True  # what Ctrl+C does
    thread.join(5)
    assert not thread.is_alive(), "server still waiting on the closed dashboard's WebSocket"


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
        return bool(api(port, "system"))
    except OSError:
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_signal_stops_the_server_even_while_the_model_is_not_answering(sig):
    """Ctrl+C, and `systemctl stop` (SIGTERM), on the real process, started the way the Pi starts it (PYTHONPATH, no
    virtual environment activated), with AI explanations on and an Ollama that accepts a request and never answers."""
    hung = socket.socket()
    hung.bind(("127.0.0.1", 0))
    hung.listen(5)  # connections are accepted by the kernel and never answered
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "roadsense.web", "--port", str(port), "--no-browser"],
        env=subprocess_env(ROADSENSE_AI_EXPLAIN="1", ROADSENSE_AI_TIMEOUT_S="120",
                           ROADSENSE_AI_URL="http://127.0.0.1:%d/api/generate" % hung.getsockname()[1]),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: proc.poll() is not None or answers(port), timeout=60)  # a Pi starts slower
        assert proc.poll() is None, proc.communicate()[0]
        frame = sensor_frame()
        frame["objects"] = [[[75, 0, 0], "pothole severity=HIGH length=1.2 width=0.9 depth=0.08"]]
        for side in ("left", "right"):
            frame["sides"][side][0] = 2
        assert api(port, "sensor-frame", frame)["accepted"] is True  # a decision the model is asked to explain
        wait_for(lambda: api(port, "ai-explanation")["status"] == "generating")
        assert api(port, "sensor-frame", sensor_frame(2, 0.4))["accepted"] is True  # frames go on meanwhile
        started = time.monotonic()
        proc.send_signal(sig)
        output = proc.communicate(timeout=30)[0]
        took = time.monotonic() - started
    finally:
        hung.close()
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    # uvicorn re-raises SIGTERM once shut down; systemd counts that as a clean stop
    assert proc.returncode == (0 if sig == signal.SIGINT else -signal.SIGTERM), output
    assert "Application shutdown complete" in output and "Traceback" not in output, output
    assert took < 10, "shutdown waited %.1f s for the model" % took
