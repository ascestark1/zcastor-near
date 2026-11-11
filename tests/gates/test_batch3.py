"""
Batch three: targets, spread_viability, range_adequacy, risk_reward,
session_exhaustion, edge_ledger, viability.

Plus the full stack end to end — which is where the v1 route-guard bug is
finally proved dead against all three market-quality gates at once.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.gates.base import GateContext, Verdict  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.gates.coherence import ModelCoherenceGate, SignalCoherenceGate  # noqa: E402
from zcastor.gates.confidence import ConfidenceGate  # noqa: E402
from zcastor.gates.deadman import DeadmanGate  # noqa: E402
from zcastor.gates.direction import DirectionGate  # noqa: E402
from zcastor.gates.exhaustion import SessionExhaustionGate  # noqa: E402
from zcastor.gates.feed import FeedGate  # noqa: E402
from zcastor.gates.ledger import EdgeLedgerGate  # noqa: E402
from zcastor.gates.london import LondonBiasGate  # noqa: E402
from zcastor.gates.range import RangeAdequacyGate, RiskRewardGate  # noqa: E402
from zcastor.gates.regime import RegimeContextGate  # noqa: E402
from zcastor.gates.session import SessionGate  # noqa: E402
from zcastor.gates.spread import SpreadHardGate, SpreadViabilityGate  # noqa: E402
from zcastor.gates.stack import StackLimitGate  # noqa: E402
from zcastor.gates.targets import TargetStage  # noqa: E402
from zcastor.gates.viability import ViabilityGate  # noqa: E402
from zcastor.gates.zone import FlipConfirmGate, ZoneGate  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeTargets:
    def __init__(self, tp=63200.0, sl=62660.0):
        self._tp, self._sl = tp, sl

    def compute_tp(self, signal, fallback_price):
        return self._tp

    def compute_sl(self, signal, fallback_price):
        return self._sl


class FakeMarket:
    volume = 0.05

    def __init__(self, session_open=0.0, balance=1000.0, free_margin=800.0,
                 margin=100.0, tick=None):
        self._open, self._balance = session_open, balance
        self._free, self._margin = free_margin, margin
        self._tick = tick or {"time": 1_750_000_000, "price": 62850.0,
                              "ask": 62860.0, "bid": 62840.0}

    def get_tick(self):
        return self._tick

    def session_open_price(self, session):
        return self._open

    def get_balance(self):
        return self._balance

    def get_free_margin(self):
        return self._free

    def get_margin_for(self, direction, volume, price):
        return self._margin


class FakeRisk:
    def __init__(self, zone=("fresh", 0, "ok"), coherence=("ok", ""),
                 model=("ok", 0.55, 40), pnl=0.0):
        self._zone, self._coh, self._model, self._pnl = zone, coherence, model, pnl

    def check_zone(self, p, d, tf):
        return self._zone

    def check_signal_coherence(self, d, tf):
        return self._coh

    def check_model_coherence(self):
        return self._model

    def today_pnl(self):
        return self._pnl


class FakeLedger:
    def __init__(self, action="shadow", n=12):
        self._a, self._n = action, n

    def verdict(self, tf, session, route):
        return {"action": self._a, "n": self._n, "exp": 1.25,
                "lo": -0.40, "why": f"n={self._n} below 40"}


class FakePositions:
    def __init__(self, n=0):
        self._n = n

    def live(self):
        return [{"ticket": i} for i in range(self._n)]


def at_hour(h):
    return lambda: datetime(2026, 8, 27, h, 0, tzinfo=timezone.utc)


def context(signal=None, market=None, risk=None, targets=None, ledger=None,
            positions=None, derived=None):
    ctx = GateContext(
        signal=signal if signal is not None else {},
        origin_hash="sha256:origin",
        policy=POLICY,
        market=market or FakeMarket(),
        risk=risk or FakeRisk(),
        positions=positions or FakePositions(),
        ledger=ledger or FakeLedger(),
        targets=targets or FakeTargets(),
    )

    ctx.derived.update({
        "direction": "UP", "is_buy": True, "mid": 62850.0,
        "execution_price": 62860.0, "spread_pts": 20,
    })
    if derived:
        ctx.derived.update(derived)
    return ctx


def run(gates, ctx):
    return GatePipeline(gates).run(ctx, "sig_test", "sha256:policy")


# ── targets ───────────────────────────────────────────────────────────────────

def test_targets_measured_from_execution_price_not_mid():
    ctx = context()
    run([TargetStage()], ctx)
    assert ctx.derived["tp_pts"] == 340.0   # 63200 - 62860 (ask), not 62850
    assert ctx.derived["sl_pts"] == 200.0


def test_uncomputable_targets_reject():
    t = run([TargetStage()], context(targets=FakeTargets(tp=None)))
    assert t.action is Verdict.REJECT
    assert not t.recordable


def test_zero_stop_distance_rejects():
    t = run([TargetStage()], context(targets=FakeTargets(sl=62860.0)))
    assert t.reason == "stop_distance_zero"


# ── spread viability ──────────────────────────────────────────────────────────

def test_spread_cheap_against_a_wide_target():
    t = run([SpreadViabilityGate()], context(derived={"tp_pts": 1000.0}))
    assert t.action is Verdict.ALLOW


def test_same_cost_fatal_against_a_tight_target():
    """50pts is 5% of 1000 and 58% of 86. The absolute number is the wrong question."""
    ctx = context(derived={"spread_pts": 50, "cost_pts": 50, "tp_pts": 86.0})
    t = run([SpreadViabilityGate()], ctx)
    assert t.reason == "cost_too_high_for_target"
    assert t.results[0].detail["ratio"] == 0.581


def test_commission_counts_even_when_the_spread_is_tiny():
    """
    A venue quoting 0.01 and charging 0.1% per side is not cheap. Judging it on
    spread alone would pass a trade whose fees exceed a third of the target.
    """
    ctx = context(derived={"spread_pts": 0.01, "cost_pts": 160.0, "tp_pts": 400.0})
    t = run([SpreadViabilityGate()], ctx)
    assert t.reason == "cost_too_high_for_target"
    assert t.results[0].detail["commission_pts"] == 0


def test_cost_falls_back_to_spread_when_no_commission_is_configured():
    ctx = context(derived={"spread_pts": 20, "tp_pts": 1000.0})
    assert run([SpreadViabilityGate()], ctx).action is Verdict.ALLOW


# ── range adequacy ────────────────────────────────────────────────────────────

def test_range_below_stop_suppresses():
    ctx = context({"nearest_resistance_pts": 150},
                  derived={"sl_pts": 200.0})
    t = run([RangeAdequacyGate()], ctx)
    assert t.reason == "range_below_sl"


def test_ample_range_passes():
    ctx = context({"nearest_resistance_pts": 600}, derived={"sl_pts": 200.0})
    assert run([RangeAdequacyGate()], ctx).action is Verdict.ALLOW


def test_missing_level_means_unknown_not_zero():
    """The 15 Jun crash: an UP signal with no resistance passed float(None)."""
    ctx = context({}, derived={"sl_pts": 200.0})
    t = run([RangeAdequacyGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert t.results[0].detail["reason"] == "level_unknown"


def test_unparseable_level_does_not_crash():
    ctx = context({"nearest_resistance_pts": "n/a"}, derived={"sl_pts": 200.0})
    t = run([RangeAdequacyGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert t.results[0].detail["reason"] == "level_unparseable"


def test_sell_reads_the_support_level():
    ctx = context({"nearest_support_pts": 150, "nearest_resistance_pts": 9999},
                  derived={"direction": "DOWN", "sl_pts": 200.0})
    assert run([RangeAdequacyGate()], ctx).reason == "range_below_sl"


# ── risk reward ───────────────────────────────────────────────────────────────

def test_rr_below_one_suppresses():
    ctx = context(derived={"tp_pts": 150.0, "sl_pts": 200.0})
    t = run([RiskRewardGate()], ctx)
    assert t.reason == "insufficient_rr"
    assert t.results[0].detail["rr"] == 0.75


def test_rr_above_one_passes():
    ctx = context(derived={"tp_pts": 340.0, "sl_pts": 200.0})
    assert run([RiskRewardGate()], ctx).action is Verdict.ALLOW


# ── session exhaustion ────────────────────────────────────────────────────────

def test_spent_session_suppresses():
    # London typical 1000, frac 0.6 -> threshold 600. Moved 660 up.
    ctx = context({"session": "London"}, market=FakeMarket(session_open=62200.0))
    t = run([SessionExhaustionGate()], ctx)
    assert t.reason == "session_exhausted"
    assert t.results[0].detail["in_dir_move_pts"] == 660


def test_fresh_session_passes():
    ctx = context({"session": "London"}, market=FakeMarket(session_open=62700.0))
    assert run([SessionExhaustionGate()], ctx).action is Verdict.ALLOW


def test_move_against_the_signal_is_not_exhaustion():
    """Price down 660 on an UP signal is opportunity, not a spent move."""
    ctx = context({"session": "London"}, market=FakeMarket(session_open=63520.0))
    assert run([SessionExhaustionGate()], ctx).action is Verdict.ALLOW


def test_unknown_session_skips_the_gate():
    ctx = context({"session": "Narnia"}, market=FakeMarket(session_open=62200.0))
    assert run([SessionExhaustionGate()], ctx).action is Verdict.ALLOW


# ── edge ledger ───────────────────────────────────────────────────────────────

def test_ledger_is_advisory_by_default():
    t = run([EdgeLedgerGate(enforce=False)], context({"timeframe": "15m"}))
    assert t.action is Verdict.ALLOW
    assert t.results[0].status.value == "advisory"
    assert t.results[0].detail["n"] == 12


def test_ledger_binds_when_enforcing():
    t = run([EdgeLedgerGate(enforce=True)], context({"timeframe": "15m"}))
    assert t.reason == "ledger_shadow"


def test_graduated_segment_passes_under_enforcement():
    t = run([EdgeLedgerGate(enforce=True)],
            context({"timeframe": "15m"}, ledger=FakeLedger(action="trade", n=52)))
    assert t.action is Verdict.ALLOW


# ── viability ─────────────────────────────────────────────────────────────────

def test_viable_trade_passes():
    ctx = context(derived={"sl_pts": 200.0})   # 200 * 0.05 = $10 on $1000 = 1%
    assert run([ViabilityGate()], ctx).action is Verdict.ALLOW


def test_risk_above_cap_suppresses():
    # 8000pts * 0.05 = $400 = 40% of $1000, cap is 30%
    ctx = context(derived={"sl_pts": 8000.0})
    t = run([ViabilityGate()], ctx)
    assert t.reason == "risk_above_cap"


def test_insufficient_margin_suppresses():
    ctx = context(market=FakeMarket(free_margin=50.0, margin=100.0),
                  derived={"sl_pts": 200.0})
    assert run([ViabilityGate()], ctx).reason == "insufficient_margin"


def test_viability_fails_closed_on_unreadable_balance():
    ctx = context(market=FakeMarket(balance=0), derived={"sl_pts": 200.0})
    assert run([ViabilityGate()], ctx).reason == "balance_unavailable"


# ── the full stack ────────────────────────────────────────────────────────────

def full_stack(now_hour=14, enforce_ledger=False):
    return [
        DirectionGate(), ConfidenceGate(), FeedGate(), SpreadHardGate(),
        DeadmanGate(), StackLimitGate(),
        ZoneGate(), FlipConfirmGate(), SignalCoherenceGate(),
        ModelCoherenceGate(), SessionGate(), RegimeContextGate(),
        LondonBiasGate(now=at_hour(now_hour)),
        TargetStage(),
        SpreadViabilityGate(), RangeAdequacyGate(), RiskRewardGate(),
        SessionExhaustionGate(), EdgeLedgerGate(enforce=enforce_ledger),
        ViabilityGate(),
    ]


def clean_signal(**over):
    base = {
        "direction": "UP", "confidence": "high", "timeframe": "15m",
        "session": "London", "regime": "trending", "tf_regime": "trending",
        "nearest_resistance_pts": 600, "block_confidence": "high",
    }
    base.update(over)
    return base


def fresh_ctx(signal=None, **kw):
    ctx = context(signal if signal is not None else clean_signal(),
                  market=FakeMarket(session_open=62700.0), **kw)
    ctx.derived.clear()          # let the stack derive everything itself
    return ctx


def test_full_stack_is_nineteen_gates_plus_one_stage():
    assert len(full_stack()) == 20


def test_clean_signal_survives_the_whole_stack():
    t = run(full_stack(), fresh_ctx())
    assert t.action is Verdict.ALLOW, t.reason
    assert t.executable


def test_routed_signal_survives_all_three_market_quality_gates():
    """
    THE v1 BUG, at full scale.

    A ranging 5m signal routes to the watcher. Its market stop is 200pts against
    only 150pts of room and an R:R of 0.44 — v1 would have killed it on
    range_below_sl, judging a structural entry by a market stop it would never
    use. All three market-only gates must be skipped.
    """
    ctx = fresh_ctx(clean_signal(
        timeframe="5m", regime="ranging", tf_regime="ranging",
        block_confidence="low", nearest_resistance_pts=150,
    ), targets=FakeTargets(tp=62948.0, sl=62660.0))   # tp 88pts, sl 200pts

    t = run(full_stack(), ctx)

    assert t.action is Verdict.ROUTE
    assert t.route_kind == "fresh"
    skipped = {r.name for r in t.results if r.status.value == "skipped"}
    assert {"spread_viability", "range_adequacy", "risk_reward",
            "session_exhaustion", "viability", "london_bias"} <= skipped


def test_market_signal_with_the_same_numbers_is_correctly_refused():
    """The mirror image: unrouted, those gates apply and should bite."""
    ctx = fresh_ctx(clean_signal(nearest_resistance_pts=150),
                    targets=FakeTargets(tp=62948.0, sl=62660.0))
    t = run(full_stack(), ctx)
    assert t.action is Verdict.SUPPRESS
    # 20pt spread on an 88pt target is 23%, under the 30% ceiling — so the
    # spread gate passes and range adequacy is what bites: 150pts of room
    # against a 200pt stop.
    assert t.reason == "range_below_sl"
    assert "risk_reward" in {r.name for r in t.shadow_objections()}


def test_trace_carries_every_gate_for_the_record():
    t = run(full_stack(), fresh_ctx())
    d = t.to_dict()
    assert len(d["gates"]) == 20
    assert d["policy_hash"] == "sha256:policy"
    assert all(g["status"] in
               ("passed", "binding", "shadow", "skipped", "advisory")
               for g in d["gates"])


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
