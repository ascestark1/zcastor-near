"""
Engine lock.

The engine holds the outbox and the anchor index in memory and appends to both.
An ops script run while it is up appends too, from its own separate copy of the
state, and neither sees the other's writes. That produced divergent state three
times: repairs that the engine then overwrote, and requeues it never saw.

So the engine takes a lock file while it runs, and scripts that write to the
outbox or index refuse to start while the lock is held by a live process.

A stale lock — left by a crash — is detected by checking whether the recorded
process still exists, so a crash never blocks recovery.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_NAME = ".engine.lock"


class EngineRunning(RuntimeError):
    """A write was attempted while the engine holds the state."""


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)          # signal 0 checks existence without sending
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # exists, owned by someone else
    return True


def acquire(data_dir: str | Path) -> Path:
    """Called by the engine at startup."""
    path = Path(data_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = holder_pid(data_dir)
    if holder and holder != os.getpid() and _alive(holder):
        raise EngineRunning(f"another engine is running as pid {holder}")
    path.write_text(str(os.getpid()))
    return path


def release(data_dir: str | Path) -> None:
    path = Path(data_dir) / LOCK_NAME
    if path.exists() and holder_pid(data_dir) == os.getpid():
        path.unlink()


def holder_pid(data_dir: str | Path) -> int:
    path = Path(data_dir) / LOCK_NAME
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def require_engine_stopped(data_dir: str | Path) -> None:
    """Called by ops scripts before they write shared state."""
    pid = holder_pid(data_dir)
    if pid and _alive(pid):
        raise EngineRunning(
            f"the engine is running (pid {pid}) and holds the outbox and index "
            f"in memory. Writing now would fork the state — stop it first.")
