"""
Resolver tests.

The two that carry the most weight are test_same_bar_stop_and_target_scores_the_stop
(assuming the good one turns a coin flip into free money on paper) and
test_refusals_are_measured_too (without it, "the system declines a lot" is an
assertion rather than a finding).
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.resolve.classify import classify, horizon_minutes  # noqa: E402
from zcastor.resolve.resolver import OutcomeResolver  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())
H15 = POLICY["resolver"]["horizon_min_by_tf"]["15m"]
GIVE_UP = POLICY["resolver"]["give_up_hours"]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeMarket:
    volume = 0.05

    def __init__(self, rates=None, raises=False):
        self._rates = rates if rates is not None else []
        self._raises = raises

    def get_rates_range(self, frm, to):
        if self._raises:
            raise ConnectionError("bridge down")
        return list(self._rates)


def bar(high, low, close=None):
    return {"high": high, "low": low, "close": close if close is not None else low}


def flat(price, n=5):
    return [bar(price, price, price) for _ in range(n)]


# ── classification ────────────────────────────────────────────────────────────

def test_no_bars_means_cannot_say():
    """None must never be read as a loss — it is the resolver failing, not the signal."""
    assert classify(ref_price=60000.0, direction="UP", rates=[]) is None


def test_buy_hits_target():
    v = classify(ref_price=60000.0, direction="UP",
                 rates=[bar(60400, 59990, 60400)],
                 sl_pts=200, tp_pts=300, spread_pts=40)
    assert v["outcome"] == "tp" and v["won"] and v["net_pts"] == 300


def test_buy_hits_stop():
    v = classify(ref_price=60000.0, direction="UP",
                 rates=[bar(60100, 59800, 59850)],
                 sl_pts=200, tp_pts=300, spread_pts=40)
    assert v["outcome"] == "sl" and not v["won"] and v["net_pts"] == -200


def test_same_bar_stop_and_target_scores_the_stop():
    """M1 bars do not say which came first. Assuming the good one is free money."""
    v = classify(ref_price=60000.0, direction="UP",
                 rates=[bar(60500, 59700, 60400)],
                 sl_pts=200, tp_pts=300, spread_pts=40)
    assert v["outcome"] == "sl"


def test_buy_entry_pays_the_spread():
    """Entry is the ask. Ignoring this flatters every result by half a spread."""
    near = classify(ref_price=60000.0, direction="UP",
                    rates=[bar(60295, 59990, 60295)],
                    sl_pts=200, tp_pts=300, spread_pts=40)
    assert near["outcome"] == "timeout", "60295 is short of the 60340 target"


def test_sell_pays_the_spread_on_exit():
    v = classify(ref_price=60000.0, direction="DOWN",
                 rates=[bar(60010, 59650, 59660)],
                 sl_pts=200, tp_pts=300, spread_pts=40)
    assert v["outcome"] == "tp"


def test_timeout_is_scored_close_to_close():
    v = classify(ref_price=60000.0, direction="UP",
                 rates=[bar(60100, 59950, 60080)],
                 sl_pts=500, tp_pts=800, spread_pts=40)
    assert v["outcome"] == "timeout"
    assert v["net_pts"] == 40.0          # 60080 - (60000 + 40)


def test_missing_target_is_derived_from_the_stop():
    """Scoring a stopped-out trade by where price ended measures a trade nobody held."""
    v = classify(ref_price=60000.0, direction="UP",
                 rates=[bar(60300, 59990, 60300)],
                 sl_pts=200, tp_pts=0, spread_pts=0, default_rr=1.3333)
    assert v["outcome"] == "tp" and v["net_pts"] == 267


def test_direction_and_geometry_are_scored_separately():
    """Stopped out, but the direction was right. Two different lessons."""
    v = classify(ref_price=60000.0, direction="UP",
                 rates=[bar(60100, 59700, 60250)],
                 sl_pts=200, tp_pts=600, spread_pts=0)
    assert v["outcome"] == "sl" and not v["won"]
    assert v["correct"] is True


def test_excursions_are_measured():
    v = classify(ref_price=60000.0, direction="UP",
                 rates=[bar(60500, 59600, 60100)], sl_pts=200, tp_pts=800,
                 spread_pts=0)
    assert v["mfe_pts"] == 500.0 and v["mae_pts"] == 400.0


def test_horizon_is_per_timeframe():
    table = POLICY["resolver"]["horizon_min_by_tf"]
    assert horizon_minutes(POLICY, "5m") == table["5m"]
    assert horizon_minutes(POLICY, "4h") == table["4h"]
    assert horizon_minutes(POLICY, "unknown") == \
        POLICY["resolver"]["horizon_min_default"]


# ── resolver ──────────────────────────────────────────────────────────────────

def resolver(tmp, market=None, clock=None, on_resolved=None):
    tmp = Path(tmp)
    return OutcomeResolver(
        policy=POLICY, market=market or FakeMarket(),
        pending_path=tmp / "pending.json",
        outcomes_path=tmp / "outcomes.json",
        on_resolved=on_resolved, clock=clock or Clock(),
    )


def register(r, signal_id="sig_1", action="allow", direction="UP"):
    r.register(signal_id=signal_id, direction=direction, ref_price=60000.0,
               timeframe="15m", session="London", action=action,
               sl_pts=200, tp_pts=300, spread_pts=40)


def test_registration_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        r = resolver(d)
        register(r)
        assert resolver(d).pending_count() == 1


def test_nothing_resolves_before_its_horizon():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket(flat(60400)), clock)
        register(r)
        assert r.tick()["resolved"] == 0
        clock.advance((H15 - 1) * 60)
        assert r.tick()["resolved"] == 0
        clock.advance(2 * 60)
        assert r.tick()["resolved"] == 1


def test_a_resolved_outcome_is_written():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket([bar(60400, 59990, 60400)]), clock)
        register(r)
        clock.advance((H15 + 1) * 60)
        r.tick()
        outcomes = json.loads((Path(d) / "outcomes.json").read_text())
        assert outcomes[0]["signal_id"] == "sig_1"
        assert outcomes[0]["outcome"] == "tp"
        assert r.pending_count() == 0


def test_unresolvable_signals_stay_pending():
    """A dead bridge must not be recorded as a wrong prediction."""
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket(raises=True), clock)
        register(r)
        clock.advance((H15 + 1) * 60)
        result = r.tick()
        assert result["stalled"] == 1 and result["resolved"] == 0
        assert r.pending_count() == 1


def test_empty_rates_stay_pending():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket([]), clock)
        register(r)
        clock.advance((H15 + 1) * 60)
        assert r.tick()["stalled"] == 1


def test_a_signal_is_abandoned_after_the_give_up_window():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket(raises=True), clock)
        register(r)
        clock.advance((GIVE_UP + 1) * 3600)
        assert r.tick()["abandoned"] == 1
        assert r.pending_count() == 0


def test_duplicate_registration_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        r = resolver(d)
        register(r)
        register(r)
        assert r.pending_count() == 1


def test_registration_never_raises():
    with tempfile.TemporaryDirectory() as d:
        r = resolver(d)
        r.register(signal_id="sig_bad", direction="UP", ref_price=0,
                   timeframe="15m")
        assert r.pending_count() == 0


def test_refusals_are_measured_too():
    """
    The counterfactual. Without measuring what we declined, 'the system says no
    a lot' is an assertion rather than a finding.
    """
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        # Refused an UP signal; price then fell — the refusal was right.
        r = resolver(d, FakeMarket([bar(60010, 59700, 59750)]), clock)
        register(r, "sig_refused", action="suppress")
        clock.advance((H15 + 1) * 60)
        r.tick()

        stats = r.stats()
        assert stats["refusals_measured"] == 1
        assert stats["refusals_vindicated"] == 1.0


def test_stats_separate_direction_accuracy_from_win_rate():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        # Right direction, stopped out on the way.
        r = resolver(d, FakeMarket([bar(60100, 59700, 60250)]), clock)
        register(r)
        clock.advance((H15 + 1) * 60)
        r.tick()
        stats = r.stats()
        assert stats["direction_accuracy"] == 1.0
        assert stats["net_win_rate"] == 0.0


def test_resolved_hook_fires():
    seen = []
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket(flat(60400)), clock,
                     on_resolved=seen.append)
        register(r)
        clock.advance((H15 + 1) * 60)
        r.tick()
        assert seen[0]["signal_id"] == "sig_1"


def test_a_broken_hook_does_not_lose_the_outcome():
    def boom(outcome):
        raise RuntimeError("anchor failed")

    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket(flat(60400)), clock, on_resolved=boom)
        register(r)
        clock.advance((H15 + 1) * 60)
        r.tick()
        assert len(json.loads((Path(d) / "outcomes.json").read_text())) == 1


def test_outcomes_feed_model_coherence():
    """The file ModelCoherence reads — nothing wrote it before this module."""
    from zcastor.risk.coherence import ModelCoherence

    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        r = resolver(d, FakeMarket([bar(60010, 59700, 59750)]), clock)
        for i in range(6):
            r.register(signal_id=f"sig_{i}", direction="UP", ref_price=60000.0,
                       timeframe="15m", sl_pts=200, tp_pts=300, spread_pts=40)
        clock.advance((H15 + 1) * 60)
        r.tick()

        status, wr, n = ModelCoherence(POLICY, Path(d) / "outcomes.json").check()
        assert n == 6 and wr == 0.0 and status == "caution"


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
