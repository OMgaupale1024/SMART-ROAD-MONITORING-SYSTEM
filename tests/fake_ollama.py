"""A stand-in for Ollama's HTTP API: for the tests, and for trying the dashboard on a computer without Ollama.

    python tests/fake_ollama.py [--port 11434] [--delay 3] [--invalid-every 4]

It answers POST /api/generate with a sentence built from the prompt's FACTS (no model involved) after --delay
seconds, and GET /api/tags with one model. With --invalid-every N, every Nth answer invents a stopping distance, to
show the dashboard falling back to the deterministic explanation.
"""
import argparse
import http.server
import json
import re
import socket
import threading
import time

MODEL = "qwen3:0.6b"
_WORDS = {"SLOW DOWN": "slowing down", "MONITOR": "monitoring it", "MAINTAIN": "keeping to the current speed target",
          "CONSIDER LANE CHANGE": "considering a lane change", "BRAKE": "braking", "EMERGENCY BRAKE": "emergency braking"}


def canned(body, invalid=False, took_s=0.0):
    """An /api/generate reply for a RoadSense explanation request, worded from the facts in its prompt."""
    prompt = body.get("prompt", "")
    fact = dict(re.findall(r"^(recommended action|overall risk|cause|recommended lane): (.*)$", prompt, re.M))
    action = fact.get("recommended action", "MAINTAIN")
    cause = fact.get("cause", "none").replace("the driver's", "your")
    text = "RoadSense recommends %s" % _WORDS.get(action, action.lower())
    if action == "CONSIDER LANE CHANGE":
        text = "RoadSense suggests considering a move to the %s" % fact.get("recommended lane", "other lane").split(" (")[0]
    text += " because of %s." % re.sub(r" \(severity (\w+)\)", r" of \1 severity", cause) if cause != "none" else "."
    if invalid:
        text += " You can stop within 25 meters."
    return {"model": body.get("model", MODEL), "response": json.dumps({"action": action.replace(" ", "_"), "explanation": text}),
            "done": True, "done_reason": "stop", "total_duration": int(took_s * 1e9), "load_duration": 9_000_000,
            "eval_count": len(text.split()) + 12, "eval_duration": int(took_s * 0.95e9)}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.server.requests.append(body)
        self.server.script(self, body)

    def do_GET(self):
        self.answer(200, {"models": [{"name": name} for name in self.server.models]})

    def answer(self, status, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the client cancelled

    def log_message(self, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # the default looks the host name up, which can stall for seconds
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        socket.socket.bind(self.socket, self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = "localhost", self.server_address[1]


def serve(port=0, script=None):
    """A running fake on 127.0.0.1 (port 0: any free one). .requests lists the generate bodies received; .script
    (handler, body) writes each answer and may be replaced; .url is what ROADSENSE_AI_URL should be."""
    server = Server(("127.0.0.1", port), Handler)
    server.requests, server.models = [], [MODEL]
    server.script = script or (lambda handler, body: handler.answer(200, canned(body)))
    server.url = "http://127.0.0.1:%d/api/generate" % server.server_port
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--delay", type=float, default=3.0, help="seconds each answer takes (default 3)")
    parser.add_argument("--invalid-every", type=int, default=0, help="every Nth answer makes an unsupported claim")
    args = parser.parse_args()
    count = [0]

    def script(handler, body):
        count[0] += 1
        time.sleep(args.delay)
        handler.answer(200, canned(body, bool(args.invalid_every) and count[0] % args.invalid_every == 0, args.delay))

    server = serve(args.port, script)
    print("fake Ollama at %s (delay %g s); Ctrl+C stops it" % (server.url, args.delay), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
