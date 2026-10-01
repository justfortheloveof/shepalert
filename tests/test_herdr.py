import contextlib
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from itertools import islice
from pathlib import Path
from typing import Any

import pytest

from shepalert import Alert
from shepalert import herdr as herdr_mod
from shepalert.herdr import (
    BACKOFF_MAX,
    BACKOFF_START,
    DEFAULT_INTERVAL,
    HerdrError,
    HerdrSheep,
    HerdrSocket,
    build_alert,
    default_socket_path,
    is_alertable,
)

# Shaped like a real `herdr agent list` record, with the paths and titles fictionalised.
AGENT: dict[str, Any] = {
    "agent": "opencode",
    "agent_status": "idle",
    "cwd": "/home/dev/repos/acme/bar-plugin",
    "pane_id": "wZ:p3E",
    "revision": 7,
    "state_change_seq": 2497,
    "tab_id": "wZ:t1V",
    "terminal_id": "term_65c7f6749352e83",
    "terminal_title": "OC | Working on the bar",
    "terminal_title_stripped": "OC | Working on the bar",
    "workspace_id": "wZ",
}
TERMINAL = AGENT["terminal_id"]

PONG = json.dumps({"id": "req1", "result": {"type": "pong"}})


def agent(**changes: Any) -> dict[str, Any]:
    return {**AGENT, **changes}


class StubSocket(HerdrSocket):
    """A `HerdrSocket` that hands back a fixed socket instead of connecting.

    Overriding `_connect` is preferable to assigning a lambda onto the instance,
    which is a `method-assign` type error and trips ruff's B010.
    """

    def __init__(self, sock: Any) -> None:
        super().__init__("fake-path")
        self._sock = sock

    @contextlib.contextmanager
    def _connect(self) -> Iterator[Any]:
        yield self._sock


def read_frame(conn: socket.socket, buffer: bytes = b"") -> tuple[dict[str, Any], bytes] | None:
    """Read one newline-delimited JSON frame, returning it and the leftover.

    herdr is free to split a frame across writes, so the buffer threads
    through every call. `None` means the peer closed; whether that is a test
    failure or simply the end of this connection's work is the caller's call,
    since the client closes after every single request.
    """
    while b"\n" not in buffer:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buffer += chunk
    line, buffer = buffer.split(b"\n", 1)
    return json.loads(line), buffer


class FakeServer(threading.Thread):
    """Scripted herdr server on one end of a socketpair."""

    def __init__(self, script: Callable[["FakeServer"], None]) -> None:
        super().__init__(daemon=True)
        self._script = script
        self.client, self.server = socket.socketpair()
        self.requests: list[dict[str, Any]] = []
        self.crashed: BaseException | None = None
        self.start()

    def run(self) -> None:
        try:
            self._script(self)
        except BaseException as exc:  # surfaced to the test as .crashed
            self.crashed = exc
        finally:
            self.server.close()

    def recv_request(self) -> dict[str, Any]:
        frame = read_frame(self.server)
        if frame is None:
            raise AssertionError("client hung up without sending a request")
        request, _buffer = frame
        self.requests.append(request)
        return request

    def send(self, *payloads: dict[str, Any]) -> None:
        for payload in payloads:
            self.server.sendall(json.dumps(payload).encode() + b"\n")

    @property
    def socket(self) -> HerdrSocket:
        return StubSocket(self.client)

    def __enter__(self) -> "FakeServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()
        self.join(timeout=2)


