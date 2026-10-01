import logging
from typing import Any

import httpx

from shepalert.core import Alert, Alerter, NotifyError

log = logging.getLogger(__name__)

BARK_TIMEOUT = 10.0


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class BarkAlerter(Alerter):
    """Sends alerts to a Bark server: `BarkAlerter(base_url, device_key, client=None)`."""

    def __init__(
        self,
        base_url: str,
        device_key: str,
        client: httpx.Client | None = None,
        timeout: float = BARK_TIMEOUT,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/push"
        self._device_key = device_key
        self._client = httpx.Client(timeout=timeout) if client is None else client
        # An injected client belongs to whoever passed it in - the tests share
        # one across cases - so only close the one we opened ourselves.
        self._owns_client = client is None

    def notify(self, alert: Alert) -> None:
        payload = {"device_key": self._device_key, "title": alert.title, "body": alert.body}
        try:
            response = self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"bark request failed ({type(exc).__name__}): {exc}") from exc
        data = _payload(response)
        # Bark answers HTTP 200 even when it rejects the push, so the payload code decides.
        if response.status_code == 200 and data.get("code") == 200:
            log.debug("bark delivered %r for %s", alert.title, alert.subject)
            return
        raise NotifyError(
            f"bark push failed (HTTP {response.status_code}, code {data.get('code')!r}): "
            f"{data.get('message') or 'no message from server'}"
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class LogAlerter(Alerter):
    """The `--dry-run` sink: takes no Bark configuration and never fails."""

    def notify(self, alert: Alert) -> None:
        log.info("dry-run alert from %s: %s - %s", alert.subject, alert.title, alert.body)
