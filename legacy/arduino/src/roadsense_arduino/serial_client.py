"""Background serial reader. Runs in its own QThread and only ever talks to the UI
through Qt signals — it never touches a widget or the database directly.

Serial input is treated as unreliable: partial lines are buffered, malformed lines are
reported (not raised into the UI), and a lost device breaks the loop cleanly.
"""
from __future__ import annotations

import threading
from typing import List, Tuple

import serial
import serial.tools.list_ports
from PySide6.QtCore import QThread, Signal

from .models import ConnectionStatus, Hello, ParseError
from .protocol import MalformedMessage, parse_line


def available_ports() -> List[Tuple[str, str]]:
    """(device, description) for every serial port currently present."""
    ports = [(p.device, p.description or "") for p in serial.tools.list_ports.comports()]
    return sorted(ports)


class SerialWorker(QThread):
    packet_received = Signal(object)      # models.Packet ('T' or 'E')
    hello_received = Signal(object)       # models.Hello
    raw_line = Signal(str)                # every non-empty raw line, for the preview
    parse_error = Signal(object)          # models.ParseError
    connection_status = Signal(object)    # models.ConnectionStatus
    error = Signal(str)                   # human-readable error text

    def __init__(self, port: str, baud: int = 115200, parent=None):
        super().__init__(parent)
        self._port = port
        self._baud = baud
        self._stop = threading.Event()

    def stop(self) -> None:
        """Ask the read loop to exit. Safe to call from the UI thread."""
        self._stop.set()

    def run(self) -> None:
        try:
            ser = serial.Serial(self._port, self._baud, timeout=0.2)
        except (serial.SerialException, OSError, ValueError) as exc:
            self.error.emit(self._open_error(exc))
            self.connection_status.emit(ConnectionStatus.ERROR)
            return

        self.connection_status.emit(ConnectionStatus.CONNECTED)
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    waiting = ser.in_waiting
                    chunk = ser.read(waiting if waiting else 1)
                except (serial.SerialException, OSError) as exc:
                    self.error.emit(f"Serial connection lost: {exc}")
                    self.connection_status.emit(ConnectionStatus.LOST)
                    break
                if not chunk:
                    continue  # timeout with no data — loop and re-check the stop flag
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    self._handle_line(line)
        finally:
            try:
                ser.close()
            except Exception:
                pass
            if self._stop.is_set():
                self.connection_status.emit(ConnectionStatus.DISCONNECTED)

    def _handle_line(self, line_bytes: bytes) -> None:
        text = line_bytes.decode("utf-8", errors="replace").strip()
        if not text:
            return
        self.raw_line.emit(text)
        try:
            result = parse_line(text)
        except MalformedMessage as exc:
            self.parse_error.emit(ParseError(raw=exc.raw, error=exc.reason))
            return
        if result is None:
            return
        if isinstance(result, Hello):
            self.hello_received.emit(result)
        else:
            self.packet_received.emit(result)

    @staticmethod
    def _open_error(exc: Exception) -> str:
        msg = str(exc)
        low = msg.lower()
        if "denied" in low or "permission" in low:
            return f"Access denied — is the port already open in another program (Serial Monitor, IDE)? [{msg}]"
        if "could not open" in low or "no such" in low or "filenotfound" in low:
            return f"Port unavailable — was the Arduino unplugged, or is the COM name wrong? [{msg}]"
        return f"Could not open serial port: {msg}"