@contextlib.contextmanager
def unix_server(tmp_path: Path, serve: Callable[[socket.socket], None]) -> Iterator[str]:
    """A real Unix socket server, so the production connect path runs.

    Polling opens a fresh connection per request, so this accepts repeatedly
    rather than once.
    """
    path = str(tmp_path / "herdr.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(4)

    def run() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                serve(conn)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        listener.close()
        thread.join(timeout=2)


class ChunkSocket:
    """A socket stand-in returning a scripted sequence of recv() results.

    An `Exception` in the script is raised instead, which is how the peer-going
    -away paths are reached: `OSError` is a mid-poll `disconnected`, and a bare
    `b""` is the clean EOF a real `recv` returns when the peer closes.
    """

    def __init__(self, *chunks: bytes | Exception) -> None:
        self.chunks = list(chunks)

    def sendall(self, data: bytes) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        if not self.chunks:
            raise ConnectionResetError("fake eof")
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        return chunk


def patched(*chunks: bytes | Exception) -> HerdrSocket:
    return StubSocket(ChunkSocket(*chunks))


# --- protocol client -------------------------------------------------------


def test_request_round_trip_matches_its_own_id() -> None:
    def script(server: FakeServer) -> None:
        server.recv_request()
        server.send({"id": "other", "event": "noise"})
        server.send({"id": server.requests[0]["id"], "result": {"type": "pong"}})

    with FakeServer(script) as server:
        assert server.socket.request("ping") == {"type": "pong"}
        assert server.crashed is None


def test_each_request_uses_a_fresh_connection(tmp_path: Path) -> None:
    """One request per connection: the server hangs up, so a second request must
    not try to reuse the first socket."""
    accepted = 0

    def serve(conn: socket.socket) -> None:
        nonlocal accepted
        accepted += 1
        frame = read_frame(conn)
        assert frame is not None
        request, _buffer = frame
        conn.sendall(json.dumps({"id": request["id"], "result": {"ok": True}}).encode() + b"\n")

    with unix_server(tmp_path, serve) as path:
        sock = HerdrSocket(path)
        assert sock.request("ping") == {"ok": True}
        assert sock.request("agent.list") == {"ok": True}

    assert accepted == 2


def test_message_split_across_two_recvs() -> None:
    line = PONG + "\n"
    assert patched(line[:10].encode(), line[10:].encode()).request("ping") == {"type": "pong"}


def test_blank_lines_in_the_stream_are_ignored() -> None:
    assert patched(f"\n\n{PONG}\n\n".encode()).request("ping") == {"type": "pong"}


@pytest.mark.parametrize(
    ("chunks", "code", "message"),
    [
        ((b"not json\n",), "bad_json", "not json"),
        ((b"[1,2,3]\n",), "bad_json", "expected an object, got list"),
        ((b'{"id":"req1","error":{"code":"nope","message":"no"}}\n',), "nope", "no"),
        ((b'{"id":"req1"}\n',), "bad_response", "no result object"),
    ],
)
def test_every_failure_becomes_a_herdr_error(chunks: tuple[bytes, ...], code: str, message: str) -> None:
    with pytest.raises(HerdrError) as excinfo:
        patched(*chunks).request("ping")
    assert excinfo.value.code == code
    assert message in excinfo.value.message


def test_connect_failure_becomes_herdr_error() -> None:
    with pytest.raises(HerdrError) as excinfo:
        HerdrSocket("/nonexistent/herdr.sock", timeout=0.5).request("ping")
    assert excinfo.value.code == "connect_failed"


def test_request_still_honours_its_timeout(tmp_path: Path) -> None:
    def serve(conn: socket.socket) -> None:
        conn.recv(4096)
        # Stall without `time.sleep`, which this module's `sleeps` fixture
        # neutralises: the point is to send no reply, not to pass 0.3s.
        threading.Event().wait(0.3)

    with unix_server(tmp_path, serve) as path:
        with pytest.raises(HerdrError) as excinfo:
            HerdrSocket(path, timeout=0.05).request("agent.list")
        assert excinfo.value.code == "timeout"


def test_send_failure_becomes_herdr_error() -> None:
    class BrokenSendSocket(ChunkSocket):
        def sendall(self, data: bytes) -> None:
            raise BrokenPipeError("server hung up")

    sock = StubSocket(BrokenSendSocket())
    with pytest.raises(HerdrError) as excinfo:
        sock.request("ping")
    assert excinfo.value.code == "send_failed"


def test_the_peer_going_away_mid_frame_becomes_an_eof() -> None:
    """A clean `recv` returning nothing is the peer closing, not an error, and
    a partial line is worth saying so about."""
    with pytest.raises(HerdrError) as excinfo:
        patched(PONG[:10].encode(), b"").request("ping")
    assert excinfo.value.code == "eof"
    assert "mid-frame" in excinfo.value.message

    with pytest.raises(HerdrError) as excinfo:
        patched(b"").request("ping")
    assert excinfo.value.code == "eof"
    assert excinfo.value.message == "closed"


def test_a_dropped_connection_becomes_a_herdr_error() -> None:
    """A reset or broken pipe is a disconnect, not a clean end of stream."""
    with pytest.raises(HerdrError) as excinfo:
        patched(ConnectionResetError("connection reset by peer")).request("ping")
    assert excinfo.value.code == "disconnected"


def test_default_socket_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/env.sock")
    assert default_socket_path() == "/tmp/env.sock"
    assert HerdrSocket().path == "/tmp/env.sock"
    monkeypatch.delenv("HERDR_SOCKET_PATH")
    assert default_socket_path().endswith("/.config/herdr/herdr.sock")
    assert HerdrSocket("/explicit.sock").path == "/explicit.sock"


# --- wording ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [("idle", True), ("blocked", True), ("done", True), ("unknown", True), ("working", False)],
)
def test_is_alertable(status: str, expected: bool) -> None:
    assert is_alertable(status) is expected


