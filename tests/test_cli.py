"""CLI behaviour: argument handling, environment validation, and wiring."""

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import shepalert
from shepalert import Alert, Alerter, Sheep, cli
from shepalert.alerter import BarkAlerter, LogAlerter
from shepalert.herdr import DEFAULT_INTERVAL, HerdrSheep

REPO_ROOT = Path(__file__).resolve().parent.parent


class RecordingMonitor:
    """A `Monitor` stand-in that counts how often the CLI starts it."""

    def __init__(self, sheep: HerdrSheep, alerter: Alerter) -> None:
        self.sheep = sheep
        self.alerter = alerter
        self.runs = 0

    def run(self) -> None:
        self.runs += 1


class Interrupting:
    """A `Monitor` stand-in that interrupts, as Ctrl-C would."""

    def __init__(self, sheep: Sheep, alerter: Alerter) -> None:
        pass

    def run(self) -> None:
        raise KeyboardInterrupt


@pytest.fixture(autouse=True)
def monitors(monkeypatch: pytest.MonkeyPatch) -> list[RecordingMonitor]:
    """Stub `cli.Monitor` for every test in this file.

    Autouse because it is a safety net, not a convenience: without it a test
    that calls `main()` reaches the real `Monitor` and polls forever. Tests
    that want to observe it request the list as well, and a test that needs
    its own behaviour just patches `cli.Monitor` over the top.
    """
    built: list[RecordingMonitor] = []

    def record(sheep: HerdrSheep, alerter: Alerter) -> RecordingMonitor:
        monitor = RecordingMonitor(sheep, alerter)
        built.append(monitor)
        return monitor

    monkeypatch.setattr(cli, "Monitor", record)
    return built


@pytest.fixture(autouse=True)
def no_bark_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BARK_URL", raising=False)
    monkeypatch.delenv("BARK_KEY", raising=False)
    monkeypatch.delenv("SHEPALERT_LOG_LEVEL", raising=False)


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """`main()` configures the root logger and never undoes it.

    Without this the level set by one test leaks into the next, so a case can
    pass or fail depending on where pytest happened to put it.
    """
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        yield
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers


