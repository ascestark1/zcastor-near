"""
Anchor guard tests.

On 2 Oct 2026 a .env without `export` left the engine on the NullBackend while
runtime.json named real registries. Two records got null receipts and would
have been skipped forever. These pin the rule that stops a repeat.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.app.anchor_guard import (  # noqa: E402
    OVERRIDE, AnchorMisconfigured, check_anchor_config)

RUNTIME = {"registry_ids": {"policy": 2194, "decisions": 2195,
                            "dossiers": 2196, "archive": 2197}}


def test_a_real_backend_is_always_fine():
    assert check_anchor_config("nvnm", RUNTIME, {}) == "ok"


def test_null_backend_with_registries_refuses_to_start():
    try:
        check_anchor_config("null", RUNTIME, {})
    except AnchorMisconfigured as exc:
        msg = str(exc)
        assert "2194" in msg and "export" in msg and OVERRIDE in msg
    else:
        raise AssertionError("must refuse: records would get null receipts")


def test_the_override_allows_a_deliberate_offline_run():
    assert check_anchor_config("null", RUNTIME, {OVERRIDE: "yes"}) == "offline-allowed"
    assert check_anchor_config("null", RUNTIME, {OVERRIDE: " YES "}) == "offline-allowed"


def test_anything_but_yes_is_not_an_override():
    for value in ("1", "true", "", "no"):
        try:
            check_anchor_config("null", RUNTIME, {OVERRIDE: value})
        except AnchorMisconfigured:
            continue
        raise AssertionError(f"{value!r} must not count as an override")


def test_no_registries_means_an_offline_setup_not_a_mistake():
    assert check_anchor_config("null", {}, {}) == "ok"
    assert check_anchor_config("null", {"registry_ids": {}}, {}) == "ok"
    assert check_anchor_config("null", {"registry_ids": {"policy": 0}}, {}) == "ok"


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
