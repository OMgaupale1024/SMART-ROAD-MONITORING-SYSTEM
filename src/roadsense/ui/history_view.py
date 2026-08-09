"""Event history: filter, sort, inspect, and delete stored events (one at a time, confirmed)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import Event, RoadStatus, distance_str
from ..repositories import EventRepository, SessionRepository

COLUMNS = ["Session", "Timestamp", "Type", "AY", "Shock", "Distance", "Arduino ms", "Notes", "Validity"]


class HistoryView(QWidget):
    export_requested = Signal(int)  # session_id

    def __init__(self, sessions: SessionRepository, events: EventRepository, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.sessions = sessions
        self.events = events
        self._build()
        self.reload()

    def _build(self) -> None:
        root = QVBoxLayout(self)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Session"))
        self.session_filter = QComboBox()
        self.session_filter.setMinimumWidth(220)
        self.session_filter.currentIndexChanged.connect(self.refresh_events)
        filters.addWidget(self.session_filter)

        filters.addWidget(QLabel("Type"))
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", None)
        for status in RoadStatus:
            self.type_filter.addItem(status.value, status.value)
        self.type_filter.currentIndexChanged.connect(self.refresh_events)
        filters.addWidget(self.type_filter)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.reload)
        filters.addWidget(self.refresh_btn)

        self.export_btn = QPushButton("Export Session…")
        self.export_btn.setToolTip("Export the selected session to CSV (choose a session first)")
        self.export_btn.clicked.connect(self._on_export)
        filters.addWidget(self.export_btn)

        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.setObjectName("Danger")
        self.delete_btn.clicked.connect(self._on_delete)
        filters.addWidget(self.delete_btn)
        filters.addStretch(1)
        root.addLayout(filters)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_detail)
        root.addWidget(self.table, 1)

        self.detail = QLabel("Select an event to see its details.")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setStyleSheet("background:#0f1216; border:1px solid #2a2f37; border-radius:4px; padding:8px;")
        self.detail.setMinimumHeight(72)
        root.addWidget(self.detail)

    # ---- data ----
    def reload(self) -> None:
        """Reload the session filter list (preserving selection) and the table."""
        current = self.session_filter.currentData()
        self.session_filter.blockSignals(True)
        self.session_filter.clear()
        self.session_filter.addItem("All sessions", None)
        for session in self.sessions.list():
            self.session_filter.addItem(f"{session.name}  (#{session.id})", session.id)
        idx = self.session_filter.findData(current)
        self.session_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.session_filter.blockSignals(False)
        self.refresh_events()

    def refresh_events(self) -> None:
        session_id = self.session_filter.currentData()
        status = self.type_filter.currentData()
        rows = self.events.list(session_id=session_id, status=status)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for event in rows:
            self._append_row(event)
        self.table.setSortingEnabled(True)
        self.export_btn.setEnabled(session_id is not None)
        self._update_detail()

    def _append_row(self, event: Event) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        text_cells = {
            0: event.session_name,
            1: event.received_at.isoformat(sep=" ", timespec="seconds"),
            2: event.status,
            5: distance_str(event.distance_cm),
            7: event.notes,
            8: "valid",  # stored events parsed successfully by definition
        }
        for col, text in text_cells.items():
            self.table.setItem(r, col, QTableWidgetItem(text))
        for col, number in ((3, event.ay), (4, event.shock), (6, event.arduino_time_ms)):
            item = QTableWidgetItem()
            item.setData(Qt.DisplayRole, int(number))  # numeric so the column sorts by value
            self.table.setItem(r, col, item)
        # stash the event id on the row's first cell
        self.table.item(r, 0).setData(Qt.UserRole, event.id)

    def _selected_event_id(self) -> Optional[int]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole)

    def _update_detail(self) -> None:
        event_id = self._selected_event_id()
        if event_id is None:
            self.detail.setText("Select an event to see its details.")
            return
        e = self.events.get(event_id)
        if e is None:
            self.detail.setText("Event no longer exists.")
            return
        self.detail.setText(
            f"<b>{e.status}</b> in session <b>{e.session_name}</b> (#{e.session_id})<br>"
            f"Received: {e.received_at.isoformat(sep=' ', timespec='seconds')} &nbsp;·&nbsp; "
            f"Arduino time: {e.arduino_time_ms} ms<br>"
            f"AY: {e.ay} &nbsp;·&nbsp; Shock: {e.shock} &nbsp;·&nbsp; Distance: {distance_str(e.distance_cm)}<br>"
            f"Validity: valid (parsed 'E' message) &nbsp;·&nbsp; Notes: {e.notes or '—'}"
        )

    def _on_delete(self) -> None:
        event_id = self._selected_event_id()
        if event_id is None:
            QMessageBox.information(self, "Delete event", "Select an event to delete.")
            return
        reply = QMessageBox.question(
            self,
            "Delete event",
            f"Delete event #{event_id}? This removes only this one event and cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.events.delete(event_id)
            self.refresh_events()

    def _on_export(self) -> None:
        session_id = self.session_filter.currentData()
        if session_id is None:
            QMessageBox.information(self, "Export", "Choose a specific session to export.")
            return
        self.export_requested.emit(session_id)
