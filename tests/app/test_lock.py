"""
Engine lock tests.

The engine and an ops script both appending to the outbox, each from its own
in-memory copy, forked the state three times. The lock stops that.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.app import lock  # noqa: E402


def test_a_script_refuses_while_the_engine_holds_the_lock():
    with tempfile.TemporaryDirectory() as d:
        lock.acquire(d)                       # this process stands in for the engine
        try:
            lock.require_engine_stopped(d)
        except lock.EngineRunning as exc:
            assert "stop it first" in str(exc)
        else:
            raise AssertionError("must refuse while the engine runs")


def test_a_script_runs_once_the_engine_has_stopped():
    with tempfile.TemporaryDirectory() as d:
        lock.acquire(d)
        lock.release(d)
        lock.require_engine_stopped(d)        # must not raise


def test_a_stale_lock_from_a_crash_does_not_block_recovery():
    """A crash leaves the file behind; a dead pid must not hold anything up."""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / lock.LOCK_NAME).write_text("999999")   # no such process
        lock.require_engine_stopped(d)
        lock.acquire(d)                                  # and a new engine starts


def test_no_lock_means_no_engine():
    with tempfile.TemporaryDirectory() as d:
        lock.require_engine_stopped(d)


def test_release_only_removes_our_own_lock():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / lock.LOCK_NAME
        path.write_text(str(os.getppid()))   # someone else's live pid
        lock.release(d)
        assert path.exists()


def test_a_garbage_lock_file_is_harmless():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / lock.LOCK_NAME).write_text("not a pid")
        lock.require_engine_stopped(d)


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
