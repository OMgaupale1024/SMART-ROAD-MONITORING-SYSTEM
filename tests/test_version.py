"""One project version: pyproject.toml and roadsense.__version__ must agree."""
from pathlib import Path

import pytest

import roadsense

tomllib = pytest.importorskip("tomllib")  # Python 3.11+


def test_pyproject_version_matches_package_version():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert declared == roadsense.__version__
