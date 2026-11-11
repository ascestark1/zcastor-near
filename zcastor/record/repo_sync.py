"""
Keep the public dossier repo in step with what gets anchored.

Every anchored record carries a URI into the dossier repo. If the file behind
that URI is never pushed, the record still proves *that* something existed,
but nobody can fetch *what*. From 6 Sep to 2 Oct 2026 the repo went unpushed
and four weeks of anchored URIs returned 404 to anyone checking.

So publishing now schedules a commit-and-push. Design:

- **Debounced.** A session close writes a log, a manifest and sometimes a
  policy in quick succession; one push covers them all.
- **Off the hot path.** Runs on a timer thread. A slow or failing git never
  touches signal handling, trading or anchoring.
- **Never prompts.** GIT_TERMINAL_PROMPT=0 plus a timeout: if credentials are
  missing, the push fails fast and says so in the log instead of hanging the
  thread on a password prompt nobody will answer.
- **Never raises.** Failures are logged; the next publish tries again, and
  so does the flush on shutdown.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("zcastor.record.repo_sync")

Runner = Callable[..., subprocess.CompletedProcess]


class RepoSync:
    def __init__(self, root: str | Path, *, enabled: bool = True,
                 delay: float = 15.0, timeout: float = 60.0,
                 runner: Optional[Runner] = None) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self.delay = delay
        self.timeout = timeout
        self._run = runner or subprocess.run
        self._lock = threading.Lock()           # guards the timer and reasons
        self._push_lock = threading.Lock()      # one git sequence at a time
        self._timer: Optional[threading.Timer] = None
        self._reasons: list[str] = []
        self.last_result: str = "never run"

    # ── scheduling ────────────────────────────────────────────────────────────

    def request(self, reason: str = "") -> None:
        """Schedule a push. Repeated calls inside the delay collapse into one."""
        if not self.enabled:
            return
        with self._lock:
            if reason:
                self._reasons.append(reason)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self.push)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> bool:
        """Push now, cancelling any pending timer. Used on shutdown."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        return self.push() if self.enabled else False

    # ── the push ──────────────────────────────────────────────────────────────

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        return self._run(["git", "-C", str(self.root), *args],
                         capture_output=True, text=True,
                         timeout=self.timeout, env=env)

    def push(self) -> bool:
        with self._lock:
            reasons, self._reasons = self._reasons, []
            self._timer = None
        with self._push_lock:
            return self._push(reasons)

    def _push(self, reasons: list[str]) -> bool:
        if not (self.root / ".git").exists():
            self.last_result = "not a git repo"
            logger.warning("dossier repo %s is not a git repo — not pushing",
                           self.root)
            return False
        try:
            self._git("add", "-A")
            staged = self._git("diff", "--cached", "--quiet")
            if staged.returncode != 0:
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                what = ", ".join(sorted(set(reasons))) or "records"
                commit = self._git("commit", "-m", f"records: {what} ({stamp})")
                if commit.returncode != 0:
                    raise RuntimeError(commit.stderr.strip() or commit.stdout.strip())
            # Push even with nothing new staged: earlier commits may still be
            # waiting from a run where the push failed.
            pushed = self._git("push")
            if pushed.returncode != 0:
                raise RuntimeError(pushed.stderr.strip()[:300])
        except subprocess.TimeoutExpired:
            self.last_result = "timed out"
            logger.warning("dossier push timed out after %ss", self.timeout)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_result = f"failed: {exc}"
            hint = (" — store a credential: git config credential.helper store, "
                    "then push once by hand"
                    if "terminal prompts disabled" in str(exc)
                    or "could not read Username" in str(exc) else "")
            logger.warning("dossier push failed: %s%s", exc, hint)
            return False
        self.last_result = "pushed"
        logger.info("dossier repo pushed (%s)", ", ".join(sorted(set(reasons))) or "sync")
        return True
