"""Optional live-state publisher: POSTs each encoded snapshot to the RoadSense web server, off the control loop.

Standard library only (Webots runs controllers with Python 3.9). The controller hands over the newest snapshot and
never waits for the network: a background thread sends one at a time, and a newer snapshot replaces one still
waiting. If the server can't be reached, the publisher says so once, retries with exponential backoff (with the
newest snapshot only) and says so once more when it gets through again. Nothing it does can stop the simulation.
"""
import math
import os
import threading
import time
import urllib.error
import urllib.request
from typing import NamedTuple

DEFAULT_URL = "http://127.0.0.1:8000/api/live-state"  # python -m roadsense.web's default address


class Config(NamedTuple):
    url: str = DEFAULT_URL
    period_s: float = 0.2  # every 200 ms processing cycle; snapshots can't come faster than the cycles
    timeout_s: float = 0.5  # per request; the server is normally on this computer


def config_from_environment(environ=os.environ, log=print):
    """The publisher's Config, or None if ROADSENSE_LIVE_PUBLISH isn't on (1, true, yes, on). ROADSENSE_LIVE_URL,
    ROADSENSE_LIVE_PERIOD_S and ROADSENSE_LIVE_TIMEOUT_S override the defaults; an unusable value is reported and
    ignored."""
    if environ.get("ROADSENSE_LIVE_PUBLISH", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    defaults, numbers = Config(), {}
    for field, name in (("period_s", "ROADSENSE_LIVE_PERIOD_S"), ("timeout_s", "ROADSENSE_LIVE_TIMEOUT_S")):
        if name in environ:
            try:
                value = float(environ[name])
            except ValueError:
                value = float("nan")
            if math.isfinite(value) and 0 < value <= 60:
                numbers[field] = value
            else:
                log("[RoadSense:LIVE] ignoring %s=%r, using %s" % (name, environ[name], getattr(defaults, field)))
    return Config(environ.get("ROADSENSE_LIVE_URL") or defaults.url, **numbers)


def _describe(error):
    if isinstance(error, urllib.error.HTTPError):
        return "HTTP %d %s" % (error.code, error.read(200).decode("utf-8", "replace"))
    if isinstance(error, urllib.error.URLError):
        return str(error.reason)
    return str(error) or type(error).__name__


class LivePublisher:
    """Sends snapshots (bytes) to url from a daemon thread; publish() never blocks."""

    def __init__(self, url, timeout_s, log=print, first_backoff_s=1.0, max_backoff_s=10.0, send=None):
        self.url, self.timeout_s, self._log = url, timeout_s, log
        self._first_backoff_s, self._max_backoff_s = first_backoff_s, max_backoff_s
        self._send = send or self._post
        # localhost needs no proxy, even when the environment names one
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._wake = threading.Condition()
        self._pending = None
        self._closed = False
        self.in_flight = False
        self.sent = self.failed = self.skipped = 0
        self._thread = threading.Thread(target=self._run, name="RoadSenseLivePublisher", daemon=True)
        self._thread.start()

    def publish(self, payload):
        """Queue payload, replacing a snapshot that hasn't been sent yet."""
        with self._wake:
            if self._pending is not None:
                self.skipped += 1
            self._pending = payload
            self._wake.notify()

    def close(self, timeout_s=1.0):
        """Send the snapshot still waiting, if the server takes it within timeout_s, and stop."""
        with self._wake:
            self._closed = True
            self._wake.notify()
        self._thread.join(timeout_s)

    def _post(self, payload):
        request = urllib.request.Request(self.url, data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
        with self._opener.open(request, timeout=self.timeout_s) as response:
            response.read()

    def _run(self):
        backoff, failing = 0.0, None  # failing: why the last attempt failed, None while the server takes them
        while True:
            with self._wake:
                self._wake.wait_for(lambda: self._pending is not None or self._closed)
                if self._pending is None:
                    return  # closed, nothing left to send
                payload, self._pending = self._pending, None
                self.in_flight = True
            try:
                self._send(payload)
            except Exception as error:  # anything at all: the simulation must not notice
                self.failed += 1
                reason = _describe(error)
                if reason != failing:
                    self._log("[RoadSense:LIVE] cannot reach %s: %s; retrying with backoff, newest snapshot only"
                              % (self.url, reason))
                failing = reason
                backoff = min(self._max_backoff_s, backoff * 2 or self._first_backoff_s)
                with self._wake:
                    self.in_flight = False
                    if self._pending is None:
                        self._pending = payload  # retry this one unless a newer one comes
                    else:
                        self.skipped += 1
                    if self._closed:
                        return  # no retries while stopping
                    self._wake.wait_for(lambda: self._closed, backoff)
                continue
            with self._wake:
                self.in_flight = False
            self.sent += 1
            if failing is not None:
                self._log("[RoadSense:LIVE] publishing live state to %s again (%d attempts failed, %d snapshots "
                          "skipped)" % (self.url, self.failed, self.skipped))
            elif self.sent == 1:
                self._log("[RoadSense:LIVE] publishing live state to %s" % self.url)
            backoff, failing = 0.0, None
