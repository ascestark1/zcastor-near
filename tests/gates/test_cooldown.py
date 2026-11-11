"""
Entry cooldown tests.

Reconstructs the 31 August defect: four entries on essentially one setup,
16.53 of a 51.46 total loss. The stack limit permitted it because five
positions were allowed and only four were open — position count was the wrong
measure.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.gates.base import GateContext, Verdict  # noqa: E402
from zcastor.gates.cooldown import EntryCooldownGate  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def ctx(direction="SELL", price=77948.55):
    c = GateContext(signal={}, origin_hash="o", policy=POLICY)
    c.derived.update({"direction": direction, "execution_price": price})
    return c


def run(gate, c):
    return GatePipeline([gate]).run(c, "sig_test", "sha256:p")


def test_the_first_entry_is_never_blocked():
    assert run(EntryCooldownGate(), ctx()).action is Verdict.ALLOW


def test_the_31_august_duplicate_is_refused():
    """Two fills in the same second at 77948.55 and 77941.25."""
    clock = Clock()
    gate = EntryCooldownGate(clock=clock)
    gate.record_fill(77948.55, "SELL")
    t = run(gate, ctx("SELL", 77941.25))
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "entry_cooldown"




def test_the_cooldown_expires():
    clock = Clock()
    gate = EntryCooldownGate(clock=clock)
    gate.record_fill(77948.55, "SELL")
    clock.advance(POLICY["entry_cooldown_seconds"] + 1)
    assert run(gate, ctx("SELL", 77941.25)).action is Verdict.ALLOW


def test_an_opposite_direction_entry_is_a_reversal_not_a_double_up():
    gate = EntryCooldownGate(clock=Clock())
    gate.record_fill(77948.55, "SELL")
    assert run(gate, ctx("BUY", 77941.25)).action is Verdict.ALLOW


def test_a_distant_entry_is_a_different_trade():
    gate = EntryCooldownGate(clock=Clock())
    gate.record_fill(77948.55, "SELL")
    far = 77948.55 - POLICY["entry_cooldown_distance_pts"] - 100
    assert run(gate, ctx("SELL", far)).action is Verdict.ALLOW


def test_routed_signals_are_exempt():
    """
    An armed entry has already waited for its level; the fill IS the
    confirmation. Cooling those down would suppress the only entry type that
    measured positive.
    """
    assert EntryCooldownGate().runs_when_routed is False


def test_only_a_fill_starts_the_cooldown():
    """A refused signal must not block the next one."""
    gate = EntryCooldownGate(clock=Clock())
    run(gate, ctx())
    assert run(gate, ctx()).action is Verdict.ALLOW


# ── the vocabulary bug ────────────────────────────────────────────────────────

def test_the_production_vocabularies_are_reconciled():
    """
    Production records a fill as BUY/SELL and evaluates a signal as UP/DOWN.
    Comparing the two directly let every same-direction entry through as a
    "reversal" — the cooldown never blocked anything in production. The
    earlier tests used SELL on both sides, so they could not see it.
    """
    gate = EntryCooldownGate(clock=Clock())
    gate.record_fill(77948.55, "SELL")                 # as the engine reports it
    t = run(gate, ctx("DOWN", 77941.25))               # as the gate context holds it
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "entry_cooldown"


def test_a_real_reversal_still_passes():
    gate = EntryCooldownGate(clock=Clock())
    gate.record_fill(77948.55, "SELL")
    assert run(gate, ctx("UP", 77941.25)).action is Verdict.ALLOW


def test_the_watcher_can_ask_directly():
    """Watcher fills bypass the pipeline, so the gate answers them directly."""
    gate = EntryCooldownGate(clock=Clock())
    gate.record_fill(77948.55, "BUY")
    assert gate.blocks(77941.0, "UP")
    assert gate.blocks(77941.0, "BUY")
    assert not gate.blocks(77941.0, "SELL")
    assert not gate.blocks(79000.0, "BUY"), "far enough to be a different trade"


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
