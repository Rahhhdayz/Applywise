"""Offline tests for the self-healing local workflow lock."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobagent.runtime import WorkflowLock


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be running."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_lock_is_reclaimed_when_the_owning_process_has_died(tmp_path: Path) -> None:
    lock_path = tmp_path / "jobagent.lock"
    lock_path.write_text(str(_dead_pid()))

    with WorkflowLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())

    assert not lock_path.exists()


def test_lock_is_refused_while_the_owning_process_is_alive(tmp_path: Path) -> None:
    lock_path = tmp_path / "jobagent.lock"
    lock_path.write_text(str(os.getpid()))

    with pytest.raises(RuntimeError, match="already running"), WorkflowLock(lock_path):
        pass  # pragma: no cover - must not be entered

    lock_path.unlink()


def test_lock_treats_a_corrupt_lock_file_as_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "jobagent.lock"
    lock_path.write_text("not-a-pid")

    with WorkflowLock(lock_path):
        assert lock_path.read_text().strip() == str(os.getpid())
