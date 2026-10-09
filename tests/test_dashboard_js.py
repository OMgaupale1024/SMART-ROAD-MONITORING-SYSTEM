"""The autonomy dashboard's JavaScript tests (tests/js/), run with Node's built-in test runner when Node is installed."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def node_version():
    found = re.match(r"v(\d+)\.(\d+)", subprocess.run([NODE, "--version"], capture_output=True, text=True).stdout)
    return tuple(map(int, found.groups())) if found else (0, 0)


# 22.7 runs the browser's ES modules (.js files with import/export, no package.json) without flags
@pytest.mark.skipif(NODE is None or node_version() < (22, 7), reason="needs Node.js 22.7 or later")
def test_autonomy_dashboard_javascript():
    result = subprocess.run([NODE, "--test", "tests/js/"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
