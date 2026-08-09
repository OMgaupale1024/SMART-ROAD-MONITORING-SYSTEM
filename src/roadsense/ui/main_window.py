"""Main window: wires the serial source, services, and screens together.

All serial data arrives via queued Qt signals on the UI thread, so widgets are only ever
touched from the main thread. Two timers drive the UI: a fast one for staleness/duration
and a slower one to flush buffered telemetry to SQLite.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import export_service
from ..database import Database
from ..models import ConnectionStatus, Packet
from ..repositories import EventRepository, SessionRepository, TelemetryRepository
from ..serial_client import SerialWorker
from ..session_service import SessionService
from ..simulator import SimulationSource
from .connection_panel import ConnectionPanel
from .dashboard import Dashboard
from .history_view import HistoryView
from .session_panel import SessionPanel

STALE_SECONDS = 2.0
ALERT_COOLDOWN_S = 3.0


class MainWindow(QMainWindow):
    def __init__(self, database: Database):
        super().__init__()
        self.setWindowTitle("RoadSense Desktop")
        self.resize(1180, 760)

        self.db = database
        self.sessions = SessionRepository(database.conn)
        self.telemetry = TelemetryRepository(database.conn)
        self.events = EventRepository(database.conn)
        self.service = SessionService(self.sessions, self.telemetry, self.events)

        # serial/simulation source state
        self.source = None
        self._is_serial = False
        self._connected = False
        self._sim_mode = False
        self._current_port = ""

        # counters and diagnostics (bounded)
        self.valid_count = 0
        self.invalid_count = 0
        self.diagnostics: deque = deque(maxlen=200)
        self._last_valid_monotonic: Optional[float] = None
        self._stale = False
        self._last_pothole_alert = 0.0
        self._pending_notes: Optional[str] = None
        self._history_dirty = False

        self._build_ui()
        self._build_menu()

        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(250)
        self.ui_timer.timeout.connect(self._ui_tick)
        self.ui_timer.start()

        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(1000)
        self.flush_timer.timeout.connect(self._flush_tick)
        self.flush_timer.start()

        self.session_panel.set_storage_path(self.db.path)
        self.statusBar().showMessage("Ready — select a COM port and connect, or enable Simulation Mode.")

    # ---- construction ----
    def _build_ui(self) -> None:
        self.sim_banner = QLabel("⚠  DEVELOPER SIMULATION MODE — data is synthetic, NOT from real hardware")
        self.sim_banner.setObjectName("Banner")
        self.sim_banner.setAlignment(Qt.AlignCenter)
        self.sim_banner.setStyleSheet("background-color:#b8531a; color:#ffffff; font-weight:700; padding:6px;")
        self.sim_banner.setVisible(False)

        self.connection_panel = ConnectionPanel()
        self.connection_panel.connect_requested.connect(self._on_connect_requested)
        self.connection_panel.disconnect_requested.connect(self._on_disconnect_requested)

        self.session_panel = SessionPanel()
        self.session_panel.start_requested.connect(self._on_start_recording)
        self.session_panel.stop_requested.connect(self._on_stop_recording)
        self.session_panel.notes_changed.connect(self._on_notes_changed)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(self.connection_panel, 1)
        left_lay.addWidget(self.session_panel)

        self.dashboard = Dashboard()
        self.history = HistoryView(self.sessions, self.events)
        self.history.export_requested.connect(self._export_session)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.dashboard, "Live Dashboard")
        self.tabs.addTab(self.history, "Event History")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 840])

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addWidget(self.sim_banner)
        outer.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_menu(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        export_action = QAction("&Export Session…", self)
        export_action.triggered.connect(self._on_export_menu)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = menubar.addMenu("&View")
        diag_action = QAction("Serial &Diagnostics…", self)
        diag_action.triggered.connect(self._show_diagnostics)
        view_menu.addAction(diag_action)

        tools_menu = menubar.addMenu("&Tools")
        self.sim_action = QAction("Developer &Simulation Mode", self)
        self.sim_action.setCheckable(True)
        self.sim_action.setChecked(False)
        self.sim_action.toggled.connect(self._toggle_simulation)
        tools_menu.addAction(self.sim_action)

        help_menu = menubar.addMenu("&Help")
        about_action = QAction("&About RoadSense", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ---- source lifecycle ----
    def _on_connect_requested(self, port: str, baud: int) -> None:
        if self.source is not None:
            return
        self._current_port = port
        self.connection_panel.set_status(ConnectionStatus.CONNECTING)
        worker = SerialWorker(port, baud)
        worker.finished.connect(self._on_serial_finished)
        self._start_source(worker, is_serial=True)
        self.statusBar().showMessage(f"Connecting to {port} at {baud} baud…")

    def _on_disconnect_requested(self) -> None:
        self._stop_source()
        self.statusBar().showMessage("Disconnected.")

    def _start_source(self, source, is_serial: bool) -> None:
        self.source = source
        self._is_serial = is_serial
        source.packet_received.connect(self._on_packet)
        source.hello_received.connect(self._on_hello)
        source.raw_line.connect(self._on_raw_line)
        source.parse_error.connect(self._on_parse_error)
        source.connection_status.connect(self._on_status)
        source.error.connect(self._on_error)
        # fresh connection: reset live view and counters
        self.valid_count = 0
        self.invalid_count = 0
        self.diagnostics.clear()
        self._last_valid_monotonic = None
        self._stale = False
        self.dashboard.reset()
        self.dashboard.set_counts(0, 0)
        self.connection_panel.clear_raw()
        source.start()

    def _stop_source(self) -> None:
        source = self.source
        if source is None:
            return
        self.source = None  # drop first so the finished slot is a no-op for this stop
        if self._is_serial:
            source.stop()
            source.wait(3000)
        else:
            source.stop()
        self._connected = False
        self.connection_panel.set_connected(False)
        self.connection_panel.set_status(ConnectionStatus.DISCONNECTED)
        self.dashboard.set_stale()

    def _on_serial_finished(self) -> None:
        # Fires when the worker thread ends (clean stop or lost device).
        if self.sender() is self.source:
            self.source = None

    # ---- source signal handlers (UI thread) ----
    def _on_packet(self, packet: Packet) -> None:
        self.valid_count += 1
        self._last_valid_monotonic = time.monotonic()
        self.dashboard.update_packet(packet)
        stored_event = self.service.handle_packet(packet)
        if packet.is_event:
            if stored_event is not None:
                self._history_dirty = True
                if self.tabs.currentWidget() is self.history:
                    self.history.refresh_events()
            if packet.status == "POTHOLE" and self.service.is_recording:
                now = time.monotonic()
                if now - self._last_pothole_alert >= ALERT_COOLDOWN_S:
                    self._last_pothole_alert = now
                    self.dashboard.flash_alert("⚠  POTHOLE detected — event recorded")
                    self.statusBar().showMessage("Pothole event recorded.", 4000)

    def _on_hello(self, hello) -> None:
        label = f"{self._current_port} · firmware {hello.version}" if self._current_port else f"firmware {hello.version}"
        self.connection_panel.set_device(label)
        self.statusBar().showMessage(f"Handshake OK — RoadSense firmware {hello.version}.", 4000)

    def _on_raw_line(self, line: str) -> None:
        self.connection_panel.append_raw(line)
        self.connection_panel.set_last_packet(datetime.now())

    def _on_parse_error(self, parse_error) -> None:
        self.invalid_count += 1
        self.diagnostics.append(parse_error)

    def _on_status(self, status: ConnectionStatus) -> None:
        if status == ConnectionStatus.CONNECTED:
            self._connected = True
            device = self.connection_panel.device_label.text()
            self.connection_panel.set_connected(True, device=self._current_port or device)
            self.connection_panel.set_status(ConnectionStatus.CONNECTED)
            self.statusBar().showMessage("Connected — waiting for data…")
        elif status == ConnectionStatus.LOST:
            self._connected = False
            self.connection_panel.set_connected(False)
            self.connection_panel.set_status(ConnectionStatus.LOST)
            self.dashboard.set_stale()
            self.statusBar().showMessage("Connection lost.", 8000)
        elif status == ConnectionStatus.DISCONNECTED:
            self._connected = False
            self.connection_panel.set_connected(False)
            self.connection_panel.set_status(ConnectionStatus.DISCONNECTED)
            self.dashboard.set_stale()
        elif status == ConnectionStatus.ERROR:
            self._connected = False
            self.connection_panel.set_status(ConnectionStatus.ERROR)

    def _on_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)
        self.connection_panel.set_error(message)
        if not self._connected:  # open failure the user must see
            QMessageBox.warning(self, "Serial error", message)

    # ---- timers ----
    def _ui_tick(self) -> None:
        self.dashboard.set_counts(self.valid_count, self.invalid_count)
        if self.service.is_recording and self.service.active is not None:
            duration = (datetime.now() - self.service.active.started_at).total_seconds()
            self.dashboard.set_recording(True, duration)
            self.session_panel.set_stats(self.service.data_point_count, self.service.event_count)
        else:
            self.dashboard.set_recording(False)

        if self._connected:
            now = time.monotonic()
            fresh = self._last_valid_monotonic is not None and (now - self._last_valid_monotonic) <= STALE_SECONDS
            if fresh:
                if self._stale:
                    self._stale = False
                    self.connection_panel.set_status(ConnectionStatus.CONNECTED)
            else:
                self._stale = True
                self.dashboard.set_stale()
                detail = "no data yet" if self._last_valid_monotonic is None else "no data >2s"
                self.connection_panel.set_status(ConnectionStatus.NO_DATA, detail)

    def _flush_tick(self) -> None:
        self.service.flush()
        if self.service.is_recording and self._pending_notes is not None:
            if self._pending_notes != (self.service.active.notes if self.service.active else None):
                self.service.update_notes(self._pending_notes)

    # ---- recording ----
    def _on_start_recording(self, name: str, notes: str) -> None:
        if self.service.is_recording:
            return
        session = self.service.start(name or None, notes)
        self._pending_notes = notes
        self.session_panel.on_started(session.name, session.started_at)
        self.dashboard.set_recording(True, 0.0)
        self._last_pothole_alert = 0.0
        self.statusBar().showMessage(f"Recording “{session.name}” — saving locally.", 4000)

    def _on_stop_recording(self) -> None:
        finished = self.service.stop()
        if finished is None:
            return
        self.session_panel.on_stopped(finished.ended_at or datetime.now())
        self.session_panel.set_stats(self.service.data_point_count, self.service.event_count)
        self.dashboard.set_recording(False)
        self._pending_notes = None
        self.history.reload()
        self.statusBar().showMessage(f"Recording stopped — {finished.name} saved.", 5000)

    def _on_notes_changed(self, notes: str) -> None:
        self._pending_notes = notes

    # ---- export ----
    def _on_export_menu(self) -> None:
        sessions = self.sessions.list()
        if not sessions:
            QMessageBox.information(self, "Export", "No sessions recorded yet.")
            return
        # Prefer whatever the history view has selected; otherwise the most recent session.
        session_id = self.history.session_filter.currentData()
        if session_id is None:
            session_id = sessions[0].id
        self._export_session(session_id)

    def _export_session(self, session_id: int) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            QMessageBox.warning(self, "Export", "That session no longer exists.")
            return
        directory = QFileDialog.getExistingDirectory(self, f"Export “{session.name}” — choose a folder")
        if not directory:
            return
        existing = [p.name for p in export_service.target_files(directory) if Path(p).exists()]
        if existing:
            reply = QMessageBox.question(
                self,
                "Overwrite files?",
                "These files already exist and will be overwritten:\n  " + "\n  ".join(existing),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        try:
            written = export_service.export_session(
                self.sessions, self.telemetry, self.events, session_id, directory
            )
        except Exception as exc:  # surface any IO/permission problem instead of failing silently
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self,
            "Export complete",
            "Wrote:\n  " + "\n  ".join(p.name for p in written) + f"\n\nto {directory}",
        )
        self.statusBar().showMessage(f"Exported session “{session.name}” to {directory}", 6000)

    # ---- simulation ----
    def _toggle_simulation(self, checked: bool) -> None:
        if checked:
            if self.source is not None and self._is_serial:
                QMessageBox.information(
                    self, "Simulation Mode",
                    "Simulation uses synthetic data; the serial connection will be closed.",
                )
                self._stop_source()
            self._sim_mode = True
            self.sim_banner.setVisible(True)
            self.connection_panel.setEnabled(False)
            sim = SimulationSource(self)
            self._start_source(sim, is_serial=False)
            self._connected = True
            self.statusBar().showMessage("SIMULATION MODE active — synthetic data.")
        else:
            self._sim_mode = False
            self.sim_banner.setVisible(False)
            self._stop_source()
            self.connection_panel.setEnabled(True)
            self.statusBar().showMessage("Simulation Mode off.")

    # ---- misc dialogs ----
    def _on_tab_changed(self, _index: int) -> None:
        if self.tabs.currentWidget() is self.history and self._history_dirty:
            self.history.reload()
            self._history_dirty = False

    def _show_diagnostics(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Serial Diagnostics — rejected lines")
        dialog.resize(640, 380)
        lay = QVBoxLayout(dialog)
        lay.addWidget(QLabel(f"Rejected lines kept in memory (max {self.diagnostics.maxlen}): {len(self.diagnostics)}"))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        if self.diagnostics:
            text.setPlainText(
                "\n".join(
                    f"{d.received_at.strftime('%H:%M:%S')}  {d.error}  ||  {d.raw!r}"
                    for d in self.diagnostics
                )
            )
        else:
            text.setPlainText("No malformed lines received.")
        lay.addWidget(text)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        lay.addWidget(close)
        dialog.exec()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About RoadSense Desktop",
            "RoadSense Desktop\n\n"
            "USB-serial road-condition monitor for the car-in-a-box prototype.\n"
            "Connects to an Arduino UNO, shows live road status, records sessions, and exports CSV.\n\n"
            "Local-only: no cloud, no GPS, no wireless.",
        )

    # ---- shutdown ----
    def closeEvent(self, event) -> None:
        if self.service.is_recording:
            self.service.stop()  # flush + end so nothing recorded is lost
        self._stop_source()
        self.db.close()
        super().closeEvent(event)
