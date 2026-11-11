"""
Adapter tests: target optimiser, zone tracker, coherence, edge ledger, journal.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.data.journal import Journal  # noqa: E402
from zcastor.data.ledger import EdgeLedger  # noqa: E402
from zcastor.risk.coherence import (  # noqa: E402
    ModelCoherence, RiskAdapter, SignalCoherence,
)
from zcastor.risk.zones import ZoneTracker  # noqa: E402
from zcastor.targets.optimizer import TargetOptimizer, normalise_tf  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def signal(**over):
    s = {"direction": "UP", "timeframe": "15m", "session": "London",
         "regime": "trending", "zone_phase": "fresh", "atr": 200}
    s.update(over)
    return s


# ── target optimiser ──────────────────────────────────────────────────────────

def opt():
    return TargetOptimizer(POLICY)


def test_timeframe_aliases_normalise():
    assert normalise_tf("60") == "1h" and normalise_tf("240m") == "4h"
    assert normalise_tf("nonsense") == "1h"


def test_stop_stays_inside_timeframe_bounds():
    """A 5m stop of 400pts is not a 5m trade, whatever ATR says."""
    o = opt()
    for tf, (lo, hi) in POLICY["targets"]["tf_sl_range_pts"].items():
        for atr in (10, 200, 5000):
            stop = o.sl_pts(signal(timeframe=tf, atr=atr))
            assert lo <= stop <= hi, f"{tf} atr={atr} -> {stop}"


def test_exhausting_phase_tightens_the_stop():
    o = opt()
    fresh = o.sl_pts(signal(zone_phase="fresh"))
    spent = o.sl_pts(signal(zone_phase="exhausting"))
    assert spent < fresh




def test_the_floor_dominates_at_realistic_atr():
    """At the ATR this instrument actually shows, every stop sits at the floor."""
    o = opt()
    lo, _ = POLICY["targets"]["tf_sl_range_pts"]["15m"]
    for atr in (100, 150, 200, 300):
        for regime in ("volatile", "ranging", "trending"):
            assert o.sl_pts(signal(regime=regime, atr=atr)) >= lo


def test_opposing_level_anchors_the_stop_when_inside_bounds():
    """Bounds come from policy, so widening stops cannot silently break this."""
    o = opt()
    lo, hi = POLICY["targets"]["tf_sl_range_pts"]["15m"]
    inside = (lo + hi) / 2 - o.buffer
    plain = o.sl_pts(signal())
    anchored = o.sl_pts(signal(nearest_support_pts=inside))
    assert anchored != plain
    assert lo <= anchored <= hi


def test_opposing_level_outside_bounds_is_ignored():
    o = opt()
    plain = o.sl_pts(signal())
    absurd = o.sl_pts(signal(nearest_support_pts=5000))
    assert absurd == plain


def test_missing_level_never_becomes_zero():
    """The 15 Jun crash: an UP signal with no support passed float(None)."""
    o = opt()
    for value in (None, "", "n/a", 0, -5):
        stop = o.sl_pts(signal(nearest_support_pts=value))
        assert stop > 0


def test_sl_direction_is_below_for_buys_above_for_sells():
    o = opt()
    assert o.compute_sl(signal(), 60000.0) < 60000.0
    assert o.compute_sl(signal(direction="DOWN"), 60000.0) > 60000.0


def test_no_price_returns_none_rather_than_a_guess():
    o = opt()
    assert o.compute_sl(signal(), 0) is None
    assert o.compute_tp(signal(), 0) is None
    assert o.compute_tp(signal(direction="SIDEWAYS"), 60000.0) is None


def test_target_respects_minimum_risk_reward():
    o = opt()
    for tf in ("5m", "15m", "1h", "4h"):
        s = signal(timeframe=tf)
        stop = o.sl_pts(s)
        tp = o.compute_tp(s, 60000.0)
        assert (tp - 60000.0) >= stop * POLICY["targets"]["min_risk_reward"] - 0.01


def test_target_respects_the_timeframe_ceiling():
    o = opt()
    tp = o.compute_tp(signal(timeframe="5m", nearest_resistance_pts=9999), 60000.0)
    assert (tp - 60000.0) <= POLICY["targets"]["tf_tp_ceiling_pts"]["5m"]


def test_structure_anchors_the_target_when_it_clears_min_rr():
    o = opt()
    lo, _ = POLICY["targets"]["tf_sl_range_pts"]["15m"]
    level = lo * POLICY["targets"]["min_risk_reward"] + o.buffer + 200
    tp = o.compute_tp(signal(timeframe="15m",
                             nearest_resistance_pts=level), 60000.0)
    assert abs((tp - 60000.0) - (level - o.buffer)) < 1.0


def test_level_too_close_falls_back_to_session_rr():
    """A level nearer than min R:R is an obstacle, not a target."""
    o = opt()
    s = signal(timeframe="15m", nearest_resistance_pts=60)
    tp_pts = o.compute_tp(s, 60000.0) - 60000.0
    assert tp_pts > 60


def test_ranging_caps_risk_reward_on_fast_timeframes():
    o = opt()
    s = signal(timeframe="5m", regime="ranging", nearest_resistance_pts=9999)
    stop = o.sl_pts(s)
    rr = (o.compute_tp(s, 60000.0) - 60000.0) / stop
    assert rr <= POLICY["targets"]["regime_rr_cap"]["ranging"] + 0.01


def test_ranging_cap_does_not_apply_to_slow_timeframes():
    o = opt()
    s = signal(timeframe="1h", regime="ranging", nearest_resistance_pts=9999)
    rr = (o.compute_tp(s, 60000.0) - 60000.0) / o.sl_pts(s)
    assert rr > POLICY["targets"]["regime_rr_cap"]["ranging"]


def test_sell_target_is_below_entry():
    o = opt()
    assert opt().compute_tp(signal(direction="DOWN"), 60000.0) < 60000.0


def test_explain_describes_the_derivation():
    e = opt().explain(signal(), 60000.0)
    assert e["timeframe"] == "15m" and e["phase"] == "fresh"
    assert e["risk_reward"] >= 1.5


# ── zones ─────────────────────────────────────────────────────────────────────

def zones(clock=None):
    return ZoneTracker(POLICY, clock=clock or Clock())


def test_fresh_zone_is_ok():
    assert zones().check(60000.0, "up", "15m")[0] == "ok"


def test_zone_blocks_after_four_entries():
    z = zones()
    for _ in range(4):
        z.record_entry(60000.0, "up", "15m", bounce_pts=200)
    status, n, _ = z.check(60000.0, "up", "15m")
    assert status == "blocked" and n == 4




def test_entries_expire_out_of_the_window():
    clock = Clock()
    z = zones(clock)
    for _ in range(4):
        z.record_entry(60000.0, "up", "15m", bounce_pts=200)
    clock.advance(7201)
    assert z.check(60000.0, "up", "15m")[0] == "ok"




def test_higher_timeframe_suppresses_lower():
    z = zones()
    for _ in range(4):
        z.record_entry(60000.0, "up", "1h", bounce_pts=200)
    status, _, reason = z.check(60000.0, "up", "15m")
    assert status == "blocked" and "1h exhausted" in reason


def test_lower_timeframe_does_not_suppress_higher():
    z = zones()
    for _ in range(4):
        z.record_entry(60000.0, "up", "5m", bounce_pts=200)
    assert z.check(60000.0, "up", "4h")[0] == "ok"


def test_opposite_exhaustion_marks_this_direction_as_the_flip():
    z = zones()
    for _ in range(4):
        z.record_entry(60000.0, "down", "15m", bounce_pts=200)
    status, _, reason = z.check(60000.0, "up", "15m")
    assert status == "elevated" and "flip" in reason


def test_weak_bounces_downgrade_a_fresh_zone():
    z = zones()
    z.record_entry(60000.0, "up", "15m", bounce_pts=20)
    status, _, reason = z.check(60000.0, "up", "15m")
    assert status == "caution" and "weak bounces" in reason


def test_zones_are_bucketed_by_price():
    z = zones()
    for _ in range(4):
        z.record_entry(60000.0, "up", "15m", bounce_pts=200)
    assert z.check(60000.0, "up", "15m")[0] == "blocked"
    assert z.check(65000.0, "up", "15m")[0] == "ok"


def test_reset_clears_the_whole_bucket():
    z = zones()
    for _ in range(4):
        z.record_entry(60000.0, "up", "15m", bounce_pts=200)
    assert z.reset_zone(60000.0) > 0
    assert z.check(60000.0, "up", "15m")[0] == "ok"




# ── coherence ─────────────────────────────────────────────────────────────────

def test_signal_coherence_flags_oscillation():
    c = SignalCoherence(POLICY)
    c.record("down", "15m")
    c.record("down", "15m")
    assert c.check("up", "15m")[0] == "caution"


def test_signal_coherence_ignores_other_timeframes():
    c = SignalCoherence(POLICY)
    c.record("down", "1h")
    c.record("down", "1h")
    assert c.check("up", "15m")[0] == "ok"


def test_model_coherence_needs_a_minimum_sample():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "outcomes.json"
        p.write_text(json.dumps([{"correct": False}] * 3))
        status, wr, n = ModelCoherence(POLICY, p).check()
        assert status == "ok" and n == 3


def test_model_coherence_flags_a_poor_run():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "outcomes.json"
        p.write_text(json.dumps([{"correct": False}] * 8 + [{"correct": True}] * 2))
        status, wr, n = ModelCoherence(POLICY, p).check()
        assert status == "caution" and wr == 0.2


def test_model_coherence_survives_a_missing_file():
    status, wr, n = ModelCoherence(POLICY, "/nonexistent/x.json").check()
    assert status == "ok" and n == 0


def test_risk_adapter_exposes_only_the_four_gate_methods():
    with tempfile.TemporaryDirectory() as d:
        adapter = RiskAdapter(
            zones=zones(),
            signal_coherence=SignalCoherence(POLICY),
            model_coherence=ModelCoherence(POLICY, Path(d) / "x.json"),
            journal=Journal(Path(d) / "trades.jsonl"),
        )
        assert adapter.check_zone(60000.0, "up", "15m")[0] == "ok"
        assert adapter.check_signal_coherence("up", "15m")[0] == "ok"
        assert adapter.check_model_coherence()[0] == "ok"
        assert adapter.today_pnl() == 0.0


# ── edge ledger ───────────────────────────────────────────────────────────────

def ledger(tmp, clock=None):
    return EdgeLedger(POLICY, Path(tmp) / "edge.jsonl", clock=clock or Clock())


def test_empty_segment_is_shadow():
    with tempfile.TemporaryDirectory() as d:
        v = ledger(d).verdict("15m", "London", "market")
        assert v["action"] == "shadow" and v["why"] == "no data"




def test_consistent_edge_graduates():
    with tempfile.TemporaryDirectory() as d:
        led = ledger(d)
        for i in range(50):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.0 + (0.1 if i % 2 else -0.1))
        v = led.verdict("15m", "London", "market")
        assert v["action"] == "execute" and v["lo"] > 0


def test_high_variance_does_not_graduate():
    """A good mean with wide spread is not an edge."""
    with tempfile.TemporaryDirectory() as d:
        led = ledger(d)
        for i in range(50):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=10.0 if i % 5 == 0 else -1.5)
        v = led.verdict("15m", "London", "market")
        assert v["action"] == "shadow"


def test_decayed_edge_is_demoted():
    with tempfile.TemporaryDirectory() as d:
        led = ledger(d)
        for _ in range(45):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=2.0)
        for _ in range(20):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=-1.0)
        v = led.verdict("15m", "London", "market")
        assert v["action"] == "shadow" and "decayed" in v["why"]


def test_one_lucky_trade_never_graduates_a_segment():
    with tempfile.TemporaryDirectory() as d:
        led = ledger(d)
        led.record(timeframe="4h", session="Asian", route="market", r_multiple=9.0)
        assert led.verdict("4h", "Asian", "market")["lo"] < 0


def test_segments_are_independent():
    with tempfile.TemporaryDirectory() as d:
        led = ledger(d)
        for _ in range(50):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.0)
        assert led.verdict("15m", "London", "watcher")["n"] == 0


def test_ledger_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "edge.jsonl"
        led = EdgeLedger(POLICY, path, clock=Clock())
        for _ in range(10):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.0)
        assert EdgeLedger(POLICY, path, clock=Clock()).verdict(
            "15m", "London", "market")["n"] == 10


def test_old_records_fall_out_of_the_window():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        led = ledger(d, clock)
        for _ in range(50):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.0)
        clock.advance(31 * 86400)
        assert led.verdict("15m", "London", "market")["n"] == 0


# ── journal ───────────────────────────────────────────────────────────────────

def journal(tmp):
    return Journal(Path(tmp) / "trades.jsonl")


def open_one(j, ticket=1):
    j.open_trade(ticket=ticket, signal_id=f"sig_{ticket}", direction="UP",
                 timeframe="15m", session="London", route="market",
                 entry_price=60000.0, sl=59900.0, tp=60300.0, volume=0.05)


def test_open_then_close():
    with tempfile.TemporaryDirectory() as d:
        j = journal(d)
        open_one(j)
        assert j.open_tickets() == [1]
        assert j.close_trade(ticket=1, pnl=12.5, close_price=60250.0)
        assert j.get(1)["state"] == "closed" and j.get(1)["win"] is True


def test_double_close_is_refused():
    """Double-booking corrupts the drawdown gate and the edge ledger at once."""
    with tempfile.TemporaryDirectory() as d:
        j = journal(d)
        open_one(j)
        assert j.close_trade(ticket=1, pnl=12.5, close_price=60250.0)
        assert not j.close_trade(ticket=1, pnl=12.5, close_price=60250.0)
        assert j.stats()["net_pnl"] == 12.5


def test_close_for_unknown_ticket_is_refused():
    with tempfile.TemporaryDirectory() as d:
        assert not journal(d).close_trade(ticket=999, pnl=1.0, close_price=1.0)


def test_journal_survives_a_restart_with_state():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "trades.jsonl"
        j = Journal(path)
        j.open_trade(ticket=1, signal_id="s", direction="UP", timeframe="15m",
                     session="London", route="market", entry_price=60000.0,
                     sl=1.0, tp=2.0, volume=0.05)
        j.close_trade(ticket=1, pnl=-5.0, close_price=59900.0)

        reopened = Journal(path)
        assert reopened.get(1)["state"] == "closed"
        assert reopened.get(1)["pnl"] == -5.0
        assert not reopened.close_trade(ticket=1, pnl=-5.0, close_price=1.0)


def test_today_pnl_counts_closed_trades_only():
    with tempfile.TemporaryDirectory() as d:
        j = journal(d)
        open_one(j, 1)
        open_one(j, 2)
        j.close_trade(ticket=1, pnl=-8.0, close_price=59900.0)
        assert j.today_pnl() == -8.0


def test_stats_summarise_the_book():
    with tempfile.TemporaryDirectory() as d:
        j = journal(d)
        for t in (1, 2, 3):
            open_one(j, t)
        j.close_trade(ticket=1, pnl=10.0, close_price=1.0)
        j.close_trade(ticket=2, pnl=-4.0, close_price=1.0)
        s = j.stats()
        assert s == {"total": 3, "open": 1, "closed": 2, "wins": 1,
                     "win_rate": 0.5, "net_pnl": 6.0}


# ── policy scoping ────────────────────────────────────────────────────────────

def scoped(tmp, policy_hash):
    return EdgeLedger(POLICY, Path(tmp) / "edge.jsonl", clock=Clock(),
                      policy_hash=policy_hash)


def test_a_policy_change_resets_the_evidence():
    """
    R-multiples normalise for size but not for geometry. A 15m stop moving from
    115pts to 500 changes spread from a third of the risk to a tenth, and the
    same signal produces a different hit-rate distribution. Pooling them would
    give a number describing neither.
    """
    with tempfile.TemporaryDirectory() as d:
        old = scoped(d, "sha256:OLD")
        for _ in range(45):
            old.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.2)
        assert old.verdict("15m", "London", "market")["action"] == "execute"

        new = scoped(d, "sha256:NEW")
        v = new.verdict("15m", "London", "market")
        assert v["action"] == "shadow" and v["n"] == 0


def test_out_of_scope_evidence_is_reported_not_hidden():
    """'No data' and 'data under a superseded policy' are different claims."""
    with tempfile.TemporaryDirectory() as d:
        old = scoped(d, "sha256:OLD")
        for _ in range(10):
            old.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.0)
        new = scoped(d, "sha256:NEW")
        assert "under earlier" in new.verdict("15m", "London", "market")["why"]
        assert new.stats()["out_of_scope"] == 10


def test_a_genuinely_new_segment_says_no_data():
    with tempfile.TemporaryDirectory() as d:
        led = scoped(d, "sha256:NEW")
        assert led.verdict("4h", "Asian", "market")["why"] == "no data"


def test_records_under_the_current_policy_still_count():
    with tempfile.TemporaryDirectory() as d:
        led = scoped(d, "sha256:NEW")
        for i in range(50):
            led.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.0 + (0.1 if i % 2 else -0.1))
        assert led.verdict("15m", "London", "market")["action"] == "execute"


def test_an_unscoped_ledger_counts_everything():
    """Tests and one-off analysis; never how the engine runs."""
    with tempfile.TemporaryDirectory() as d:
        old = scoped(d, "sha256:OLD")
        for _ in range(45):
            old.record(timeframe="15m", session="London", route="market",
                       r_multiple=1.2)
        pooled = EdgeLedger(POLICY, Path(d) / "edge.jsonl", clock=Clock())
        assert pooled.verdict("15m", "London", "market")["n"] == 45


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
