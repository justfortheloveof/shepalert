"""Join a Sheep to an Alerter, one alert at a time."""

import logging

from shepalert.core import Alerter, NotifyError, Sheep

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, sheep: Sheep, alerter: Alerter) -> None:
        self.sheep = sheep
        self.alerter = alerter

    def run(self) -> None:
        for alert in self.sheep.watch():
            try:
                self.alerter.notify(alert)
            except NotifyError:
                log.exception("alert failed")
