"""Application entry point."""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .database import Database
from .ui import styles
from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("RoadSense Desktop")
    app.setOrganizationName("RoadSense")
    app.setStyleSheet(styles.DARK_QSS)

    database = Database()  # opens/creates the SQLite file in the OS app-data dir
    window = MainWindow(database)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
