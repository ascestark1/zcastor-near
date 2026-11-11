"""
Origin capture and replay.

The claim under test: a captured decision, replayed offline under the same
policy, comes out identical in every field — verdict, reason, binding gate,
every gate's result and detail, and every derived price. If that holds, a
replay under a different policy shows exactly what that policy would change,
which is the proof the ATR switch needs.
"""

import copy
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "execution"))

from test_engine import FakeMarket, FakeOrders, FakePositions, FakeWatcher, signal  # noqa: E402

from replay import harness  # noqa: E402
from zcastor.data.journal import Journal  # noqa: E402
from zcastor.data.ledger import EdgeLedger  # noqa: E402
from zcastor.execution.engine import Engine  # noqa: E402
from zcastor.gates.assembly import build_gates  # noqa: E402
from zcastor.gates.base import Verdict  # noqa: E402
from zcastor.gates.cooldown import EntryCooldownGate  # noqa: E402
from zcastor.gates.feed import FeedGate  # noqa: E402
from zcastor.gates.london import LondonBiasGate  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.market.levels import HourlyLevels  # noqa: E402
from zcastor.record import capture as cap  # noqa: E402
from zcastor.record.capture import OriginCapture  # noqa: E402
from zcastor.record.decision_log import DecisionLog  # noqa: E402
from zcastor.risk.coherence import ModelCoherence, RiskAdapter, SignalCoherence  # noqa: E402
from zcastor.risk.zones import ZoneTracker  # noqa: E402
from zcastor.targets.optimizer import TargetOptimizer  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())
T0 = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc).timestamp()   # London open


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class LiveMarket(FakeMarket):
    """A venue whose tick time follows the clock, until it is frozen."""

    def __init__(self, clock):
        super().__init__()
        self._clock = clock
        self.frozen_at = None

    def get_tick(self):
        tick = super().get_tick()
        tick["time"] = self.frozen_at or self._clock()
        return tick


def build(tmp, *, policy=POLICY, components_fail=False, levels=True):
    tmp = Path(tmp)
    clock = Clock(T0)
    journal = Journal(tmp / "trades.jsonl")
    zones = ZoneTracker(policy, clock=clock)
    coherence = SignalCoherence(policy, clock=clock)
    risk = RiskAdapter(zones=zones, signal_coherence=coherence,
                       model_coherence=ModelCoherence(policy, tmp / "out.json"),
                       journal=journal)
    cooldown = EntryCooldownGate(clock=clock)
    feed = FeedGate(clock=clock)
    london = LondonBiasGate(now=lambda: datetime.fromtimestamp(clock(), timezone.utc))
    lv = None
    if levels:
        lv = HourlyLevels(lambda f, t: [], clock=clock)
        lv._highs, lv._lows, lv._refreshed_at = [63400.0, 64100.0], [62300.0], T0
    engine = Engine(
        pipeline=GatePipeline(build_gates(policy, levels=lv, cooldown=cooldown,
                                          feed=feed, london=london)),
        market=LiveMarket(clock), orders=FakeOrders(), risk=risk,
        targets=TargetOptimizer(policy),
        ledger=EdgeLedger(policy, tmp / "edge.jsonl"), journal=journal,
        positions=FakePositions(0), policy=policy, policy_hash="sha256:abc123",
        decision_log=DecisionLog("2026-08-27"), watcher=FakeWatcher(),
        code_version="test",
    )
    engine.on_fill_notify = lambda **kw: cooldown.record_fill(kw.get("price", 0),
                                                              kw.get("side", ""))

    def components():
        if components_fail:
            raise RuntimeError("probe broken")
        return {"zones": zones, "signal_coherence": coherence,
                "cooldown": cooldown, "feed": feed, "levels": lv}

    engine.capture = OriginCapture(tmp / "origins", policy=policy,
                                   policy_hash="sha256:abc123",
                                   components=components, symbol="BTCUSDT",
                                   venue="hyperliquid-paper", clock=clock)
    return engine, clock


def session_of_mixed_signals(engine, clock):
    """Allows, a cooldown refusal, session and London refusals, a route, zone
    fills, a reject and a stale feed: every kind of stateful decision."""
    ranging = dict(timeframe="5m", regime="ranging", tf_regime="ranging",
                   block_confidence="low")
    plan = [
        signal(session="NY PM"),                          # session_blocked
        signal(**ranging),                                # route
        signal(),                                         # allow, fills
        signal(),                                         # entry_cooldown
        signal(direction="DOWN"),                         # London window
        signal(direction="SIDEWAYS"),                     # reject
    ]
    traces = []
    for s in plan:
        traces.append(engine.process(s))
        clock.t += 60
    for _ in range(5):                                    # 1h signals, spaced
        clock.t += 1200                                   # past the cooldown
        traces.append(engine.process(signal(timeframe="1h")))
    engine.market.frozen_at = clock.t                     # the feed stops
    clock.t += 600
    traces.append(engine.process(signal()))
    traces.append(engine.process(signal()))
    return traces


