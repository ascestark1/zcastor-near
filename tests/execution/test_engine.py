"""
Engine tests — the whole system end to end, with fakes at the edges.

No broker, no chain, no clock dependence. Every layer built so far runs: origin
capture, the twenty-stage gate pipeline, order placement, the journal, zone
memory, the record layer and the outbox.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.base import NullBackend  # noqa: E402
from zcastor.anchor.outbox import Outbox  # noqa: E402
from zcastor.data.journal import Journal  # noqa: E402
from zcastor.data.ledger import EdgeLedger  # noqa: E402
from zcastor.execution.booking import Booker, ClosePoller  # noqa: E402
from zcastor.execution.engine import Engine  # noqa: E402
from zcastor.gates.base import Verdict  # noqa: E402
from zcastor.gates.coherence import ModelCoherenceGate, SignalCoherenceGate  # noqa: E402
from zcastor.gates.confidence import ConfidenceGate  # noqa: E402
from zcastor.gates.deadman import DeadmanGate  # noqa: E402
from zcastor.gates.direction import DirectionGate  # noqa: E402
from zcastor.gates.exhaustion import SessionExhaustionGate  # noqa: E402
from zcastor.gates.feed import FeedGate  # noqa: E402
from zcastor.gates.ledger import EdgeLedgerGate  # noqa: E402
from zcastor.gates.london import LondonBiasGate  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.gates.range import RangeAdequacyGate, RiskRewardGate  # noqa: E402
from zcastor.gates.regime import RegimeContextGate  # noqa: E402
from zcastor.gates.session import SessionGate  # noqa: E402
from zcastor.gates.spread import SpreadHardGate, SpreadViabilityGate  # noqa: E402
from zcastor.gates.stack import StackLimitGate  # noqa: E402
from zcastor.gates.targets import TargetStage  # noqa: E402
from zcastor.gates.viability import ViabilityGate  # noqa: E402
from zcastor.gates.zone import FlipConfirmGate, ZoneGate  # noqa: E402
from zcastor.record.decision_log import DecisionLog  # noqa: E402
from zcastor.record.publisher import Publisher  # noqa: E402
from zcastor.risk.coherence import ModelCoherence, RiskAdapter, SignalCoherence  # noqa: E402
from zcastor.risk.zones import ZoneTracker  # noqa: E402
from zcastor.targets.optimizer import TargetOptimizer  # noqa: E402

POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeMarket:
    volume = 0.05

    def __init__(self, *, balance=1000.0, positions=None, session_open=62700.0,
                 ask=62860.0, bid=62840.0, deals=None):
        self._balance = balance
        self._positions = positions if positions is not None else []
        self._session_open = session_open
        self._ask, self._bid = ask, bid
        self._deals = deals or {}

    def begin(self):
        pass

    def get_tick(self):
        return {"price": (self._ask + self._bid) / 2, "ask": self._ask,
                "bid": self._bid, "spread_pts": round(self._ask - self._bid),
                "time": 1_750_000_000}

    def get_balance(self):
        return self._balance

    def get_free_margin(self):
        return self._balance * 0.9

    def get_equity(self):
        return self._balance

    def get_margin_for(self, direction, volume, price):
        return 100.0

    def get_open_positions(self):
        return list(self._positions)

    def get_closed_deal(self, ticket):
        return self._deals.get(int(ticket))

    def session_open_price(self, session):
        return self._session_open

    def state(self, session=""):
        tick = self.get_tick()
        return {"tick_time": tick["time"], "ask": tick["ask"], "bid": tick["bid"],
                "spread_pts": tick["spread_pts"], "balance": self._balance,
                "free_margin": self.get_free_margin(),
                "open_positions": len(self._positions), "session": session,
                "session_open": self._session_open}


class FakeOrders:
    volume = 0.05

    def __init__(self, *, fail=False):
        self.placed = []
        self.closed = []
        self._fail = fail
        self._next = 1000

    def market_order(self, *, side, price, sl, tp=None):
        if self._fail:
            return {"error": "no money"}
        self._next += 1
        self.placed.append({"side": side, "price": price, "sl": sl, "tp": tp})
        return {"ticket": self._next, "side": side, "price": price,
                "sl": sl, "tp": tp}

    def close_position(self, ticket, side):
        self.closed.append(ticket)
        return {"ticket": ticket, "close_price": 62850.0}


class FakePositions:
    def __init__(self, n=0):
        self._n = n

    def live(self):
        return [{"ticket": i} for i in range(self._n)]


class FakeWatcher:
    def __init__(self):
        self.armed = []

    def arm(self, *, signal_id, signal, route_kind, derived):
        self.armed.append((signal_id, route_kind))
        return {"zone": derived.get("execution_price")}


def at_hour(h):
    return lambda: datetime(2026, 8, 27, h, 0, tzinfo=timezone.utc)


def full_stack(now_hour=14):
    return [
        DirectionGate(), ConfidenceGate(), FeedGate(), SpreadHardGate(),
        DeadmanGate(), StackLimitGate(), ZoneGate(), FlipConfirmGate(),
        SignalCoherenceGate(), ModelCoherenceGate(), SessionGate(),
        RegimeContextGate(), LondonBiasGate(now=at_hour(now_hour)),
        TargetStage(), SpreadViabilityGate(), RangeAdequacyGate(),
        RiskRewardGate(), SessionExhaustionGate(), EdgeLedgerGate(enforce=False),
        ViabilityGate(),
    ]


def build(tmp, *, market=None, orders=None, watcher=None, positions=0):
    tmp = Path(tmp)
    journal = Journal(tmp / "trades.jsonl")
    ledger = EdgeLedger(POLICY, tmp / "edge.jsonl")
    zones = ZoneTracker(POLICY)
    risk = RiskAdapter(zones=zones,
                       signal_coherence=SignalCoherence(POLICY),
                       model_coherence=ModelCoherence(POLICY, tmp / "out.json"),
                       journal=journal)
    outbox = Outbox(tmp / "outbox.jsonl")
    log = DecisionLog("2026-08-27")

    publisher = Publisher(tmp / "dossiers",
                          uri_template="https://example.test/{path}")
    engine = Engine(
        pipeline=GatePipeline(full_stack()),
        market=market or FakeMarket(),
        orders=orders or FakeOrders(),
        risk=risk,
        targets=TargetOptimizer(POLICY),
        ledger=ledger,
        journal=journal,
        positions=FakePositions(positions),
        policy=POLICY,
        policy_hash="sha256:policy",
        decision_log=log,
        outbox=outbox,
        watcher=watcher,
        publisher=publisher,
        code_version="test",
        registries={"dossiers": "afritensor-dossiers"},
    )
    return engine, journal, ledger, outbox, log


def signal(**over):
    s = {"direction": "UP", "confidence": "high", "timeframe": "15m",
         "session": "London", "regime": "trending", "tf_regime": "trending",
         "zone_phase": "fresh", "atr": 200, "nearest_resistance_pts": 600,
         "nearest_support_pts": 250, "block_confidence": "high"}
    s.update(over)
    return s


# ── happy path ────────────────────────────────────────────────────────────────

def test_clean_signal_trades_and_is_journalled():
    with tempfile.TemporaryDirectory() as d:
        orders = FakeOrders()
        engine, journal, _, outbox, log = build(d, orders=orders)
        trace = engine.process(signal())

        assert trace.action is Verdict.ALLOW
        assert len(orders.placed) == 1
        assert orders.placed[0]["side"] == "BUY"
        assert len(journal.open_tickets()) == 1
        assert log.summary() == {"allow": 1}


def test_trade_is_priced_at_the_ask_for_a_buy():
    with tempfile.TemporaryDirectory() as d:
        orders = FakeOrders()
        engine, *_ = build(d, orders=orders)
        engine.process(signal())
        assert orders.placed[0]["price"] == 62860.0


def test_execution_produces_a_dossier_record_in_the_outbox():
    with tempfile.TemporaryDirectory() as d:
        engine, _, _, outbox, _ = build(d)
        engine.process(signal())
        assert len(outbox.pending) == 1
        entry = outbox.pending[0]
        assert entry.registry == "afritensor-dossiers"
        assert len(entry.checksum) == 64
        assert entry.uri.endswith(".json")
        # The artifact must exist, and its hash must be the anchored one.
        relative = entry.uri.replace("https://example.test/", "")
        assert engine.publisher.verify(relative, "sha256:" + entry.checksum)


def test_the_outbox_drains_offline():
    with tempfile.TemporaryDirectory() as d:
        engine, _, _, outbox, _ = build(d)
        engine.process(signal())
        assert outbox.drain(NullBackend())["sent"] == 1


# ── refusals ──────────────────────────────────────────────────────────────────



def test_rejected_signal_produces_no_record_at_all():
    with tempfile.TemporaryDirectory() as d:
        engine, _, _, outbox, log = build(d)
        trace = engine.process(signal(direction="SIDEWAYS"))
        assert trace.action is Verdict.REJECT
        assert log.entries == []
        assert outbox.pending == []


def test_order_failure_is_recorded_as_a_suppression():
    """The decision stood; only the fill failed. Both facts belong in the record."""
    with tempfile.TemporaryDirectory() as d:
        engine, journal, _, _, log = build(d, orders=FakeOrders(fail=True))
        trace = engine.process(signal())
        assert trace.reason == "order_failed"
        assert journal.open_tickets() == []
        assert log.reasons() == {"order_failed": 1}


# ── routing ───────────────────────────────────────────────────────────────────

def ranging_signal():
    return signal(timeframe="5m", regime="ranging", tf_regime="ranging",
                  block_confidence="low")


def test_routed_signal_arms_the_watcher_and_places_no_order():
    with tempfile.TemporaryDirectory() as d:
        orders, watcher = FakeOrders(), FakeWatcher()
        engine, *_ = build(d, orders=orders, watcher=watcher)
        trace = engine.process(ranging_signal())

        assert trace.action is Verdict.ROUTE
        assert watcher.armed and watcher.armed[0][1] == "fresh"
        assert orders.placed == []


def test_routing_without_a_watcher_is_an_honest_suppression():
    with tempfile.TemporaryDirectory() as d:
        engine, _, _, _, log = build(d, watcher=None)
        trace = engine.process(ranging_signal())
        assert trace.action is Verdict.SUPPRESS
        assert trace.reason == "watcher_disabled"
        assert log.reasons() == {"watcher_disabled": 1}


def test_watcher_failure_does_not_take_down_the_engine():
    class Exploding:
        def arm(self, **kw):
            raise RuntimeError("watcher broken")

    with tempfile.TemporaryDirectory() as d:
        engine, *_ = build(d, watcher=Exploding())
        trace = engine.process(ranging_signal())
        assert trace.reason == "watcher_error"


# ── zone behaviour ────────────────────────────────────────────────────────────

def test_zone_memory_records_only_after_a_fill():
    with tempfile.TemporaryDirectory() as d:
        engine, *_ = build(d, orders=FakeOrders(fail=True))
        engine.process(signal())
        assert engine.risk.check_zone(62850.0, "up", "15m")[1] == 0


def test_four_fills_exhaust_the_zone_and_the_fifth_routes():
    with tempfile.TemporaryDirectory() as d:
        watcher = FakeWatcher()
        engine, *_ = build(d, watcher=watcher)
        for _ in range(4):
            engine.process(signal())
        trace = engine.process(signal())
        assert trace.action is Verdict.ROUTE
        assert trace.route_kind == "exhaustion"


def test_exhausted_zone_closes_open_positions_in_that_direction():
    """v1 did this inside a gate. It is an action, so it lives in the engine."""
    with tempfile.TemporaryDirectory() as d:
        market = FakeMarket(positions=[
            {"ticket": 1, "side": "BUY", "volume": 0.05, "open_price": 62800.0,
             "sl": 0, "tp": 0, "profit": 1.0},
            {"ticket": 2, "side": "SELL", "volume": 0.05, "open_price": 62900.0,
             "sl": 0, "tp": 0, "profit": -1.0},
        ])
        orders = FakeOrders()
        engine, *_ = build(d, market=market, orders=orders, watcher=FakeWatcher())
        for _ in range(4):
            engine.process(signal())
        orders.closed.clear()
        engine.process(signal())
        assert orders.closed == [1], "only the same-direction position closes"


# ── booking ───────────────────────────────────────────────────────────────────

def test_close_poller_books_and_feeds_the_edge_ledger():
    with tempfile.TemporaryDirectory() as d:
        market = FakeMarket()
        engine, journal, ledger, _, _ = build(d, market=market)
        engine.process(signal())
        ticket = journal.open_tickets()[0]

        market._deals[ticket] = {"ticket": ticket, "pnl": 15.0,
                                 "close_price": 63160.0,
                                 "close_time": 1_750_000_500, "win": True}
        booker = Booker(market=market, journal=journal, ledger=ledger)
        booked = ClosePoller(market=market, journal=journal, booker=booker).poll()

        assert len(booked) == 1
        assert journal.get(ticket)["pnl"] == 15.0
        assert ledger.verdict("15m", "London", "market")["n"] == 1


def test_unclosed_position_is_not_booked_as_zero():
    with tempfile.TemporaryDirectory() as d:
        market = FakeMarket()
        engine, journal, ledger, _, _ = build(d, market=market)
        engine.process(signal())
        ticket = journal.open_tickets()[0]

        booker = Booker(market=market, journal=journal, ledger=ledger)
        assert booker.book(ticket) is None          # no deal yet
        assert journal.get(ticket)["state"] == "open"
        assert ledger.verdict("15m", "London", "market")["n"] == 0


def test_r_multiple_uses_the_stop_recorded_at_entry():
    with tempfile.TemporaryDirectory() as d:
        market = FakeMarket()
        engine, journal, ledger, _, _ = build(d, market=market)
        engine.process(signal())
        ticket = journal.open_tickets()[0]
        trade = journal.get(ticket)
        risk = abs(trade["entry_price"] - trade["sl"]) * trade["volume"]

        market._deals[ticket] = {"ticket": ticket, "pnl": risk * 2,
                                 "close_price": 1.0, "close_time": 1, "win": True}
        Booker(market=market, journal=journal, ledger=ledger).book(ticket)
        assert abs(ledger.verdict("15m", "London", "market")["exp"] - 2.0) < 0.01


def test_double_booking_is_impossible():
    with tempfile.TemporaryDirectory() as d:
        market = FakeMarket()
        engine, journal, ledger, _, _ = build(d, market=market)
        engine.process(signal())
        ticket = journal.open_tickets()[0]
        market._deals[ticket] = {"ticket": ticket, "pnl": 10.0,
                                 "close_price": 1.0, "close_time": 1, "win": True}
        booker = Booker(market=market, journal=journal, ledger=ledger)
        assert booker.book(ticket) is not None
        assert booker.book(ticket) is None
        assert journal.stats()["net_pnl"] == 10.0


# ── accountability invariants ─────────────────────────────────────────────────

def test_recording_failure_never_breaks_trading():
    class BrokenLog:
        def add(self, **kw):
            raise RuntimeError("disk full")

    with tempfile.TemporaryDirectory() as d:
        orders = FakeOrders()
        engine, journal, *_ = build(d, orders=orders)
        engine.decision_log = BrokenLog()
        trace = engine.process(signal())
        assert trace.action is Verdict.ALLOW
        assert len(orders.placed) == 1
        assert len(journal.open_tickets()) == 1




def test_identical_payloads_hash_identically():
    with tempfile.TemporaryDirectory() as d:
        engine, _, _, _, log = build(d)
        engine.process(signal(session="NY PM"))
        engine.process(signal(session="NY PM"))
        origins = {e["proof"]["origin"] for e in log.entries}
        assert len(origins) == 1, "same input must produce one origin hash"


def test_decision_log_is_deterministic():
    with tempfile.TemporaryDirectory() as d:
        engine, _, _, _, log = build(d)
        engine.process(signal(session="NY PM"))
        assert log.checksum(policy_version="2.0.0-dev") == \
               log.checksum(policy_version="2.0.0-dev")


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
