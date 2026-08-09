"""Where the app keeps its local data — an OS app-data dir, never beside the source."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "RoadSense"


def app_data_dir() -> Path:
    """Return (creating if needed) the per-user data directory for this app.

    Overridable with ROADSENSE_DATA_DIR (handy for tests and portable installs).
    """
    override = os.environ.get("ROADSENSE_DATA_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def database_path() -> Path:
    return app_data_dir() / "roadsense.db"
