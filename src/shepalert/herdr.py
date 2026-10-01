"""Client for the herdr Unix-socket API, and the sheep that watches it."""

import contextlib
import functools
import itertools
import json
import logging
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from shepalert.core import Alert, Sheep

log = logging.getLogger(__name__)


class HerdrError(Exception):
    """Anything the herdr socket protocol can do wrong."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 10.0
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
UNKNOWN = "unknown"


def default_socket_path() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or str(Path.home() / ".config" / "herdr" / "herdr.sock")


def is_alertable(status: str | None) -> bool:
    return status is not None and status != "working"


@functools.cache
def hostname() -> str:
    """This machine's name, for the alert body.

    Cached on first use rather than read at import, so importing this module
    does no I/O and a test can substitute a name without reloading.
    """
    return socket.gethostname()


def _repo_name(cwd: str | None) -> str:
    """The project directory's name, or `UNKNOWN` when there is not one.

    A cwd of `/` or nothing at all has no basename to report, so say the project
    is unknown rather than name the wrong one.
    """
    return os.path.basename((cwd or "").rstrip("/")) or UNKNOWN


def _decode(line: bytes) -> dict[str, Any]:
    try:
        frame = json.loads(line)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HerdrError("bad_json", line[:120].decode("utf-8", "replace")) from exc
    if not isinstance(frame, dict):
        raise HerdrError("bad_json", f"expected an object, got {type(frame).__name__}")
    return frame


def _result(frame: dict[str, Any]) -> dict[str, Any]:
    error = frame.get("error")
    if isinstance(error, dict):
        raise HerdrError(str(error.get("code") or "error"), str(error.get("message") or ""))
    result = frame.get("result")
    if not isinstance(result, dict):
        raise HerdrError("bad_response", "response carried no result object")
    return result


def build_alert(pane: dict[str, Any], status: str) -> Alert:
    agent = str(pane.get("agent") or UNKNOWN)
    return Alert(
        source="herdr",
        subject=str(pane.get("terminal_id") or ""),
        title=f"ShepAlert: {agent} {status}",
        body=f"host: {hostname()}\ndir: {_repo_name(pane.get('cwd'))}",
    )


class HerdrSocket:
    """Newline-delimited JSON over the herdr Unix socket."""

    def __init__(self, path: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.path = path or default_socket_path()
        self.timeout = timeout
        self._ids = itertools.count(1)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request on a throwaway connection and return its result."""
        req_id = f"req{next(self._ids)}"
        with self._connect() as sock:
            self._send(sock, {"id": req_id, "method": method, "params": params or {}})
            # herdr interleaves unsolicited event frames with our answer, so read
            # until our own id comes back. `_frames` never returns - it yields or
            # raises - hence `while True` rather than `for`, which would leave a
            # fall-through past the end of the loop.
            frames = self._frames(sock)
            while True:
                frame = next(frames)
                if frame.get("id") != req_id:
                    continue
                return _result(frame)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[socket.socket]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
        except OSError as exc:
            sock.close()
            raise HerdrError("connect_failed", f"{self.path}: {exc}") from exc
        try:
            yield sock
        finally:
            sock.close()

    def _send(self, sock: socket.socket, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        try:
            sock.sendall(line)
        except OSError as exc:
            raise HerdrError("send_failed", str(exc)) from exc

    def _frames(self, sock: socket.socket) -> Iterator[dict[str, Any]]:
        buffer = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError as exc:
                raise HerdrError("timeout", f"no data within {self.timeout}s") from exc
            except OSError as exc:
                raise HerdrError("disconnected", str(exc)) from exc
            if not chunk:
                raise HerdrError("eof", "connection closed mid-frame" if buffer else "closed")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    yield _decode(line)


class HerdrSheep(Sheep):
    """Alerts every time an agent pane stops being `working`.

    Polls `agent.list` rather than subscribing to herdr's event stream. The global
    `pane.updated` stream is a state dump pushed once on subscribe, not a change
    feed - a live transition is only published on the per-pane
    `pane.agent_status_changed` event, which needs an explicit pane id and therefore
    one socket per agent. For a handful of agents, one 4 ms request every 10 s is
    far less machinery for a notification that is not urgent.
    """

    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        interval: float = DEFAULT_INTERVAL,
    ) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.interval = interval
        self.last_status: dict[str, str] = {}
        self.backoff = BACKOFF_START

    def watch(self) -> Iterator[Alert]:
        while True:
            try:
                yield from self._cycle()
            except HerdrError as exc:
                log.warning("herdr: %s", exc)
                log.info("herdr: retrying in %.0fs", self.backoff)
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, BACKOFF_MAX)
                continue
            time.sleep(self.interval)

    def _cycle(self) -> Iterator[Alert]:
        agents = HerdrSocket(self.socket_path, self.timeout).request("agent.list")
        self.backoff = BACKOFF_START
        # Diff against the previous poll into `seen`, and commit `seen` once at
        # the end. Mutating self.last_status as we go would leave the sheep
        # holding half of two polls if this generator is abandoned part-way
        # through, which is exactly what a caller that stops early does.
        seen: dict[str, str] = {}
        for agent in agents.get("agents") or []:
            terminal_id = agent.get("terminal_id")
            name = agent.get("agent")
            status = agent.get("agent_status")
            # `agent.list` is documented to list only agents, so a record with
            # no agent is a plain shell pane - or a protocol change. Either way
            # there is nothing to report, and alerting on it would say
            # "unknown idle", which is worse than silence.
            if not terminal_id or not name or status is None:
                continue
            seen[terminal_id] = status
            previous = self.last_status.get(terminal_id)
            if previous == status:
                continue
            log.info("%s %s %s -> %s", agent.get("pane_id"), agent.get("agent"), previous, status)
            if is_alertable(status):
                yield build_alert(agent, status)
        # Agents missing from `seen` have gone away, and dropping them here means
        # one that reappears is treated as new rather than diffed against a
        # status left over from before it exited.
        for terminal_id in self.last_status.keys() - seen.keys():
            log.debug("herdr: %s is gone", terminal_id)
        self.last_status = seen
