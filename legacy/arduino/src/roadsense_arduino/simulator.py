"""Developer Simulation Mode source.

Emits the SAME Qt signals as SerialWorker so the UI wires it identically, but the data
is synthetic. This is OFF by default and only ever runs when the user explicitly enables
it; the UI shows a loud banner while it is active. It is NOT a substitute for real serial.
"""
from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .models import ConnectionStatus, Hello, Packet


class SimulationSource(QObject):
    packet_received = Signal(object)
    hello_received = Signal(object)
    raw_line = Signal(str)
    parse_error = Signal(object)          # present for interface parity; never emitted
    connection_status = Signal(object)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(100)  # 10 Hz, matching the firmware rate
        self._timer.timeout.connect(self._tick)
        self._t0 = 0.0
        self._prev_ay = 0
        self._counter = 0
        self._rng = random.Random(1234)

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._counter = 0
        self._prev_ay = 0
        self.connection_status.emit(ConnectionStatus.CONNECTED)
        self.hello_received.emit(Hello(version="SIM"))
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.connection_status.emit(ConnectionStatus.DISCONNECTED)

    def _tick(self) -> None:
        self._counter += 1
        t_ms = int((time.monotonic() - self._t0) * 1000)
        ay = int(1500 * math.sin(self._counter / 8.0) + self._rng.gauss(0, 250))
        shock = abs(ay - self._prev_ay)
        self._prev_ay = ay

        roll = self._rng.random()
        status = "NORMAL"
        if roll > 0.985:
            status, shock = "POTHOLE", self._rng.randint(15000, 22000)
        elif roll > 0.94:
            status, shock = "SPEED_BREAKER", self._rng.randint(8000, 14000)

        distance = None if self._rng.random() > 0.9 else round(self._rng.uniform(5, 120), 1)
        dist_field = "NA" if distance is None else distance

        self._emit("T", t_ms, ay, shock, distance, dist_field, status)
        if status != "NORMAL":
            self._emit("E", t_ms, ay, shock, distance, dist_field, status)

    def _emit(self, kind, t_ms, ay, shock, distance, dist_field, status) -> None:
        raw = f"{kind},{t_ms},{ay},{shock},{dist_field},{status}"
        pkt = Packet(kind, t_ms, ay, shock, distance, status, raw=raw)
        self.raw_line.emit(raw)
        self.packet_received.emit(pkt)
