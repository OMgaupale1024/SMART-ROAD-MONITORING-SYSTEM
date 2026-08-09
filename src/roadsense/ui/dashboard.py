"""Live dashboard: big status panel, metric tiles, and rolling shock/distance plots.

Plot history is bounded by both a time window and deque maxlen, so a long session cannot
grow memory without limit.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..models import Packet, distance_str
from . import styles

pg.setConfigOption("background", styles.PLOT_BACKGROUND)
pg.setConfigOption("foreground", styles.PLOT_FOREGROUND)
pg.setConfigOptions(antialias=True)

WINDOW_SECONDS = 60          # rolling view width
_MAXLEN = 4000               # hard cap on retained points per curve (~6.6 min at 10 Hz)


class _Tile(QFrame):
    """A small labelled metric readout."""

    def __init__(self, title: str, tooltip: str = ""):
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(2)
        cap = QLabel(title)
        cap.setObjectName("Metric")
        self.value = QLabel("—")
        self.value.setObjectName("MetricValue")
        lay.addWidget(cap)
        lay.addWidget(self.value)
        if tooltip:
            self.setToolTip(tooltip)


class Dashboard(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._t0 = time.monotonic()
        self._shock_t: deque = deque(maxlen=_MAXLEN)
        self._shock_v: deque = deque(maxlen=_MAXLEN)
        self._dist_t: deque = deque(maxlen=_MAXLEN)
        self._dist_v: deque = deque(maxlen=_MAXLEN)
        self._status_key = "STALE"
        self._build()
        self._apply_status("STALE")

    def _build(self) -> None:
        root = QVBoxLayout(self)

        self.alert_banner = QLabel("")
        self.alert_banner.setObjectName("Banner")
        self.alert_banner.setAlignment(Qt.AlignCenter)
        self.alert_banner.setVisible(False)
        root.addWidget(self.alert_banner)

        self.status_panel = QLabel("NO DATA")
        self.status_panel.setAlignment(Qt.AlignCenter)
        self.status_panel.setMinimumHeight(96)
        f = self.status_panel.font()
        f.setPointSize(30)
        f.setBold(True)
        self.status_panel.setFont(f)
        self.status_panel.setToolTip("Current road condition, classified on the Arduino")
        root.addWidget(self.status_panel)

        # metric tiles
        grid = QGridLayout()
        grid.setSpacing(8)
        self.tiles = {
            "ay": _Tile("AY (raw accel Y)", "Raw MPU6050 Y-axis acceleration register value"),
            "shock": _Tile("Shock", "abs(AY - previous AY), the Arduino's jolt measure"),
            "distance": _Tile("Road distance", "HC-SR04 distance in cm, or NA when unavailable"),
            "packets": _Tile("Packets received", "Valid telemetry packets parsed this connection"),
            "invalid": _Tile("Invalid ignored", "Malformed lines rejected by the parser"),
            "last_valid": _Tile("Last valid data", "Time of the most recent valid telemetry"),
            "recording": _Tile("Recording", "Whether telemetry is being saved to the database"),
            "duration": _Tile("Session duration", "Elapsed time of the active recording session"),
        }
        order = ["ay", "shock", "distance", "packets", "invalid", "last_valid", "recording", "duration"]
        for i, key in enumerate(order):
            grid.addWidget(self.tiles[key], i // 4, i % 4)
        root.addLayout(grid)

        self.last_event_tile = _Tile("Last detected road event", "Most recent 'E' message from the Arduino")
        self.last_event_tile.value.setText("None yet")
        root.addWidget(self.last_event_tile)

        # plots
        plots = QHBoxLayout()
        self.shock_plot, shock_box = self._make_plot("Shock over time", "shock")
        self.shock_curve = self.shock_plot.plot(pen=pg.mkPen(styles.SHOCK_CURVE, width=2))
        self.dist_plot, dist_box = self._make_plot("Distance over time", "cm")
        self.dist_curve = self.dist_plot.plot(
            pen=pg.mkPen(styles.DISTANCE_CURVE, width=2), connect="finite"
        )
        plots.addWidget(shock_box)
        plots.addWidget(dist_box)
        root.addLayout(plots, 1)

        # a lightweight ticker for the alert auto-hide
        self._alert_timer = QTimer(self)
        self._alert_timer.setSingleShot(True)
        self._alert_timer.timeout.connect(lambda: self.alert_banner.setVisible(False))

    def _make_plot(self, title: str, y_units: str):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        widget = pg.PlotWidget()
        widget.setMouseEnabled(x=False, y=True)
        widget.showGrid(x=True, y=True, alpha=0.2)
        widget.setLabel("bottom", "seconds")
        widget.setLabel("left", y_units)
        lay.addWidget(widget)
        return widget, box

    # ---- data updates (called on the UI thread from signal handlers) ----
    def update_packet(self, packet: Packet) -> None:
        self._apply_status(packet.status)
        if packet.is_event:
            self.set_last_event(packet)
            return  # 'E' carries the same fields; the 'T' stream drives the tiles/plots
        self.tiles["ay"].value.setText(str(packet.ay))
        self.tiles["shock"].value.setText(str(packet.shock))
        self.tiles["distance"].value.setText(distance_str(packet.distance_cm))
        self.tiles["last_valid"].value.setText(packet.received_at.strftime("%H:%M:%S"))

        t = time.monotonic() - self._t0
        self._shock_t.append(t)
        self._shock_v.append(packet.shock)
        self._dist_t.append(t)
        self._dist_v.append(packet.distance_cm if packet.distance_cm is not None else float("nan"))
        self._trim(t)
        self._redraw(t)

    def _trim(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        for ts, vs in ((self._shock_t, self._shock_v), (self._dist_t, self._dist_v)):
            while ts and ts[0] < cutoff:
                ts.popleft()
                vs.popleft()

    def _redraw(self, now: float) -> None:
        self.shock_curve.setData(list(self._shock_t), list(self._shock_v))
        self.dist_curve.setData(list(self._dist_t), list(self._dist_v))
        left = max(0.0, now - WINDOW_SECONDS)
        self.shock_plot.setXRange(left, max(now, WINDOW_SECONDS), padding=0)
        self.dist_plot.setXRange(left, max(now, WINDOW_SECONDS), padding=0)

    def set_counts(self, valid: int, invalid: int) -> None:
        self.tiles["packets"].value.setText(str(valid))
        self.tiles["invalid"].value.setText(str(invalid))

    def set_recording(self, recording: bool, duration_s: float = 0.0) -> None:
        self.tiles["recording"].value.setText("● REC" if recording else "Idle")
        self.tiles["recording"].value.setStyleSheet("color:#e0554d;" if recording else "")
        if recording:
            m, s = divmod(int(duration_s), 60)
            h, m = divmod(m, 60)
            self.tiles["duration"].value.setText(f"{h:d}:{m:02d}:{s:02d}")
        else:
            self.tiles["duration"].value.setText("—")

    def set_last_event(self, packet: Packet) -> None:
        label = styles.STATUS_LABELS.get(packet.status, packet.status)
        self.last_event_tile.value.setText(
            f"{label}  ·  {packet.received_at.strftime('%H:%M:%S')}  ·  shock {packet.shock}  ·  {distance_str(packet.distance_cm)}"
        )

    def flash_alert(self, text: str, hold_ms: int = 2500) -> None:
        self.alert_banner.setText(text)
        self.alert_banner.setVisible(True)
        self._alert_timer.start(hold_ms)

    def set_stale(self) -> None:
        self._apply_status("STALE")

    def reset(self) -> None:
        for d in (self._shock_t, self._shock_v, self._dist_t, self._dist_v):
            d.clear()
        self._t0 = time.monotonic()
        self.shock_curve.setData([], [])
        self.dist_curve.setData([], [])
        for key in ("ay", "shock", "distance", "last_valid"):
            self.tiles[key].value.setText("—")
        self.set_stale()

    def _apply_status(self, status: str) -> None:
        key = status if status in styles.STATUS_COLORS else "STALE"
        self._status_key = key
        bg = styles.STATUS_COLORS[key]
        fg = styles.STATUS_TEXT_COLORS[key]
        self.status_panel.setText(styles.STATUS_LABELS[key])
        self.status_panel.setStyleSheet(
            f"background-color:{bg}; color:{fg}; border-radius:8px;"
        )
