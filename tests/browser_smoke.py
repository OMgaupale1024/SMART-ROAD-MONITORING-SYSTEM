"""Manual browser regression: real local server, synthetic sensor frames, isolated Vivaldi.

Run with a Python that has the web extra and websockets:
  PYTHONPATH=src venv/bin/python tests/browser_smoke.py
Add --headed for a visible Vivaldi window.
Screenshots and results go to --output (default /tmp/roadsense-browser).
"""
import argparse
import base64
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
VIVALDI = "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(url, data=None, method=None):
    body = None if data is None else json.dumps(data).encode()
    with urlopen(Request(url, data=body, method=method, headers={"Content-Type": "application/json"}), timeout=3) as r:
        return json.load(r)


def until(check, timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (OSError, ValueError) as error:
            last = str(error)
        time.sleep(0.05)
    raise AssertionError(f"Timed out: {last}")


class Page:
    def __init__(self, url):
        self.connection = connect(url, max_size=16 * 1024 * 1024)
        self.socket = self.connection.__enter__()
        self.sequence = 0
        self.errors = []
        try:
            self.call("Runtime.enable")
            self.call("Page.enable")
        except Exception:
            self.connection.__exit__(*sys.exc_info())
            raise

    def call(self, method, **params):
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        while True:
            message = json.loads(self.socket.recv(timeout=8))
            if message.get("method") == "Runtime.exceptionThrown":
                self.errors.append(message["params"])
            if message.get("method") == "Runtime.consoleAPICalled" and message["params"]["type"] == "error":
                self.errors.append(message["params"])
            if message.get("id") == self.sequence:
                assert "error" not in message, message
                return message.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def text(self, element):
        return self.evaluate(f"document.getElementById({json.dumps(element)})?.textContent")


def frame(sequence, active=True):
    return {
        "schema": "roadsense.sensors.v1", "timestamp": round(sequence * 0.2, 3),
        "ego": {"vehicle_id": "BROWSER_TEST", "position": [-100, 6.875, 0.3], "velocity": [-10, 0],
                "speed_mps": 10, "yaw_rad": math.pi, "steering_rad": 0, "lane": 1, "maneuver": "lane_keep"},
        "radars": {"radar front": {"targets": [[30, 0, None]], "limits": [1, 150, 2.6]},
                   "radar rear": {"targets": [], "limits": [1, 100, 2.6]}},
        "objects": [[[48, 0, 0], "pothole severity=HIGH length=1.2 width=0.9 depth=0.08"]],
        "sides": {"left": [10, 10], "right": [10, 10]},
        "simulation": {"run_id": "browser-smoke", "sequence": sequence, "active": active},
    }


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise AssertionError("Owned test process did not stop on SIGTERM")


def run(args):
    if not Path(args.browser).is_file() or not os.access(args.browser, os.X_OK):
        raise SystemExit(f"Vivaldi executable unavailable: {args.browser}. Install/configure Vivaldi; no browser fallback is used.")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    server_port, debug_port, unavailable_port = port(), port(), port()
    base = f"http://127.0.0.1:{server_port}"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
               ROADSENSE_AI_EXPLAIN="1", ROADSENSE_AI_URL=f"http://127.0.0.1:{unavailable_port}/api/generate",
               ROADSENSE_AI_TIMEOUT_S="0.5")
    server = browser = page = None
    producer_stop = threading.Event()
    producer = None
    sequence = [0]
    failures = []
    results = {"source": "synthetic sensor frames; not Webots or Raspberry Pi", "browser": args.browser, "viewports": []}
    with tempfile.TemporaryDirectory(prefix="roadsense-browser-profile-") as profile, \
            (output / "server.log").open("w") as server_log, (output / "browser.log").open("w") as browser_log:
        def start_server():
            process = subprocess.Popen([sys.executable, "-m", "roadsense.web", "--port", str(server_port), "--no-browser"],
                                       cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
            try:
                until(lambda: http(base + "/api/system"))
            except Exception:
                stop(process)
                raise
            return process

        try:
            server = start_server()
            browser = subprocess.Popen([args.browser, *([] if args.headed else ["--headless"]), "--no-first-run",
                                        "--disable-extensions", "--disable-background-networking", "--disable-sync",
                                        "--no-default-browser-check", "--password-store=basic", "--use-mock-keychain",
                                        "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                                        f"--user-data-dir={profile}", f"--remote-debugging-port={debug_port}", "about:blank"],
                                       stdout=browser_log, stderr=subprocess.STDOUT)
            targets = until(lambda: [t for t in http(f"http://127.0.0.1:{debug_port}/json/list")
                                     if t["type"] == "page" and t["url"] == "about:blank"])
            try:
                page = Page(targets[0]["webSocketDebuggerUrl"])
            except TimeoutError as error:
                raise RuntimeError("Vivaldi debugger did not respond; browser checks could not run. "
                                   "No fallback browser was launched. See browser.log.") from error
            page.call("Page.navigate", url=base)
            until(lambda: page.text("statusText") == "NO PRODUCER")
            until(lambda: page.text("railAiNote") == "Ollama not reachable")
            results["no_producer"] = True

            def produce():
                try:
                    while not producer_stop.is_set():
                        sequence[0] += 1
                        http(base + "/api/sensor-frame", frame(sequence[0]))
                        producer_stop.wait(0.2)
                except Exception as error:
                    failures.append(str(error))

            producer = threading.Thread(target=produce)
            producer.start()
            until(lambda: page.text("statusText") == "LIVE")
            until(lambda: page.text("aiBadge") == "CURRENT")
            until(lambda: page.text("hazardCount") == "1" and page.text("tTracks") == "1")
            until(lambda: page.text("eProcess") not in (None, "—", "n/a"))
            assert page.text("nextAction") not in (None, "—")
            assert "fixed rules" in page.text("aiOrigin"), page.text("aiOrigin")
            assert page.evaluate("document.querySelector('#scene').getContext('webgl2') !== null")
            assert not page.evaluate("[...document.querySelectorAll('a')].some(a => /autonomy|arduino/i.test(a.href))")
            for width, height in [(1440, 900), (1024, 768), (820, 1180), (430, 932)]:
                page.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
                page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                dimensions = page.evaluate("({width:innerWidth, scroll:document.documentElement.scrollWidth, canvas:document.querySelector('#scene').getBoundingClientRect().toJSON()})")
                assert dimensions["scroll"] <= width, dimensions
                assert dimensions["canvas"]["width"] > 0 and dimensions["canvas"]["height"] > 0
                shot = page.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
                (output / f"dashboard-{width}.png").write_bytes(base64.b64decode(shot["data"]))
                results["viewports"].append({"width": width, "height": height, "overflow": False})
            assert not page.errors, page.errors
            results["normal_console_errors"] = 0
            results["live"] = {"action": page.text("nextAction"), "hazards": 1, "tracks": 1,
                               "explanation": page.text("aiBadge"), "ollama": "unavailable", "webgl2": True}
            producer_stop.set()
            producer.join(timeout=4)
            assert not failures, failures
            until(lambda: page.text("statusText") == "STALE")
            assert page.text("aiBadge") == "STALE"
            results["stale"] = True
            sequence[0] += 1
            http(base + "/api/sensor-frame", frame(sequence[0], active=False))
            until(lambda: page.text("statusText") == "SIMULATION ENDED")
            results["ended"] = True

            hostile = '<img src=x onerror="window.roadsenseInjected=true">'
            snapshot = http(base + "/api/live-state")
            snapshot["unified_safety"]["reason"] = hostile
            for seq in (1, 2):
                snapshot["simulation"].update(run_id="hostile-text", sequence=seq, active=True)
                http(base + "/api/live-state", snapshot)
            until(lambda: page.text("nextReason") == hostile)
            assert page.evaluate("!window.roadsenseInjected && !document.querySelector('#nextReason img')")
            assert not page.errors, page.errors
            results["untrusted_text"] = True

            # A page with WebGL disabled must retain its safety and diagnostics panels.
            page.call("Page.addScriptToEvaluateOnNewDocument", source="""
                const original = HTMLCanvasElement.prototype.getContext;
                HTMLCanvasElement.prototype.getContext = function(kind, ...args) {
                    return /webgl/.test(kind) ? null : original.call(this, kind, ...args);
                };
            """)
            page.errors.clear()
            page.call("Page.reload")
            until(lambda: page.text("statusText") == "CONNECTING")
            for seq in (1, 2):
                data = frame(seq)
                data["simulation"]["run_id"] = "webgl-fallback"
                http(base + "/api/sensor-frame", data)
            until(lambda: page.text("statusText") == "LIVE")
            until(lambda: page.text("noticeTitle") == "3D view unavailable")
            assert page.text("nextAction") not in (None, "—")
            results["webgl_fallback"] = True
            results["expected_webgl_errors"] = len(page.errors)

            stop(server)
            until(lambda: page.text("statusText") == "DISCONNECTED")
            results["disconnected"] = True
            server = start_server()
            until(lambda: page.text("statusText") == "NO PRODUCER")
            results["reconnected"] = True
        finally:
            producer_stop.set()
            if producer:
                producer.join(timeout=4)
            # Attempt every cleanup even if closing the debugger or a process fails.
            with ExitStack() as cleanup:
                cleanup.callback(stop, server)
                cleanup.callback(stop, browser)
                if page:
                    cleanup.callback(page.connection.__exit__, None, None, None)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=VIVALDI, help="Vivaldi executable (default: %(default)s); no automatic fallback")
    parser.add_argument("--headed", action="store_true", help="Show the isolated Vivaldi test window")
    parser.add_argument("--output", default="/tmp/roadsense-browser")
    return parser.parse_args(argv)


if __name__ == "__main__":
    run(parse_args())
