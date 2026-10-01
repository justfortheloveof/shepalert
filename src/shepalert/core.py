"""The contract every sheep and alerter is written against."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Alert:
    source: str
    subject: str
    title: str
    body: str


class NotifyError(Exception):
    pass


class _Named:
    """Mixin: logs read better naming the concrete class than the ABC it implements."""

    def __str__(self) -> str:
        return type(self).__name__


class Sheep(_Named, ABC):
    @abstractmethod
    def watch(self) -> Iterator[Alert]:
        """Yield an Alert every time something is worth reporting."""


class Alerter(_Named, ABC):
    @abstractmethod
    def notify(self, alert: Alert) -> None:
        """Deliver the alert or raise NotifyError."""

    def close(self) -> None:
        """Release anything `notify` opened. Called once, after the last alert.

        The default does nothing, so an alerter that holds no resources - a log,
        a stateless webhook - needs no override. One that opens a client or a
        socket should close it here; the CLI calls this on the way out, including
        after a Ctrl-C, so nothing is left to the garbage collector.
        """
