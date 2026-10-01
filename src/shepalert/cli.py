"""Console entry point: environment, logging, --dry-run, wiring."""

import argparse
import logging
import os
import sys

from shepalert import Alerter, Monitor
from shepalert.alerter import BarkAlerter, LogAlerter
from shepalert.herdr import DEFAULT_INTERVAL, HerdrSheep

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _positive_seconds(raw: str) -> float:
    """An argparse `type` for `--interval`: reject anything unusable outright.

    A bad flag is a typo the user can see and fix, so it hard-fails with exit 2.
    `SHEPALERT_INTERVAL` is deliberately more forgiving - it warns and falls back,
    because an unset or stale value in a shell profile should not stop the app
    from starting. The two paths differ on purpose; do not "fix" one to match
    the other without deciding which policy applies.
    """
    try:
        seconds = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than zero, got {raw!r}")
    return seconds


def _interval() -> float:
    raw = (os.environ.get("SHEPALERT_INTERVAL") or "").strip()
    if not raw:
        return DEFAULT_INTERVAL
    try:
        seconds = float(raw)
    except ValueError:
        log.warning("unrecognised SHEPALERT_INTERVAL %r, falling back to %ss", raw, DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL
    if seconds <= 0:
        log.warning("SHEPALERT_INTERVAL must be positive, got %r; using %ss", raw, DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL
    return seconds


def _log_level() -> int:
    raw = (os.environ.get("SHEPALERT_LOG_LEVEL") or "").strip()
    if not raw:
        return logging.INFO
    level = logging.getLevelNamesMapping().get(raw.upper())
    if level is None:
        log.warning("unrecognised SHEPALERT_LOG_LEVEL %r, falling back to INFO", raw)
        return logging.INFO
    return level


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shepalert",
        description="Notify via Bark when a herdr coding agent stops being busy.",
    )
    parser.add_argument(
        "--interval",
        type=_positive_seconds,
        default=None,
        metavar="SECONDS",
        help=f"how often to poll herdr for agent status changes (default {DEFAULT_INTERVAL:g})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log alerts to stderr instead of sending them, and need no BARK_URL/BARK_KEY",
    )
    return parser.parse_args(argv)


def _read_bark_env() -> tuple[str, str]:
    values = {name: (os.environ.get(name) or "").strip() for name in ("BARK_URL", "BARK_KEY")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        log.error(
            "ShepAlert: missing environment variable(s): %s;"
            " set them in the environment (BARK_URL = Bark server base URL,"
            " BARK_KEY = device key), or pass --dry-run to log alerts instead",
            ", ".join(missing),
        )
        raise SystemExit(2)
    return values["BARK_URL"], values["BARK_KEY"]


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(format=LOG_FORMAT, stream=sys.stderr)
    logging.getLogger().setLevel(_log_level())
    if args.dry_run:
        alerter: Alerter = LogAlerter()
        mode = "dry-run"
    else:
        base_url, device_key = _read_bark_env()
        alerter = BarkAlerter(base_url, device_key)
        mode = f"bark at {base_url}"
    interval = args.interval if args.interval is not None else _interval()
    log.info("ShepAlert: watching herdr (%s), polling every %gs", mode, interval)
    try:
        Monitor(HerdrSheep(interval=interval), alerter).run()
    except KeyboardInterrupt:
        log.info("ShepAlert: interrupted, stopping")
    finally:
        alerter.close()
