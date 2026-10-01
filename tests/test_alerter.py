import json
import logging
from collections.abc import Callable

import httpx
import pytest

from shepalert import Alert, NotifyError
from shepalert.alerter import BarkAlerter, LogAlerter

DEVICE_KEY = "kA7-secretDeviceKey-do-not-leak"
ALERT = Alert(
    source="herdr",
    subject="wZ:p2X",
    title="agent stopped working",
    body="p2X (opencode) went idle in shepalert",
)

Handler = Callable[[httpx.Request], httpx.Response]


def make_alerter(handler: Handler, base_url: str = "https://bark.example") -> BarkAlerter:
    return BarkAlerter(base_url, DEVICE_KEY, client=httpx.Client(transport=httpx.MockTransport(handler)))


def accept(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "message": "success"})


def respond(code: int, message: str, status: int = 200) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": message})

    return handler


def html_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html>not bark</html>")


def code_as_string(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": "200", "message": "success"})


def raises(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def capturing(handler: Handler = accept) -> tuple[Handler, list[httpx.Request]]:
    """Wrap `handler` so the requests it saw are collected alongside it."""
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return capture, seen


def test_request_is_a_bare_three_field_push() -> None:
    capture, seen = capturing()

    make_alerter(capture).notify(ALERT)

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/push"
    assert request.headers["content-type"] == "application/json"
    # The exact dict is the whole claim: no `group`, `sound`, `icon`, `level`
    # or `url` rides along, because no key beyond these three is in it.
    assert json.loads(request.content) == {"device_key": DEVICE_KEY, "title": ALERT.title, "body": ALERT.body}


def test_success_returns_none() -> None:
    # The `-> None` return already guarantees this statically; the assertion is
    # the runtime half of that contract, so the ignore is deliberate.
    assert make_alerter(accept).notify(ALERT) is None  # type: ignore[func-returns-value]


def test_payload_level_code_failure_raises_with_server_message() -> None:
    with pytest.raises(NotifyError) as excinfo:
        make_alerter(respond(400, "device_key invalid")).notify(ALERT)
    assert "device_key invalid" in str(excinfo.value)


def test_http_500_raises() -> None:
    with pytest.raises(NotifyError, match="500"):
        make_alerter(respond(500, "internal server error", status=500)).notify(ALERT)


def test_non_json_response_raises() -> None:
    with pytest.raises(NotifyError):
        make_alerter(html_error).notify(ALERT)


def test_transport_error_becomes_notify_error() -> None:
    with pytest.raises(NotifyError, match="connection refused"):
        make_alerter(raises(httpx.ConnectError("connection refused"))).notify(ALERT)


def test_timeout_becomes_notify_error() -> None:
    with pytest.raises(NotifyError, match="timed out"):
        make_alerter(raises(httpx.ReadTimeout("timed out"))).notify(ALERT)


@pytest.mark.parametrize(
    "handler",
    [
        respond(400, "device_key invalid"),
        respond(401, "unauthorized", status=401),
        html_error,
        code_as_string,
        raises(httpx.ConnectError("connection refused")),
    ],
)
def test_device_key_never_leaks(handler: Handler, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(NotifyError) as excinfo:
        make_alerter(handler).notify(ALERT)
    assert DEVICE_KEY not in str(excinfo.value)
    assert DEVICE_KEY not in repr(excinfo.value)
    assert DEVICE_KEY not in caplog.text


@pytest.mark.parametrize("base_url", ["https://bark.example", "https://bark.example/", "https://bark.example///"])
def test_trailing_slashes_never_double_up(base_url: str) -> None:
    capture, seen = capturing()

    make_alerter(capture, base_url=base_url).notify(ALERT)

    assert str(seen[0].url) == "https://bark.example/push"


def test_str_is_the_class_name() -> None:
    alerter = BarkAlerter("https://bark.example/", DEVICE_KEY)
    try:
        assert str(alerter) == "BarkAlerter"
    finally:
        alerter.close()


def test_close_shuts_a_client_the_alerter_owns() -> None:
    # `_client` is reached into deliberately: the client is opened in `__init__`
    # and never exposed, so ownership is only observable from the inside.
    alerter = BarkAlerter("https://bark.example", DEVICE_KEY)
    assert not alerter._client.is_closed
    alerter.close()
    assert alerter._client.is_closed


def test_close_leaves_an_injected_client_open() -> None:
    """The tests share clients; closing one on the alerter's behalf would
    break every case that came after it."""
    client = httpx.Client(transport=httpx.MockTransport(accept))
    BarkAlerter("https://bark.example", DEVICE_KEY, client=client).close()
    assert not client.is_closed
    client.close()


def test_the_log_alerter_needs_no_close() -> None:
    # The base `close()` is a no-op, so this is the whole contract: an alerter
    # holding no resources inherits it and needs no override.
    assert LogAlerter().close() is None  # type: ignore[func-returns-value]


def test_log_alerter_logs_and_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        assert LogAlerter().notify(ALERT) is None  # type: ignore[func-returns-value]
    assert ALERT.title in caplog.text
    assert ALERT.body in caplog.text
    assert caplog.records[-1].levelno >= logging.INFO
