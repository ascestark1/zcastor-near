"""
Batch two: zone, flip_confirm, coherence, session, regime_context, london_bias.

This is where the trading judgement lives, so the tests are mostly about the
exceptions — the cases where a gate deliberately does NOT fire.
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
from zcastor.gates.london import LondonBiasGate  # noqa: E402
from zcastor.gates.regime import RegimeContextGate  # noqa: E402
from zcastor.gates.session import SessionGate  # noqa: E402
from zcastor.gates.zone import FlipConfirmGate, ZoneGate  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeRisk:
    def __init__(self, zone=("fresh", 0, "ok"), coherence=("ok", ""),
                 model=("ok", 0.55, 40)):
        self._zone, self._coh, self._model = zone, coherence, model

    def check_zone(self, price, direction, timeframe):
        return self._zone

    def check_signal_coherence(self, direction, timeframe):
        return self._coh

    def check_model_coherence(self):
        return self._model


class FakeMarket:
    def __init__(self, session_open=0.0):
        self._open = session_open

    def session_open_price(self, session):
        return self._open


def at_hour(h):
    return lambda: datetime(2026, 8, 27, h, 0, tzinfo=timezone.utc)


def context(signal=None, risk=None, market=None, derived=None):
    ctx = GateContext(
        signal=signal if signal is not None else {},
        origin_hash="sha256:origin",
        policy=POLICY,
        market=market or FakeMarket(),
        risk=risk or FakeRisk(),
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


# ── zone ──────────────────────────────────────────────────────────────────────

def test_exhausted_zone_routes_rather_than_dies():
    ctx = context(risk=FakeRisk(zone=("blocked", 4, "4 entries in zone")))
    t = run([ZoneGate()], ctx)
    assert t.action is Verdict.ROUTE
    assert t.route_kind == "exhaustion"


def test_zone_gate_does_not_consult_a_watcher_flag():
    """v1 chose route-or-suppress from WATCHER_ENABLED. The gate states its
    judgement; the deployment decides whether it can be honoured."""
    ctx = context(risk=FakeRisk(zone=("blocked", 4, "spent")))
    t = run([ZoneGate()], ctx)
    assert t.action is Verdict.ROUTE, "must not depend on deployment config"


def test_elevated_zone_passes_as_the_flip_trade():
    ctx = context(risk=FakeRisk(zone=("elevated", 1, "opposite exhausted")))
    t = run([ZoneGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert ctx.derived["zone_status"] == "elevated"


def test_caution_zone_is_informational_only():
    ctx = context(risk=FakeRisk(zone=("caution", 3, "getting full")))
    assert run([ZoneGate()], ctx).action is Verdict.ALLOW


# ── flip confirmation ─────────────────────────────────────────────────────────

def test_flip_pending_without_confirmation_suppresses():
    ctx = context({"zone_flip_pending": True}, derived={"zone_status": "fresh"})
    t = run([FlipConfirmGate()], ctx)
    assert t.reason == "flip_pending_no_confirmation"


def test_flip_pending_with_elevated_zone_is_the_trade_we_want():
    ctx = context({"zone_flip_pending": True}, derived={"zone_status": "elevated"})
    assert run([FlipConfirmGate()], ctx).action is Verdict.ALLOW


def test_no_flip_pending_passes():
    assert run([FlipConfirmGate()], context({})).action is Verdict.ALLOW


# ── coherence ─────────────────────────────────────────────────────────────────

def test_coherence_caution_suppresses_without_a_flip():
    ctx = context(risk=FakeRisk(coherence=("caution", "UP then DOWN on 15m")))
    t = run([SignalCoherenceGate()], ctx)
    assert t.reason == "coherence_block"


def test_coherence_caution_overridden_by_confirmed_flip():
    """The reversal call is the highest-value setup; do not kill it."""
    ctx = context(
        {"zone_flip_pending": True},
        risk=FakeRisk(coherence=("caution", "contradiction")),
    )
    assert run([SignalCoherenceGate()], ctx).action is Verdict.ALLOW


def test_coherence_caution_overridden_by_elevated_zone():
    ctx = context(
        risk=FakeRisk(coherence=("caution", "contradiction")),
        derived={"zone_status": "elevated"},
    )
    assert run([SignalCoherenceGate()], ctx).action is Verdict.ALLOW


def test_model_coherence_never_binds():
    ctx = context(risk=FakeRisk(model=("caution", 0.20, 30)))
    t = run([ModelCoherenceGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert t.results[0].status.value == "advisory"
    assert t.results[0].detail["win_rate"] == 0.2


# ── session ───────────────────────────────────────────────────────────────────





def test_open_session_passes():
    assert run([SessionGate()], context({"session": "London"})).action is Verdict.ALLOW


# ── regime context ────────────────────────────────────────────────────────────

def ranging(tf, tf_regime="ranging", block_conf="low"):
    return {"regime": "ranging", "tf_regime": tf_regime,
            "timeframe": tf, "block_confidence": block_conf}


def test_trending_market_passes_any_timeframe():
    t = run([RegimeContextGate()], context({"regime": "trending", "timeframe": "5m"}))
    assert t.action is Verdict.ALLOW


def test_ranging_5m_routes_to_the_range_edge():
    t = run([RegimeContextGate()], context(ranging("5m")))
    assert t.action is Verdict.ROUTE
    assert t.route_kind == "fresh"


def test_ranging_1h_trades_at_market():
    assert run([RegimeContextGate()], context(ranging("1h"))).action is Verdict.ALLOW


def test_own_tf_directional_overrides_stale_global_ranging():
    """A stale 4H ranging read must not kill a live 15m move."""
    ctx = context(ranging("30m", tf_regime="trending"))
    t = run([RegimeContextGate()], ctx)
    assert t.action is Verdict.ALLOW
    assert t.results[0].detail["reason"] == "own_tf_directional_override"


def test_regime_aliases_normalise():
    ctx = context(ranging("5m", tf_regime="expansion"))
    assert run([RegimeContextGate()], ctx).action is Verdict.ALLOW


def test_strong_block_confidence_leaves_it_to_other_gates():
    ctx = context(ranging("5m", block_conf="high"))
    assert run([RegimeContextGate()], ctx).action is Verdict.ALLOW


# ── london bias ───────────────────────────────────────────────────────────────

def london_sell(tf_regime="trending", session="London"):
    return {"direction": "DOWN", "tf_regime": tf_regime, "session": session,
            "timeframe": "15m"}


def test_buys_are_unaffected():
    t = run([LondonBiasGate(now=at_hour(8))], context({"direction": "UP"}))
    assert t.action is Verdict.ALLOW


def test_sell_outside_the_window_passes():
    ctx = context(london_sell(), derived={"direction": "DOWN"})
    t = run([LondonBiasGate(now=at_hour(14))], ctx)
    assert t.action is Verdict.ALLOW


def test_sell_in_window_needs_both_confirmations():
    # Session flat: open equals execution price, so no down move.
    ctx = context(london_sell(), market=FakeMarket(session_open=62860.0),
                  derived={"direction": "DOWN"})
    t = run([LondonBiasGate(now=at_hour(8))], ctx)
    assert t.reason == "london_sell_bias"
    assert t.results[0].detail["intraday_down"] is False


def test_sell_with_down_move_but_ranging_own_tf_suppresses():
    ctx = context(london_sell(tf_regime="ranging"),
                  market=FakeMarket(session_open=63200.0),
                  derived={"direction": "DOWN"})
    t = run([LondonBiasGate(now=at_hour(8))], ctx)
    assert t.reason == "london_sell_bias"
    assert t.results[0].detail["own_tf_directional"] is False


def test_sell_with_both_confirmations_passes():
    ctx = context(london_sell(), market=FakeMarket(session_open=63200.0),
                  derived={"direction": "DOWN"})
    t = run([LondonBiasGate(now=at_hour(8))], ctx)
    assert t.action is Verdict.ALLOW
    assert t.results[0].detail["move_down_pts"] == 340


def test_down_move_must_clear_spread_noise():
    """Threshold is max(2x spread, 50pts). At 100pt spread that is 200pts."""
    ctx = context(london_sell(), market=FakeMarket(session_open=62960.0),
                  derived={"direction": "DOWN", "spread_pts": 100})
    t = run([LondonBiasGate(now=at_hour(8))], ctx)
    assert t.reason == "london_sell_bias", "100pts down is noise at a 100pt spread"
    assert t.results[0].detail["threshold_pts"] == 200


def test_london_gate_skipped_for_routed_signals():
    """The finding is about market SELLs into London strength. A routed entry
    is a different trade at a different price on confirmation."""
    ctx = context({**london_sell(), **ranging("5m")}, derived={"direction": "DOWN"})
    t = run([RegimeContextGate(), LondonBiasGate(now=at_hour(8))], ctx)
    assert t.action is Verdict.ROUTE
    assert [r.status.value for r in t.results if r.name == "london_bias"] == ["skipped"]


# ── batch two in sequence ─────────────────────────────────────────────────────

def batch_two(now_hour=14):
    return [ZoneGate(), FlipConfirmGate(), SignalCoherenceGate(),
            ModelCoherenceGate(), SessionGate(), RegimeContextGate(),
            LondonBiasGate(now=at_hour(now_hour))]


def test_clean_signal_passes_the_whole_batch():
    ctx = context({"session": "London", "regime": "trending", "timeframe": "15m"})
    assert run(batch_two(), ctx).action is Verdict.ALLOW


def test_exhausted_zone_routes_and_survives_the_rest_of_the_batch():
    ctx = context(
        {"session": "London", "regime": "trending", "timeframe": "5m"},
        risk=FakeRisk(zone=("blocked", 4, "spent")),
    )
    t = run(batch_two(), ctx)
    assert t.action is Verdict.ROUTE, "no later gate may quietly kill a routed signal"
    assert t.route_kind == "exhaustion"




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
