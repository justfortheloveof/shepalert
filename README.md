# ShepAlert

Watches herdr, the terminal workspace manager for coding agents, through its local
Unix socket, and pushes a Bark notification to your iPhone whenever an agent stops
being busy, i.e. whenever an agent's status becomes anything other than `working`
(`idle`, `done`, `blocked` or `unknown`). It polls herdr's `agent.list` every 10
seconds, about 4 ms per poll, and notifies on a status change.

## Requirements

- A **running herdr server**. ShepAlert talks to the herdr socket at `$HERDR_SOCKET_PATH`,
  falling back to `~/.config/herdr/herdr.sock`. It needs the socket, so herdr has to be
  up before ShepAlert can do anything.
- A **Bark server** and a **device key** from it (not needed for `--dry-run`). See
  [Finb/bark-server](https://github.com/Finb/bark-server) for the server - use an
  existing one or run your own.
- **Python >= 3.11** and [**uv**](https://docs.astral.sh/uv/).

## Install and run

```sh
uv sync
uv run shepalert --help
```

```
usage: shepalert [-h] [--interval SECONDS] [--dry-run]

Notify via Bark when a herdr coding agent stops being busy.

options:
  -h, --help          show this help message and exit
  --interval SECONDS  how often to poll herdr for agent status changes (default 10)
  --dry-run           log alerts to stderr instead of sending them, and need no BARK_URL/BARK_KEY
```

## Configuration

Environment variables only. Four variables:

| Variable | Required | Meaning |
| --- | --- | --- |
| `BARK_URL` | yes, unless `--dry-run` | Bark server base URL, e.g. `https://bark.example.com`. A trailing `/` is stripped. |
| `BARK_KEY` | yes, unless `--dry-run` | Bark device key. A secret: it is never written to a log line, and the tests assert that. |
| `SHEPALERT_INTERVAL` | no | Seconds between polls. Default `10`. Also settable with `--interval`. |
| `SHEPALERT_LOG_LEVEL` | no | Logging level for the whole app. Default `INFO`. |

Logging goes to stderr. Everything else is a constant in the source: the socket path,
the 5 s request timeout, and the 1 s→30 s retry backoff.

## What it alerts on

An alert fires on any status change into something other than `working`:

- **A change into a non-`working` status.** An agent going `working -> idle` notifies;
  staying `idle` does not.
- **Startup.** Agents that are already alertable when ShepAlert starts get one
  notification each, so you find out what is waiting for you.
- **After herdr goes away.** If a poll fails, ShepAlert backs off 1 s→30 s and retries,
  keeping the status it already knows. A restart of herdr therefore does *not* re-notify
  you about everything still sitting idle.

Two things to be aware of:

- **There is no debounce.** A genuine `idle -> working -> idle` cycle alerts twice, by
  decision.
- **`unknown` flaps.** `unknown` is alertable, and an agent restarting in a pane
  produces a real `idle -> unknown -> idle` transition, so agent restarts notify too.

Status is kept in memory only, so restarting ShepAlert itself re-alerts everything that
is currently alertable. Panes with no agent never alert: a record is only considered if
it carries a `terminal_id`, an `agent` and a status, so a plain shell sitting in a pane
is filtered out before it can alert.

## How it works

`HerdrSheep` polls `agent.list` on the herdr socket, keeps the last status it saw per
`terminal_id`, and yields an `Alert` whenever an agent's status changes. Transitions
*into* `working` are logged but silent, so one work cycle produces one notification.

## Extending it

Two abstract base classes carry the whole design. `Sheep` is a thing being watched: one
method, `watch() -> Iterator[Alert]`, a generator that yields one `Alert` per thing worth
reporting. `Alerter` is a notification backend: one method, `notify(alert)`, which either
delivers the alert or raises `NotifyError`, plus an optional `close()`. `Alert` is a
frozen dataclass of `source`, `subject`, `title` and `body`.

`Monitor` knows neither herdr nor Bark. It is a loop that pulls alerts from the sheep
and hands each one to the alerter, logging and continuing if a notification fails.

### A new Alerter

One class, one method, transport only:

```python
import httpx

from shepalert import Alert, Alerter, NotifyError


class NtfyAlerter(Alerter):
    """Publishes alerts to an ntfy topic."""

    def __init__(self, topic: str, server: str = "https://ntfy.sh") -> None:
        self._url = f"{server.rstrip('/')}/"
        self._payload = {"topic": topic, "tags": ["bell"]}
        self._client = httpx.Client(timeout=10.0)

    def notify(self, alert: Alert) -> None:
        try:
            response = self._client.post(
                self._url,
                json={**self._payload, "title": alert.title, "message": alert.body},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifyError(f"ntfy publish failed: {exc}") from exc

    def close(self) -> None:
        self._client.close()
```

`Alerter.close()` is a no-op on the base class, so override it only if you opened
something. The CLI calls it once on the way out - including after a `Ctrl-C` - so a
client you own is closed rather than left to the garbage collector. An alerter that
takes an injected client (as the tests do) should not close it, since the caller
still owns it.

`LogAlerter` in `src/shepalert/alerter.py` is the same shape in six lines, and is what
`--dry-run` uses. Wire the new one in `src/shepalert/cli.py` by passing it to the
monitor instead:

```python
from shepalert import Monitor
from shepalert.herdr import HerdrSheep

Monitor(HerdrSheep(), NtfyAlerter("my-agent-alerts")).run()
```

### A new Sheep

A sheep yields `Alert`s and nothing else. It owns its own notion of what is worth
reporting, its own wording for the title and body, and its own retry handling - an
unreachable source is the sheep's problem to absorb, since `Monitor` has no retry logic.
`HerdrSheep` is the reference implementation; keep the last-known state in an instance
dict and yield only on the transitions you care about.

## Tests

```sh
uv run pytest
```
