"""Lint and format gates, so `pytest` alone keeps the tree clean."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def dev_executable(name: str) -> str:
    beside_python = Path(sys.executable).parent / name
    if beside_python.is_file():
        return str(beside_python)
    on_path = shutil.which(name)
    if on_path:
        return on_path
    pytest.skip(f"{name} is not installed in this environment")


def run_dev(tool: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [dev_executable(tool), *args, "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_ruff_check_passes() -> None:
    result = run_dev("ruff", "check")
    assert result.returncode == 0, f"ruff check failed:\n{result.stdout}{result.stderr}"


def test_ruff_format_is_canonical() -> None:
    result = run_dev("ruff", "format", "--check")
    assert result.returncode == 0, (
        f"ruff format --check failed, run `uv run ruff format`:\n{result.stdout}{result.stderr}"
    )


def test_mypy_passes() -> None:
    result = run_dev("mypy")
    assert result.returncode == 0, f"mypy failed:\n{result.stdout}{result.stderr}"
