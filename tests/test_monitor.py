from collections.abc import Iterator

import pytest

from shepalert import Alert, Alerter, NotifyError, Sheep
from shepalert.monitor import Monitor

FOREIGN_ALERTS = [
    Alert(source="herdr", subject="pane:0", title="🥔 koda stopped typing", body="trombone 42"),
    Alert(source="tmux", subject="window:3", title="TROMBONE dev/null", body="lorem ipsum dolor"),
    Alert(source="herdr", subject="pane:1", title="koda is typing", body="bröööö p = p + 1"),
]


class FakeSheep(Sheep):
    def __init__(self, alerts: list[Alert] | None = None) -> None:
        self.alerts = FOREIGN_ALERTS if alerts is None else alerts
        self.produced: list[Alert] = []

    def watch(self) -> Iterator[Alert]:
        for alert in self.alerts:
            self.produced.append(alert)
            yield alert


class RecordingAlerter(Alerter):
    def __init__(self) -> None:
        self.received: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        self.received.append(alert)


class FailingAlerter(Alerter):
    def __init__(self) -> None:
        self.received: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        self.received.append(alert)
        raise NotifyError("bark said no")


class PeekingAlerter(Alerter):
    def __init__(self, sheep: FakeSheep) -> None:
        self.sheep = sheep
        self.produced_at_notify: list[int] = []

    def notify(self, alert: Alert) -> None:
        self.produced_at_notify.append(len(self.sheep.produced))


def test_every_alert_reaches_the_alerter_in_order_unmodified() -> None:
    alerter = RecordingAlerter()

    Monitor(FakeSheep(), alerter).run()

    # `Alert` is a frozen dataclass, so `==` is field-by-field; identity proves
    # the objects were not rebuilt on the way through.
    assert alerter.received == FOREIGN_ALERTS
    assert all(received is expected for received, expected in zip(alerter.received, FOREIGN_ALERTS, strict=True))


def test_empty_stream_notifies_nothing_and_returns() -> None:
    alerter = RecordingAlerter()

    assert Monitor(FakeSheep([]), alerter).run() is None  # type: ignore[func-returns-value]
    assert alerter.received == []


def test_failure_does_not_stop_the_loop_or_propagate() -> None:
    alerter = FailingAlerter()

    Monitor(FakeSheep(), alerter).run()

    assert alerter.received == FOREIGN_ALERTS


def test_failure_is_logged_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR", logger="shepalert.monitor"):
        Monitor(FakeSheep(), FailingAlerter()).run()

    failures = [r for r in caplog.records if r.getMessage() == "alert failed"]
    assert len(failures) == len(FOREIGN_ALERTS)
    assert all(r.levelname == "ERROR" and r.exc_info is not None for r in failures)


def test_watch_generator_is_lazy() -> None:
    sheep = FakeSheep()

    stream = sheep.watch()
    assert sheep.produced == []
    assert next(stream) is FOREIGN_ALERTS[0]
    assert sheep.produced == [FOREIGN_ALERTS[0]]


def test_monitor_notifies_each_alert_before_the_next_is_produced() -> None:
    sheep = FakeSheep()
    alerter = PeekingAlerter(sheep)

    Monitor(sheep, alerter).run()

    assert alerter.produced_at_notify == [1, 2, 3]
    assert sheep.produced == FOREIGN_ALERTS


def test_both_abcs_str_as_their_class_name() -> None:
    assert str(FakeSheep()) == "FakeSheep"
    assert str(RecordingAlerter()) == "RecordingAlerter"


@pytest.mark.parametrize("abc", [Sheep, Alerter])
def test_both_abcs_stay_abstract(abc: type) -> None:
    """Guards the shared `__str__` mixin: dropping `ABC` from the bases would
    silently make both instantiable, and nothing else would notice."""
    with pytest.raises(TypeError):
        abc()
