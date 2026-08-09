"""Connection / device panel: pick a port, connect, and watch the raw serial feed."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import ConnectionStatus
from ..serial_client import available_ports

BAUD_RATES = ["9600", "19200", "38400", "57600", "115200"]
DEFAULT_BAUD = "115200"

_STATUS_STYLE = {
    ConnectionStatus.DISCONNECTED: ("#4b5058", "#d7dbe0"),
    ConnectionStatus.CONNECTING: ("#d9a300", "#101317"),
    ConnectionStatus.CONNECTED: ("#1f9d55", "#ffffff"),
    ConnectionStatus.NO_DATA: ("#8a6d1f", "#ffffff"),
    ConnectionStatus.LOST: ("#c0392b", "#ffffff"),
    ConnectionStatus.ERROR: ("#c0392b", "#ffffff"),
}


class ConnectionPanel(QGroupBox):
    connect_requested = Signal(str, int)   # port, baud
    disconnect_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Connection", parent)
        self._connected = False
        self._build()
        self.refresh_ports()
        self.set_status(ConnectionStatus.DISCONNECTED)

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        form = QFormLayout()
        port_row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setToolTip("Serial (COM) ports currently detected on this PC")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Re-scan for serial ports (e.g. after plugging in the Arduino)")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(self.refresh_btn)
        form.addRow("Port", port_row)

        self.baud_combo = QComboBox()
        self.baud_combo.addItems(BAUD_RATES)
        self.baud_combo.setCurrentText(DEFAULT_BAUD)
        self.baud_combo.setToolTip("Must match the Arduino sketch (RoadSense firmware uses 115200)")
        form.addRow("Baud", self.baud_combo)
        outer.addLayout(form)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("Primary")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        outer.addWidget(self.connect_btn)

        self.status_label = QLabel("Disconnected")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setToolTip("Current serial connection state")
        self.status_label.setMinimumHeight(26)
        outer.addWidget(self.status_label)

        info = QFormLayout()
        self.device_label = QLabel("—")
        self.device_label.setToolTip("Connected port and reported firmware version")
        self.last_packet_label = QLabel("—")
        self.last_packet_label.setToolTip("Timestamp of the most recent line received")
        info.addRow("Device", self.device_label)
        info.addRow("Last packet", self.last_packet_label)
        outer.addLayout(info)

        outer.addWidget(QLabel("Raw incoming (last 20 lines)"))
        self.raw_preview = QPlainTextEdit()
        self.raw_preview.setObjectName("RawPreview")
        self.raw_preview.setReadOnly(True)
        self.raw_preview.setMaximumBlockCount(20)  # bounds memory: auto-drops old lines
        self.raw_preview.setMinimumHeight(120)
        self.raw_preview.setToolTip("Exactly what arrives on the wire, newest at the bottom")
        outer.addWidget(self.raw_preview, 1)

    # ---- actions ----
    def refresh_ports(self) -> None:
        previous = self.port_combo.currentData()
        self.port_combo.clear()
        ports = available_ports()
        if not ports:
            self.port_combo.addItem("No serial ports found", None)
            self.port_combo.setEnabled(False)
            return
        self.port_combo.setEnabled(not self._connected)
        for device, description in ports:
            label = f"{device} — {description}" if description else device
            self.port_combo.addItem(label, device)
        if previous is not None:
            idx = self.port_combo.findData(previous)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

    def _on_connect_clicked(self) -> None:
        if self._connected:
            self.disconnect_requested.emit()
            return
        port = self.port_combo.currentData()
        if not port:
            self.set_error("Select a serial port first (Refresh if the list is empty).")
            return
        self.connect_requested.emit(port, int(self.baud_combo.currentText()))

    # ---- updates from the main window ----
    def set_connected(self, connected: bool, device: str = "") -> None:
        self._connected = connected
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.connect_btn.setObjectName("Danger" if connected else "Primary")
        # re-polish so the new objectName's QSS actually takes effect
        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)
        self.port_combo.setEnabled(not connected and self.port_combo.count() > 0)
        self.baud_combo.setEnabled(not connected)
        self.refresh_btn.setEnabled(not connected)
        if connected and device:
            self.device_label.setText(device)
        if not connected:
            self.device_label.setText("—")

    def set_device(self, text: str) -> None:
        self.device_label.setText(text)

    def set_status(self, status: ConnectionStatus, detail: str = "") -> None:
        bg, fg = _STATUS_STYLE.get(status, ("#4b5058", "#d7dbe0"))
        text = status.value if isinstance(status, ConnectionStatus) else str(status)
        if detail:
            text = f"{text} — {detail}"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"background-color:{bg}; color:{fg}; border-radius:4px; font-weight:600;"
        )

    def set_error(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            "background-color:#c0392b; color:#ffffff; border-radius:4px; font-weight:600;"
        )

    def set_last_packet(self, when: datetime) -> None:
        self.last_packet_label.setText(when.strftime("%H:%M:%S"))

    def append_raw(self, line: str) -> None:
        self.raw_preview.appendPlainText(line)

    def clear_raw(self) -> None:
        self.raw_preview.clear()
