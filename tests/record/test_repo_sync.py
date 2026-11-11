"""
Dossier repo sync tests.

The dossier repo went unpushed from 6 Sep to 2 Oct 2026 and every anchored URI
in that window returned 404. These check that publishing schedules a push,
that a burst of publishes becomes one push, that a failing git never raises,
and — against a real local remote — that the files actually arrive.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.record.publisher import Publisher  # noqa: E402
from zcastor.record.repo_sync import RepoSync  # noqa: E402


class FakeGit:
    """Records git calls; answers with scripted return codes."""

    def __init__(self, staged=True, push_rc=0, push_err=""):
        self.calls = []
        self.staged = staged
        self.push_rc = push_rc
        self.push_err = push_err

    def __call__(self, cmd, **kwargs):
        args = cmd[3:]                         # drop "git -C <root>"
        self.calls.append((args, kwargs))
        rc, err = 0, ""
        if args[:2] == ["diff", "--cached"]:
            rc = 1 if self.staged else 0
        elif args[:1] == ["push"]:
            rc, err = self.push_rc, self.push_err
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

    def verbs(self):
        return [a[0] for a, _ in self.calls]


def repo_dir(d):
    root = Path(d)
    (root / ".git").mkdir()
    return root


def test_publishing_schedules_a_push():
    with tempfile.TemporaryDirectory() as d:
        seen = []
        pub = Publisher(d, uri_template="https://x.test/{path}")
        pub.on_published = seen.append
        pub.publish_decision_log({"session_date": "2026-10-02", "decisions": []})
        pub.publish_policy({"v": 1}, "sha256:" + "ab" * 32)
        pub.publish_policy({"v": 1}, "sha256:" + "ab" * 32)   # unchanged: no-op
        assert seen == ["decision log 2026-10-02", "policy"]


def test_a_failing_hook_never_fails_a_publish():
    with tempfile.TemporaryDirectory() as d:
        pub = Publisher(d)
        pub.on_published = lambda reason: 1 / 0
        out = pub.publish_decision_log({"session_date": "2026-10-02"})
        assert (Path(d) / out["path"]).exists()


def test_a_burst_of_requests_becomes_one_push():
    with tempfile.TemporaryDirectory() as d:
        git = FakeGit()
        sync = RepoSync(repo_dir(d), delay=0.2, runner=git)
        for reason in ("trade dossier", "trade dossier", "decision log 2026-10-02"):
            sync.request(reason)
        time.sleep(0.6)
        assert git.verbs().count("push") == 1
        commit = next(a for a, _ in git.calls if a[0] == "commit")
        assert "decision log 2026-10-02, trade dossier" in commit[2]
        assert sync.last_result == "pushed"


def test_nothing_new_still_pushes_earlier_commits():
    with tempfile.TemporaryDirectory() as d:
        git = FakeGit(staged=False)
        sync = RepoSync(repo_dir(d), runner=git)
        assert sync.flush() is True
        assert "commit" not in git.verbs() and "push" in git.verbs()


def test_git_never_prompts_and_is_time_bounded():
    with tempfile.TemporaryDirectory() as d:
        git = FakeGit()
        RepoSync(repo_dir(d), timeout=7, runner=git).flush()
        for _args, kwargs in git.calls:
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            assert kwargs["timeout"] == 7


def test_a_failed_push_is_reported_not_raised():
    with tempfile.TemporaryDirectory() as d:
        git = FakeGit(push_rc=128, push_err="fatal: could not read Username")
        sync = RepoSync(repo_dir(d), runner=git)
        assert sync.flush() is False
        assert sync.last_result.startswith("failed:")


def test_a_timeout_is_reported_not_raised():
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
    with tempfile.TemporaryDirectory() as d:
        sync = RepoSync(repo_dir(d), runner=hang)
        assert sync.flush() is False and sync.last_result == "timed out"


def test_not_a_repo_is_reported():
    with tempfile.TemporaryDirectory() as d:
        git = FakeGit()
        sync = RepoSync(d, runner=git)
        assert sync.flush() is False and sync.last_result == "not a git repo"
        assert git.calls == []


def test_disabled_does_nothing():
    with tempfile.TemporaryDirectory() as d:
        git = FakeGit()
        sync = RepoSync(repo_dir(d), enabled=False, runner=git)
        sync.request("trade dossier")
        assert sync.flush() is False and git.calls == []


def test_files_reach_a_real_remote():
    if shutil.which("git") is None:
        return                                 # nothing to test against
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        remote, work = base / "remote.git", base / "work"

        def git(*args, cwd=base):
            subprocess.run(["git", *args], cwd=cwd, check=True,
                           capture_output=True, text=True)

        git("init", "--bare", str(remote))
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
        git("clone", str(remote), str(work))
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=work)
        git("config", "user.email", "test@example.test", cwd=work)
        git("config", "user.name", "test", cwd=work)

        pub = Publisher(work, uri_template="https://x.test/{path}")
        out = pub.publish_decision_log({"session_date": "2026-10-02",
                                        "decisions": []})
        git("add", "-A", cwd=work)
        git("commit", "-m", "first", cwd=work)
        git("push", "-u", "origin", "main", cwd=work)

        sync = RepoSync(work)
        pub.on_published = sync.request
        second = pub.publish_decision_log({"session_date": "2026-10-03",
                                           "decisions": []})
        assert sync.flush() is True, sync.last_result

        listed = subprocess.run(["git", "--git-dir", str(remote), "ls-tree",
                                 "-r", "--name-only", "main"],
                                capture_output=True, text=True, check=True)
        assert out["path"] in listed.stdout
        assert second["path"] in listed.stdout


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