def test_an_unknown_status_is_not_alertable() -> None:
    """`None` means the record carried no status at all, which is not a change."""
    assert is_alertable(None) is False


def test_build_alert_wording() -> None:
    alert = build_alert(AGENT, "idle")
    assert alert.source == "herdr"
    assert alert.subject == TERMINAL
    assert alert.title == "ShepAlert: opencode idle"
    # `host` is a literal, not `herdr_mod.hostname()`, which would assert the
    # function against itself and pass whatever it returned.
    assert alert.body.splitlines() == ["host: test-host", "dir: bar-plugin"]
    # A cwd with no basename, and no cwd at all, both name the project unknown
    # rather than name the wrong one; a missing agent falls back the same way.
    assert build_alert({"agent": "claude", "cwd": "/"}, "done").body.splitlines()[-1] == "dir: unknown"
    assert build_alert({"cwd": "/home/dev/user/"}, "done").body.splitlines()[-1] == "dir: user"
    assert build_alert({}, "done").title == "ShepAlert: unknown done"
    assert build_alert({}, "done").body == "host: test-host\ndir: unknown"


@pytest.fixture(autouse=True)
def pinned_hostname(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the cached hostname for the whole module.

    `herdr.hostname()` is `functools.cache`d, so one test calling it would
    otherwise decide the value every other test sees.
    """
    herdr_mod.hostname.cache_clear()
    monkeypatch.setattr(socket, "gethostname", lambda: "test-host")
    try:
        yield
    finally:
        herdr_mod.hostname.cache_clear()


# --- the sheep -------------------------------------------------------------


class StopWatching(Exception):
    """Raised by the fake socket once it has no snapshots left."""


class FakeSocket:
    """Serves one outcome per `agent.list` call, then stops the sheep.

    An outcome is either a list of agent records or an exception to raise.
    """

    def __init__(self, outcomes: list[list[dict[str, Any]] | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[str] = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append(method)
        if not self.outcomes:
            raise StopWatching
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"type": "agent_list", "agents": outcome}


DOWN = HerdrError("connect_failed", "refused")


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every test in this module needs `time.sleep` neutralised, or a poll
    really sleeps for its interval. Autouse, so that precondition is visible
    from the test list; the handful that assert on the values request it.
    """
    recorded: list[float] = []
    # Patched on the stdlib module rather than via `herdr_mod.time`, which herdr
    # does not re-export. The sheep looks `time.sleep` up at call time, so this
    # reaches it, and monkeypatch restores it afterwards.
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


def sheep_over(monkeypatch: pytest.MonkeyPatch, fake: FakeSocket, **kwargs: Any) -> HerdrSheep:
    monkeypatch.setattr(herdr_mod, "HerdrSocket", lambda *a, **k: fake)
    return HerdrSheep("/fake.sock", **kwargs)


def run_sheep(sheep: HerdrSheep) -> list[Alert]:
    alerts: list[Alert] = []
    try:
        for alert in sheep.watch():
            alerts.append(alert)
    except StopWatching:
        pass
    return alerts


def test_already_idle_agent_alerts_on_the_first_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSocket([[agent()]])
    alerts = run_sheep(sheep_over(monkeypatch, fake))
    assert [a.title for a in alerts] == ["ShepAlert: opencode idle"]


def test_working_agent_does_not_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_sheep(sheep_over(monkeypatch, FakeSocket([[agent(agent_status="working")]]))) == []


def test_transition_into_a_non_working_status_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSocket([[agent(agent_status="working")], [agent()]])
    assert len(run_sheep(sheep_over(monkeypatch, fake))) == 1


def test_transition_into_working_does_not_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSocket([[agent()], [agent(agent_status="working")]])
    assert len(run_sheep(sheep_over(monkeypatch, fake))) == 1  # only the first poll


def test_repeated_polls_do_not_re_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSocket([[agent()], [agent()], [agent()]])
    assert len(run_sheep(sheep_over(monkeypatch, fake))) == 1


def test_a_vanished_agent_is_forgotten_and_re_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSocket([[agent()], [], [agent()]])
    assert len(run_sheep(sheep_over(monkeypatch, fake))) == 2


def test_state_is_not_half_updated_when_a_cycle_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cycle commits its state in one go, so a caller that stops early - the
    `break` in the live-socket test does exactly this - leaves the sheep still
    remembering the previous poll rather than a mixture of the two."""
    other = agent(terminal_id="term_other")
    sheep = sheep_over(monkeypatch, FakeSocket([[agent(agent_status="working"), other]]))
    sheep.last_status = {TERMINAL: "working", "term_other": "working"}

    # `islice` walks away after the first alert, exactly as the `break` in the
    # live-socket test does, leaving the cycle suspended and uncommitted.
    # Only `other` alerts: the seeded poll already had it as `working`.
    assert [a.subject for a in islice(sheep.watch(), 1)] == ["term_other"]

    assert sheep.last_status == {TERMINAL: "working", "term_other": "working"}


def test_state_is_keyed_on_terminal_id_not_pane_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two panes, one of them recycled: state must follow terminal_id."""
    recycled = agent(terminal_id="term_other", agent_status="idle")
    fake = FakeSocket(
        [
            [recycled, agent(agent_status="working")],
            [recycled, agent()],
        ]
    )
    alerts = run_sheep(sheep_over(monkeypatch, fake))
    # The recycled pane is new, so it alerts; the working one only alerts on arrival.
    assert [a.subject for a in alerts] == ["term_other", TERMINAL]
    assert all(a.title.startswith("ShepAlert: opencode idle") for a in alerts)


@pytest.mark.parametrize("missing", ["terminal_id", "agent_status"])
def test_incomplete_agent_records_are_skipped(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    incomplete = {k: v for k, v in AGENT.items() if k != missing}
    assert run_sheep(sheep_over(monkeypatch, FakeSocket([[incomplete]]))) == []


def test_transition_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:

    caplog.set_level(logging.INFO)
    fake = FakeSocket([[agent(agent_status="working")], [agent()]])
    run_sheep(sheep_over(monkeypatch, fake))
    assert "wZ:p3E opencode working -> idle" in caplog.text


def test_each_poll_sleeps_for_the_interval(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    run_sheep(sheep_over(monkeypatch, FakeSocket([[agent()]] * 3), interval=7.5))
    assert sleeps == [7.5, 7.5, 7.5]


def test_herdr_being_down_backs_off(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    alerts = run_sheep(sheep_over(monkeypatch, FakeSocket([[agent()], DOWN])))
    assert len(alerts) == 1
    assert sleeps == [DEFAULT_INTERVAL, BACKOFF_START]


def test_an_outage_does_not_re_alert_a_status_it_already_knew(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """The README's reliability promise: herdr restarting must not re-notify
    you about everything still sitting idle. The status survives the outage, so
    the poll after it is not a change and stays silent."""
    fake = FakeSocket([[agent()], DOWN, [agent()], [agent()]])

    alerts = run_sheep(sheep_over(monkeypatch, fake))

    assert [a.title for a in alerts] == ["ShepAlert: opencode idle"]
    assert sleeps == [DEFAULT_INTERVAL, BACKOFF_START, DEFAULT_INTERVAL, DEFAULT_INTERVAL]


def test_backoff_doubles_to_the_ceiling(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    run_sheep(sheep_over(monkeypatch, FakeSocket([DOWN] * 8)))
    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(sleeps) == 30.0


def test_the_polling_constants_are_what_the_readme_says() -> None:
    """Pinned as literals, not as the symbols, so changing one cannot leave
    every test green while the README's table quietly becomes a lie."""
    assert DEFAULT_INTERVAL == 10.0
    assert BACKOFF_START == 1.0
    assert BACKOFF_MAX == 30.0
    assert herdr_mod.DEFAULT_TIMEOUT == 5.0


def test_a_recovered_poll_resets_the_backoff(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    run_sheep(sheep_over(monkeypatch, FakeSocket([DOWN, [agent()], [agent()]]), interval=2.0))
    assert sleeps == [BACKOFF_START, 2.0, 2.0]


def test_polling_a_real_socket_notifies_on_a_live_change(tmp_path: Path) -> None:
    """The whole path: framing, request, diff, alert - with no fakes at all."""
    statuses = ["idle", "working", "idle"]

    def serve(conn: socket.socket) -> None:
        buffer = b""
        while (frame := read_frame(conn, buffer)) is not None:
            request, buffer = frame
            status = statuses.pop(0) if len(statuses) > 1 else "idle"
            conn.sendall(
                json.dumps({"id": request["id"], "result": {"agents": [agent(agent_status=status)]}}).encode() + b"\n"
            )

    with unix_server(tmp_path, serve) as path:
        sheep = HerdrSheep(path, interval=0)
        alerts: list[Alert] = []
        for alert in sheep.watch():
            alerts.append(alert)
            if len(alerts) == 2:
                break
        # idle -> working is silent, working -> idle alerts again.
        assert [a.title for a in alerts] == [
            "ShepAlert: opencode idle",
            "ShepAlert: opencode idle",
        ]
