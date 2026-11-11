"""
Structural level gate tests.

Motivating finding: twelve of nineteen era-3 market entries had no hourly swing
within 400 points, and they accounted for all of the losses.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.gates.base import GateContext, Verdict  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.gates.structural import StructuralLevelGate  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


class Levels:
    def __init__(self, support=None, resistance=None, raises=False):
        self.s, self.r, self._raises = support, resistance, raises

    def nearest_support(self, price):
        if self._raises:
            raise ConnectionError("feed down")
        return self.s

    def nearest_resistance(self, price):
        if self._raises:
            raise ConnectionError("feed down")
        return self.r


def ctx(price=80000.0, buy=True):
    c = GateContext(signal={}, origin_hash="o", policy=POLICY)
    c.derived.update({"execution_price": price, "is_buy": buy})
    return c


def run(gate, c):
    return GatePipeline([gate]).run(c, "sig_s", "sha256:p")


def test_shadow_mode_never_refuses():
    t = run(StructuralLevelGate(Levels(support=None)), ctx())
    assert t.action is Verdict.ALLOW


def test_shadow_mode_records_the_objection():
    """The objection must reach the trace, or shadow mode measures nothing."""
    t = run(StructuralLevelGate(Levels(support=79000.0)), ctx(80000.0))
    assert [r.name for r in t.advisory_objections()] == ["structural_level"]


def test_the_objection_reaches_the_decision_log():
    """Without this the shadow record exists nowhere anyone reads."""
    from zcastor.record.decision_log import DecisionLog
    t = run(StructuralLevelGate(Levels(support=None)), ctx())
    log = DecisionLog("2026-09-23")
    log.add(signal_id="sig_s", at="t", trace=t, origin="o", process="p", state="s")
    assert log.entries[0]["advisory_objected"] == ["structural_level"]


def test_policy_ships_in_shadow():
    assert POLICY["structural_level_enforce"] is False


def test_open_air_is_refused_when_enforced():
    t = run(StructuralLevelGate(Levels(support=None), enforce=True), ctx())
    assert t.action is Verdict.SUPPRESS and t.reason == "no_structural_level"


def test_a_distant_level_is_refused_when_enforced():
    t = run(StructuralLevelGate(Levels(support=79000.0), enforce=True), ctx(80000.0))
    assert t.reason == "no_structural_level"


def test_a_nearby_level_passes():
    t = run(StructuralLevelGate(Levels(support=79750.0), enforce=True), ctx(80000.0))
    assert t.action is Verdict.ALLOW


def test_a_sell_looks_for_resistance_above():
    t = run(StructuralLevelGate(Levels(resistance=80300.0), enforce=True),
            ctx(80000.0, buy=False))
    assert t.action is Verdict.ALLOW


def test_no_level_source_does_not_stop_trading():
    t = run(StructuralLevelGate(None, enforce=True), ctx())
    assert t.action is Verdict.ALLOW


def test_a_failing_level_feed_does_not_stop_trading():
    t = run(StructuralLevelGate(Levels(raises=True), enforce=True), ctx())
    assert t.action is Verdict.ALLOW


def test_routed_signals_are_exempt():
    assert StructuralLevelGate().runs_when_routed is False


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