def test_missing_bark_config_exits_2(caplog: pytest.LogCaptureFixture, monitors: list[RecordingMonitor]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2
    assert "BARK_URL" in caplog.text
    assert monitors == []


def test_blank_bark_config_counts_as_missing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("BARK_URL", "   ")
    monkeypatch.setenv("BARK_KEY", "key")
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2
    assert "BARK_URL" in caplog.text


def test_dry_run_needs_no_bark_config(monitors: list[RecordingMonitor]) -> None:
    cli.main(["--dry-run"])
    assert len(monitors) == 1
    assert isinstance(monitors[0].alerter, LogAlerter)
    assert monitors[0].runs == 1


def test_bark_mode_builds_a_bark_alerter(monkeypatch: pytest.MonkeyPatch, monitors: list[RecordingMonitor]) -> None:
    monkeypatch.setenv("BARK_URL", "https://bark.example.com")
    monkeypatch.setenv("BARK_KEY", "device-key")
    cli.main([])
    assert isinstance(monitors[0].alerter, BarkAlerter)


def test_ctrl_c_exits_without_propagating(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("BARK_URL", "https://bark.example.com")
    monkeypatch.setenv("BARK_KEY", "device-key")
    monkeypatch.setattr(cli, "Monitor", Interrupting)
    cli.main([])
    assert "interrupted" in caplog.text


def test_unknown_log_level_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SHEPALERT_LOG_LEVEL", "LOUD")
    cli.main(["--dry-run"])
    assert "unrecognised SHEPALERT_LOG_LEVEL" in caplog.text


def test_known_log_level_is_applied(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("SHEPALERT_LOG_LEVEL", "debug")
    cli.main(["--dry-run"])
    assert "unrecognised" not in caplog.text
    assert logging.getLogger().level == logging.DEBUG


def test_the_alerter_is_closed_when_the_monitor_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Ctrl-C path, and every other exit, releases the client."""

    class ClosingAlerter(Alerter):
        def __init__(self) -> None:
            self.closed = 0

        def notify(self, alert: Alert) -> None:
            pass

        def close(self) -> None:
            self.closed += 1

    alerter = ClosingAlerter()

    monkeypatch.setenv("BARK_URL", "https://bark.example.com")
    monkeypatch.setenv("BARK_KEY", "device-key")
    monkeypatch.setattr(cli, "BarkAlerter", lambda *a, **k: alerter)
    monkeypatch.setattr(cli, "Monitor", Interrupting)
    cli.main([])
    assert alerter.closed == 1


def test_the_alerter_is_closed_on_a_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    alerter = LogAlerter()
    closed: list[int] = []
    monkeypatch.setattr(alerter, "close", lambda: closed.append(1))
    monkeypatch.setattr(cli, "LogAlerter", lambda: alerter)
    cli.main(["--dry-run"])
    assert closed == [1]


def test_the_public_api_is_importable_from_the_package_root() -> None:
    """The README's extension examples import from `shepalert` directly."""
    from shepalert import Alert, Alerter, Monitor, NotifyError, Sheep  # noqa: F401

    assert shepalert.__all__ == ["Alert", "Alerter", "Monitor", "NotifyError", "Sheep"]


def test_python_dash_m_entry_point_works() -> None:
    """The `-m` form, in a real interpreter, so the root logger is unconfigured
    and the missing-env error really does reach stderr."""
    env = {key: value for key, value in os.environ.items() if key not in ("BARK_URL", "BARK_KEY")}
    result = subprocess.run(
        [sys.executable, "-m", "shepalert"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "BARK_URL" in result.stderr


def test_interval_defaults_to_ten_seconds(monitors: list[RecordingMonitor]) -> None:
    cli.main(["--dry-run"])
    assert monitors[0].sheep.interval == DEFAULT_INTERVAL


def test_interval_flag_overrides_the_environment(
    monkeypatch: pytest.MonkeyPatch, monitors: list[RecordingMonitor]
) -> None:
    monkeypatch.setenv("SHEPALERT_INTERVAL", "30")
    cli.main(["--dry-run", "--interval", "2.5"])
    assert monitors[0].sheep.interval == 2.5


def test_interval_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch, monitors: list[RecordingMonitor]) -> None:
    monkeypatch.setenv("SHEPALERT_INTERVAL", "30")
    cli.main(["--dry-run"])
    assert monitors[0].sheep.interval == 30.0


@pytest.mark.parametrize("raw", ["soon", "0", "-5"])
def test_a_bad_interval_env_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    monitors: list[RecordingMonitor],
    raw: str,
) -> None:
    monkeypatch.setenv("SHEPALERT_INTERVAL", raw)
    cli.main(["--dry-run"])
    assert monitors[0].sheep.interval == DEFAULT_INTERVAL
    assert "SHEPALERT_INTERVAL" in caplog.text


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("soon", "not a number"), ("0", "greater than zero"), ("-5", "greater than zero")],
)
def test_a_bad_interval_flag_exits_2(
    capsys: pytest.CaptureFixture[str],
    monitors: list[RecordingMonitor],
    raw: str,
    reason: str,
) -> None:
    """The flag hard-fails where the env var warns: a visible typo should stop."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--dry-run", "--interval", raw])
    assert exit_info.value.code == 2
    assert reason in capsys.readouterr().err
    assert monitors == []


def test_the_interval_flag_ignores_a_bad_environment_value(
    monkeypatch: pytest.MonkeyPatch, monitors: list[RecordingMonitor]
) -> None:
    """An explicit flag wins outright; the env value is never consulted."""
    monkeypatch.setenv("SHEPALERT_INTERVAL", "-5")
    cli.main(["--dry-run", "--interval", "3"])
    assert monitors[0].sheep.interval == 3.0
