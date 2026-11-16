"""Neutral dark engineering theme + status colors. Color is always paired with a text
label elsewhere in the UI, never used as the only signal."""
from __future__ import annotations

# Status -> (background color, readable label). STALE covers disconnected/stale data.
STATUS_COLORS = {
    "NORMAL": "#1f9d55",         # green
    "SPEED_BREAKER": "#d9a300",  # amber
    "POTHOLE": "#c0392b",        # red
    "STALE": "#4b5058",          # grey
}
STATUS_TEXT_COLORS = {
    "NORMAL": "#ffffff",
    "SPEED_BREAKER": "#101317",
    "POTHOLE": "#ffffff",
    "STALE": "#d7dbe0",
}
STATUS_LABELS = {
    "NORMAL": "NORMAL",
    "SPEED_BREAKER": "SPEED BREAKER",
    "POTHOLE": "POTHOLE",
    "STALE": "NO DATA",
}

# pyqtgraph colors, kept consistent with the theme.
PLOT_BACKGROUND = "#191c21"
PLOT_FOREGROUND = "#aab0b8"
SHOCK_CURVE = "#4a9df0"
DISTANCE_CURVE = "#e0873a"

DARK_QSS = """
QWidget {
    background-color: #14171b;
    color: #d7dbe0;
    font-family: "Segoe UI", "Inter", Arial, sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog { background-color: #101317; }
QGroupBox {
    border: 1px solid #2a2f37;
    border-radius: 6px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    background-color: #181c21;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: #8b93a0;
    font-weight: 600;
    text-transform: uppercase;
    font-size: 11px;
    letter-spacing: 1px;
}
QLabel#Metric { color: #8b93a0; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
QLabel#MetricValue { color: #eef1f5; font-size: 20px; font-weight: 600; }
QLabel#Banner {
    background-color: #b8531a; color: #ffffff; font-weight: 700;
    padding: 6px; border-radius: 4px; font-size: 13px;
}
QPushButton {
    background-color: #2a2f37; border: 1px solid #363c45; border-radius: 5px;
    padding: 6px 14px; color: #e7eaef;
}
QPushButton:hover { background-color: #333a44; }
QPushButton:pressed { background-color: #232830; }
QPushButton:disabled { color: #6b7280; background-color: #1c2026; }
QPushButton#Primary { background-color: #2f6fed; border-color: #2f6fed; color: #ffffff; font-weight: 600; }
QPushButton#Primary:hover { background-color: #3b7bf5; }
QPushButton#Danger { background-color: #7a2b26; border-color: #92332c; color: #ffdedb; }
QPushButton#Danger:hover { background-color: #8f3128; }
QComboBox, QLineEdit, QPlainTextEdit, QSpinBox {
    background-color: #0f1216; border: 1px solid #2a2f37; border-radius: 4px;
    padding: 4px 6px; color: #e7eaef; selection-background-color: #2f6fed;
}
QComboBox:disabled, QLineEdit:disabled { color: #6b7280; }
QComboBox QAbstractItemView { background-color: #0f1216; selection-background-color: #2f6fed; }
QPlainTextEdit#RawPreview { font-family: "Consolas", "Courier New", monospace; font-size: 12px; color: #9fd0a3; }
QTableWidget, QTableView {
    background-color: #0f1216; alternate-background-color: #14171c;
    gridline-color: #23282f; border: 1px solid #2a2f37;
}
QHeaderView::section {
    background-color: #1c2128; color: #9aa2ad; padding: 5px; border: none;
    border-right: 1px solid #23282f; font-weight: 600;
}
QTableWidget::item:selected, QTableView::item:selected { background-color: #2f6fed; color: #ffffff; }
QTabBar::tab {
    background: #181c21; color: #9aa2ad; padding: 8px 18px; border: 1px solid #2a2f37;
    border-bottom: none; border-top-left-radius: 5px; border-top-right-radius: 5px;
}
QTabBar::tab:selected { background: #14171b; color: #eef1f5; }
QTabWidget::pane { border: 1px solid #2a2f37; top: -1px; }
QStatusBar { background-color: #101317; color: #8b93a0; }
QMenuBar { background-color: #101317; }
QMenuBar::item:selected { background: #2a2f37; }
QMenu { background-color: #181c21; border: 1px solid #2a2f37; }
QMenu::item:selected { background-color: #2f6fed; }
QScrollBar:vertical { background: #14171b; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #2f3640; border-radius: 6px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""