# ── capture ──────────────────────────────────────────────────────────────────

def test_capture_writes_one_line_per_signal_with_inputs_and_outcome():
    with tempfile.TemporaryDirectory() as d:
        engine, clock = build(d)
        traces = session_of_mixed_signals(engine, clock)
        recs = list(harness.load_records(Path(d) / "origins"))
        assert len(recs) == len(traces)
        r = recs[0]
        for k in ("payload", "state", "tape", "clock", "outcome", "origin_hash"):
            assert k in r, k
        assert r["symbol"] == "BTCUSDT"
        assert any(e.get("method") == "get_tick" for e in r["tape"])
        assert (Path(d) / "origins" / "policies" / "abc123.json").exists()


def test_capture_outcome_is_the_pipeline_verdict_before_the_engine_acts():
    with tempfile.TemporaryDirectory() as d:
        engine, clock = build(d)
        engine.watcher = None                 # engine turns ROUTE into SUPPRESS
        trace = engine.process(signal(timeframe="5m", regime="ranging",
                                      tf_regime="ranging", block_confidence="low"))
        assert trace.reason == "watcher_disabled"
        rec = next(harness.load_records(Path(d) / "origins"))
        assert rec["outcome"]["action"] == "route"




def test_a_broken_capture_never_changes_or_stops_a_decision():
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as e:
        good, c1 = build(d)
        bad, c2 = build(e, components_fail=True)
        a = [(t.action, t.reason) for t in session_of_mixed_signals(good, c1)]
        b = [(t.action, t.reason) for t in session_of_mixed_signals(bad, c2)]
        assert a == b
        assert list(harness.load_records(Path(e) / "origins")) == []


# ── replay ───────────────────────────────────────────────────────────────────

def test_replay_under_the_same_policy_is_identical_in_every_field():
    with tempfile.TemporaryDirectory() as d:
        engine, clock = build(d)
        session_of_mixed_signals(engine, clock)
        recs = list(harness.load_records(Path(d) / "origins"))
        assert len(recs) >= 10
        for rec in recs:
            got = harness.replay_one(rec, POLICY)
            lines = harness.diff(rec["outcome"], got.outcome)
            assert not lines and not got.misses, (rec["signal_id"], lines, got.misses)


def test_replay_without_levels_is_identical_too():
    with tempfile.TemporaryDirectory() as d:
        engine, clock = build(d, levels=False)
        session_of_mixed_signals(engine, clock)
        for rec in harness.load_records(Path(d) / "origins"):
            got = harness.replay_one(rec, POLICY)
            assert not harness.diff(rec["outcome"], got.outcome)




def test_replay_reaches_no_network_and_reports_tape_misses():
    rec_tape = [{"obj": "market", "method": "get_tick", "args": [], "kwargs": {},
                 "result": {"price": 1.0}}]
    p = harness.TapePlayer("market", rec_tape)
    assert p.get_tick() == {"price": 1.0}
    try:
        p.get_balance()
        assert False, "should miss"
    except harness.TapeMiss:
        pass
    assert p.misses == ["market.get_balance()"]


# ── encoding ─────────────────────────────────────────────────────────────────

def test_tuples_survive_the_round_trip():
    v = ("caution", 3, "msg", {"x": (1, 2)})
    assert cap.decode(json.loads(json.dumps(cap.encode(v)))) == v


def test_recorder_records_exceptions_and_reraises_them():
    class Venue:
        def get_balance(self):
            raise ConnectionError("down")

    tape = []
    r = cap.Recorder("market", Venue(), tape)
    try:
        r.get_balance()
        assert False
    except ConnectionError:
        pass
    assert tape[0]["raises"] == {"type": "ConnectionError", "msg": "down"}
    played = harness.TapePlayer("market", tape)
    try:
        played.get_balance()
        assert False
    except Exception as exc:                   # noqa: BLE001
        assert type(exc).__name__ == "ConnectionError" and str(exc) == "down"


def test_cli_check_passes_on_a_clean_capture():
    import contextlib
    import io
    from replay.__main__ import main
    with tempfile.TemporaryDirectory() as d:
        engine, clock = build(d)
        session_of_mixed_signals(engine, clock)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["check", "--origins", str(Path(d) / "origins")])
        assert code == 0, out.getvalue()
        assert "different: 0" in out.getvalue()


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
