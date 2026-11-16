from pathlib import Path

import pytest

import browser_smoke


def test_browser_defaults_to_vivaldi_and_supports_visible_testing():
    args = browser_smoke.parse_args([])
    assert args.browser == "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"
    assert not args.headed
    assert browser_smoke.parse_args(["--headed"]).headed
    assert browser_smoke.parse_args(["--browser", "/other/Vivaldi"]).browser == "/other/Vivaldi"


def test_missing_vivaldi_stops_without_launching_or_falling_back(tmp_path, monkeypatch):
    def unexpected_launch(*args, **kwargs):
        pytest.fail("A missing Vivaldi must not launch a server or fallback browser")

    monkeypatch.setattr(browser_smoke.subprocess, "Popen", unexpected_launch)
    args = browser_smoke.parse_args(["--browser", str(tmp_path / "missing"),
                                    "--output", str(tmp_path / "output")])
    with pytest.raises(SystemExit, match="no browser fallback"):
        browser_smoke.run(args)
    assert not Path(args.output).exists()


def test_debugger_initialization_failure_closes_connection(monkeypatch):
    closed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append(exc[0])

        def send(self, message):
            pass

        def recv(self, **kwargs):
            raise TimeoutError("debugger unavailable")

    monkeypatch.setattr(browser_smoke, "connect", lambda *args, **kwargs: Connection())
    with pytest.raises(TimeoutError):
        browser_smoke.Page("ws://test")
    assert closed == [TimeoutError]
