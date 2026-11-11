"""
Venue guard tests.

For four days in September the engine traded Binance testnet while everyone
believed it was on MT5. These pin the behaviour that stops that recurring.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.app.venue import VenueDisagreement, resolve_venue  # noqa: E402


def resolve(declared="", env="", override=""):
    runtime = {"venue": declared} if declared else {}
    environ = {}
    if env:
        environ["ZCASTOR_VENUE"] = env
    if override:
        environ["ZCASTOR_VENUE_OVERRIDE"] = override
    return resolve_venue(runtime, environ)


def test_default_is_mt5():
    assert resolve() == "mt5"


def test_runtime_declaration_is_used():
    assert resolve(declared="mt5") == "mt5"


def test_env_alone_is_honoured():
    assert resolve(env="binance") == "binance"


def test_agreement_is_fine():
    assert resolve(declared="mt5", env="mt5") == "mt5"


def test_a_silent_disagreement_refuses_to_start():
    """The September incident: .env said binance, the declaration said mt5."""
    try:
        resolve(declared="mt5", env="binance")
    except VenueDisagreement as exc:
        assert "ZCASTOR_VENUE_OVERRIDE" in str(exc)
    else:
        raise AssertionError("must refuse to start on a disagreement")


def test_an_acknowledged_override_runs():
    assert resolve(declared="mt5", env="binance", override="yes") == "binance"


def test_the_decision_log_records_the_venue():
    from zcastor.record.decision_log import DecisionLog
    assert DecisionLog("2026-09-23", venue="mt5").build()["venue"] == "mt5"


def test_discord_stamps_the_venue_on_every_decision():
    from zcastor.notify.discord import Discord

    class T:
        action = type("A", (), {"value": "suppress"})()
        reason = "session_blocked"

        def binding(self):
            return type("G", (), {"name": "session"})()

        def shadow_objections(self):
            return []

    sent = []
    d = Discord("https://x.test/h", venue="binance", events={"decision"})
    d.send = sent.append
    d.decision(T(), {"direction": "UP", "timeframe": "1h",
                     "session": "London", "confidence": "high"})
    assert "BINANCE" in sent[0]


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
