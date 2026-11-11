"""
Batch one gate tests: direction, confidence, feed, spread_hard, deadman, stack.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.gates.base import GateContext, Verdict  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.gates.confidence import ConfidenceGate  # noqa: E402
from zcastor.gates.deadman import DeadmanGate  # noqa: E402
from zcastor.gates.direction import DirectionGate  # noqa: E402
from zcastor.gates.feed import FeedGate  # noqa: E402
from zcastor.gates.spread import SpreadHardGate  # noqa: E402
from zcastor.gates.stack import StackLimitGate  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeMarket:
    def __init__(self, tick=None, balance=1000.0):
        self._tick = tick if tick is not None else {
            "time": 1_750_000_000, "price": 62850.0, "ask": 62860.0, "bid": 62840.0,
        }
        self._balance = balance

    def get_tick(self):
        return self._tick

    def get_balance(self):
        return self._balance


class FakeRisk:
    def __init__(self, pnl=0.0):
        self._pnl = pnl

    def today_pnl(self):
        return self._pnl


class FakePositions:
    def __init__(self, n=0):
        self._n = n

    def live(self):
        return [{"ticket": i} for i in range(self._n)]


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def context(signal=None, market=None, risk=None, positions=None, derived=None):
    ctx = GateContext(
        # `signal or {...}` would be wrong here: an empty dict is falsy, so a
        # deliberately-empty payload would silently get the default one.
        signal=signal if signal is not None else
        {"direction": "UP", "confidence": "high", "timeframe": "15m"},
        origin_hash="sha256:origin",
        policy=POLICY,
        market=market or FakeMarket(),
        risk=risk or FakeRisk(),
        positions=positions or FakePositions(),
    )
    if derived:
        ctx.derived.update(derived)
    return ctx


def run(gates, ctx):
    return GatePipeline(gates).run(ctx, "sig_test", "sha256:policy")


# ── direction ─────────────────────────────────────────────────────────────────

def test_direction_normalises_lowercase():
    ctx = context({"direction": "up"})
    t = run([DirectionGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert ctx.derived["direction"] == "UP"
    assert ctx.derived["is_buy"] is True


def test_direction_missing_rejects_with_no_record():
    t = run([DirectionGate()], context({}))
    assert t.action is Verdict.REJECT
    assert t.reason == "direction_missing"
    assert not t.recordable


def test_direction_garbage_rejects():
    t = run([DirectionGate()], context({"direction": "SIDEWAYS"}))
    assert t.reason == "direction_invalid"


# ── confidence ────────────────────────────────────────────────────────────────

def test_confidence_high_passes():
    t = run([ConfidenceGate()], context({"confidence": "HIGH"}))
    assert t.action is Verdict.ALLOW


def test_confidence_medium_suppresses_and_is_recordable():
    t = run([ConfidenceGate()], context({"confidence": "medium"}))
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "confidence_medium"
    assert t.recordable, "a policy refusal must produce a record"


def test_confidence_missing_suppresses():
    t = run([ConfidenceGate()], context({}))
    assert t.reason == "confidence_unrecognised"


# ── feed ──────────────────────────────────────────────────────────────────────

def test_feed_produces_execution_price_on_ask_for_buy():
    ctx = context(derived={"is_buy": True})
    t = run([FeedGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert ctx.derived["execution_price"] == 62860.0     # ask, not mid
    assert ctx.derived["spread_pts"] == 20


def test_feed_uses_bid_for_sell():
    ctx = context(derived={"is_buy": False})
    run([FeedGate()], ctx)
    assert ctx.derived["execution_price"] == 62840.0


def test_frozen_feed_rejects_after_threshold():
    """The 11 Jun incident: tick time frozen while the dashboard kept firing."""
    clock = FakeClock(1000.0)
    gate = FeedGate(clock=clock)
    frozen = {"time": 1_750_000_000, "price": 62842.25, "ask": 62852.25, "bid": 62832.25}

    first = run([gate], context(market=FakeMarket(frozen), derived={"is_buy": True}))
    assert first.action is Verdict.ALLOW, "first sighting is not yet stale"

    clock.t += 60      # inside the 120s window
    mid = run([gate], context(market=FakeMarket(frozen), derived={"is_buy": True}))
    assert mid.action is Verdict.ALLOW

    clock.t += 121     # 181s frozen
    late = run([gate], context(market=FakeMarket(frozen), derived={"is_buy": True}))
    assert late.action is Verdict.REJECT
    assert late.reason == "stale_feed"
    assert late.results[0].detail["frozen_seconds"] == 181


def test_moving_feed_resets_the_freeze_timer():
    clock = FakeClock(1000.0)
    gate = FeedGate(clock=clock)
    base = {"price": 62850.0, "ask": 62860.0, "bid": 62840.0}

    run([gate], context(market=FakeMarket({**base, "time": 100}), derived={"is_buy": True}))
    clock.t += 300
    t = run([gate], context(market=FakeMarket({**base, "time": 200}), derived={"is_buy": True}))
    assert t.action is Verdict.ALLOW, "a new tick time must reset the timer"


def test_zero_price_rejects():
    tick = {"time": 1, "price": 0, "ask": 0, "bid": 0}
    t = run([FeedGate()], context(market=FakeMarket(tick), derived={"is_buy": True}))
    assert t.reason == "zero_price"


def test_tick_error_rejects():
    t = run([FeedGate()], context(market=FakeMarket({"error": "no connection"}),
                                  derived={"is_buy": True}))
    assert t.reason == "tick_unavailable"


def test_crossed_book_rejects():
    tick = {"time": 1, "price": 100.0, "ask": 90.0, "bid": 110.0}
    t = run([FeedGate()], context(market=FakeMarket(tick), derived={"is_buy": True}))
    assert t.reason == "tick_incoherent"


# ── spread ────────────────────────────────────────────────────────────────────

def test_spread_within_limit_passes():
    t = run([SpreadHardGate()], context(derived={"spread_pts": 50}))
    assert t.action is Verdict.ALLOW




def test_spread_exactly_at_limit_passes():
    t = run([SpreadHardGate()], context(derived={"spread_pts": 60}))
    assert t.action is Verdict.ALLOW, "the block is above the limit, not at it"


# ── deadman ───────────────────────────────────────────────────────────────────

def test_deadman_allows_a_normal_day():
    ctx = context(market=FakeMarket(balance=950.0), risk=FakeRisk(pnl=-50.0))
    t = run([DeadmanGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert ctx.derived["start_of_day"] == 1000.0


def test_deadman_halts_at_seventy_five_percent_down():
    # start_of_day 1000, down 750 -> balance 250
    ctx = context(market=FakeMarket(balance=250.0), risk=FakeRisk(pnl=-750.0))
    t = run([DeadmanGate()], ctx)
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "deadman_halt"
    assert t.results[0].detail["drawdown_frac"] == 0.75


def test_deadman_ignores_a_winning_day():
    ctx = context(market=FakeMarket(balance=1500.0), risk=FakeRisk(pnl=500.0))
    assert run([DeadmanGate()], ctx).action is Verdict.ALLOW


def test_deadman_fails_closed_on_unknown_balance():
    """Deliberate deviation from v1, which traded on when balance was unreadable."""
    ctx = context(market=FakeMarket(balance=None))
    t = run([DeadmanGate()], ctx)
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "balance_unknown"


# ── stack ─────────────────────────────────────────────────────────────────────

def test_stack_under_limit_passes():
    t = run([StackLimitGate()], context(positions=FakePositions(4)))
    assert t.action is Verdict.ALLOW


def test_stack_at_limit_suppresses():
    t = run([StackLimitGate()], context(positions=FakePositions(5)))
    assert t.reason == "max_stack"


# ── batch one in sequence ─────────────────────────────────────────────────────

def batch_one():
    return [DirectionGate(), ConfidenceGate(), FeedGate(),
            SpreadHardGate(), DeadmanGate(), StackLimitGate()]


def test_full_batch_passes_a_clean_signal():
    ctx = context()
    t = run(batch_one(), ctx)
    assert t.action is Verdict.ALLOW
    assert t.executable
    assert ctx.derived["execution_price"] == 62860.0


def test_batch_records_shadow_objections():
    """Suppressed by confidence, but the record still shows spread also objected."""
    ctx = context(
        {"direction": "UP", "confidence": "low"},
        market=FakeMarket({"time": 1, "price": 62850.0, "ask": 62900.0, "bid": 62800.0}),
    )
    t = run(batch_one(), ctx)
    assert t.reason == "confidence_low"
    assert "spread_hard" in [r.name for r in t.shadow_objections()]
    # Expensive gates skip once bound: no broker calls were made.
    skipped = [r.name for r in t.results if r.status.value == "skipped"]
    assert "deadman" in skipped and "stack_limit" in skipped


def test_reject_stops_before_any_broker_call():
    ctx = context({"direction": "???", "confidence": "high"})
    t = run(batch_one(), ctx)
    assert t.action is Verdict.REJECT
    assert len(t.results) == 1
    assert not t.recordable


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
