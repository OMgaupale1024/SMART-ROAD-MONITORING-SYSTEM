"""Web server reports the package's single authoritative version."""
import pytest

pytest.importorskip("fastapi")  # web extra not installed


def test_api_version_is_package_version(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADSENSE_DATA_DIR", str(tmp_path))  # importing the server opens its database
    import roadsense
    from roadsense.web.server import app

    assert app.version == roadsense.__version__
