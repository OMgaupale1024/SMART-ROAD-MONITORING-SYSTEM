"""Session recorder panel. Recording only controls persistence — live data still shows
on the dashboard when recording is off."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..session_service import default_session_name


class SessionPanel(QGroupBox):
    start_requested = Signal(str, str)   # name, notes
    stop_requested = Signal()
    notes_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Session Recorder", parent)
        self._build()
        self.set_recording(False)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit(default_session_name())
        self.name_edit.setToolTip("Name for this recording; a timestamped default is provided")
        form.addRow("Name", self.name_edit)

        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Notes (road, vehicle, conditions)…")
        self.notes_edit.setMaximumHeight(64)
        self.notes_edit.textChanged.connect(
            lambda: self.notes_changed.emit(self.notes_edit.toPlainText())
        )
        form.addRow("Notes", self.notes_edit)
        outer.addLayout(form)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start Recording")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(
            lambda: self.start_requested.emit(self.name_edit.text().strip(), self.notes_edit.toPlainText())
        )
        self.stop_btn = QPushButton("Stop Recording")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        outer.addLayout(buttons)

        stats = QFormLayout()
        self.start_label = QLabel("—")
        self.end_label = QLabel("—")
        self.points_label = QLabel("0")
        self.events_label = QLabel("0")
        stats.addRow("Started", self.start_label)
        stats.addRow("Ended", self.end_label)
        stats.addRow("Data points", self.points_label)
        stats.addRow("Events", self.events_label)
        outer.addLayout(stats)

        self.storage_label = QLabel("Records are saved locally.")
        self.storage_label.setWordWrap(True)
        self.storage_label.setStyleSheet("color:#7f8794; font-size:11px;")
        outer.addWidget(self.storage_label)

    # ---- updates ----
    def set_recording(self, recording: bool) -> None:
        self.start_btn.setEnabled(not recording)
        self.stop_btn.setEnabled(recording)
        self.name_edit.setEnabled(not recording)

    def on_started(self, name: str, started_at: datetime) -> None:
        self.name_edit.setText(name)
        self.start_label.setText(started_at.strftime("%Y-%m-%d %H:%M:%S"))
        self.end_label.setText("recording…")
        self.set_recording(True)

    def on_stopped(self, ended_at: datetime) -> None:
        self.end_label.setText(ended_at.strftime("%Y-%m-%d %H:%M:%S"))
        self.set_recording(False)
        # reset the name for the next run
        self.name_edit.setText(default_session_name())

    def set_stats(self, data_points: int, events: int) -> None:
        self.points_label.setText(str(data_points))
        self.events_label.setText(str(events))

    def set_storage_path(self, path: str) -> None:
        self.storage_label.setText(f"Records are saved locally to:\n{path}")
