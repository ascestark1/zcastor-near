"""
Orders that need the vault's permission (execution/vaulted.py).
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "execution"))

from test_engine import FakeOrders, POLICY, build, signal  # noqa: E402

from zcastor.execution.vaulted import VaultedOrders  # noqa: E402
from zcastor.gates.base import Verdict  # noqa: E402


class FakeVault:
    def __init__(self, grant=True, reason="within_limits", down=False):
        self.grant, self.reason, self.down = grant, reason, down
        self.asked, self.settled = [], []
        self.settle_down = False

    def request_permit(self, agent, **kw):
        if self.down:
            raise RuntimeError("rpc timeout")
        self.asked.append(dict(kw, agent=agent))
        return {"id": len(self.asked) - 1, "granted": self.grant,
                "reason": self.reason if not self.grant else "within_limits"}

    def settle(self, agent, permit_id, pnl_cents):
        if self.settle_down:
            raise RuntimeError("rpc timeout")
        self.settled.append((agent, permit_id, pnl_cents))


class Inner(FakeOrders):
    volume = 0.01


def vaulted(d, **kw):
    v = FakeVault(**kw)
    inner = Inner()
    return VaultedOrders(inner, v, agent="btc", leverage=3,
                         store=Path(d) / "permits.json"), v, inner


def test_a_granted_order_reaches_the_venue_with_the_right_numbers():
    with tempfile.TemporaryDirectory() as d:
        o, v, inner = vaulted(d)
        o.bind_signal("sig_1", "sha256:abc")
        r = o.market_order(side="BUY", price=110_000.0, sl=109_500.0, tp=111_000.0)
        assert r["vault_permit"] == 0 and len(inner.placed) == 1
        a = v.asked[0]
        assert a["signal_hash"] == "sha256:abc" and a["side"] == "Buy"
        assert a["notional_cents"] == 110_000 and a["risk_cents"] == 500   # $1,100, $5
        assert a["leverage_x10"] == 30


def test_a_refused_order_never_reaches_the_venue():
    with tempfile.TemporaryDirectory() as d:
        o, v, inner = vaulted(d, grant=False, reason="over_desk_budget")
        r = o.market_order(side="SELL", price=110_000.0, sl=110_500.0)
        assert r["refused"] == "over_desk_budget" and "error" in r
        assert inner.placed == []


def test_an_unreachable_vault_means_no_order():
    with tempfile.TemporaryDirectory() as d:
        o, v, inner = vaulted(d, down=True)
        r = o.market_order(side="BUY", price=110_000.0, sl=109_500.0)
        assert r["refused"] == "vault_unreachable" and inner.placed == []


def test_closing_settles_the_permit_with_the_pnl_and_forgets_it():
    with tempfile.TemporaryDirectory() as d:
        o, v, _ = vaulted(d)
        t = o.market_order(side="BUY", price=110_000.0, sl=109_500.0)["ticket"]
        o.on_closed({"ticket": t, "pnl": -4.37})
        assert v.settled == [("btc", 0, -437)]
        o.on_closed({"ticket": t, "pnl": -4.37})
        assert len(v.settled) == 1, "a permit settles once"


def test_a_failed_venue_order_gives_the_permit_back():
    with tempfile.TemporaryDirectory() as d:
        o, v, inner = vaulted(d)
        inner._fail = True
        r = o.market_order(side="BUY", price=110_000.0, sl=109_500.0)
        assert "error" in r and "refused" not in r
        assert v.settled == [("btc", 0, 0)]


def test_open_permits_survive_a_restart_and_failed_settlements_retry():
    with tempfile.TemporaryDirectory() as d:
        o, v, _ = vaulted(d)
        t = o.market_order(side="BUY", price=110_000.0, sl=109_500.0)["ticket"]
        assert json.loads((Path(d) / "permits.json").read_text()) == {str(t): 0}
        v2 = FakeVault()
        o2 = VaultedOrders(Inner(), v2, agent="btc", leverage=3,
                           store=Path(d) / "permits.json")
        v2.settle_down = True
        o2.on_closed({"ticket": t, "pnl": 2.0})
        assert v2.settled == []

        class Journal:
            def get(self, ticket):
                return {"ticket": ticket, "state": "closed", "pnl": 2.0}

        v2.settle_down = False
        o2.retry_settlements(Journal())
        assert v2.settled == [("btc", 0, 200)]
        assert json.loads((Path(d) / "permits.json").read_text()) == {}


def test_the_engine_records_a_vault_refusal_as_a_suppression_with_its_reason():
    with tempfile.TemporaryDirectory() as d:
        engine = build(d)[0]
        v = FakeVault(grant=False, reason="over_trade_risk")
        engine.orders = VaultedOrders(engine.orders, v, agent="btc", leverage=3,
                                      store=Path(d) / "permits.json")
        trace = engine.process(signal())
        assert trace.action == Verdict.SUPPRESS
        assert trace.reason == "vault_refused:over_trade_risk"
        assert engine.journal.open_tickets() == []
        assert v.asked and v.asked[0]["signal_hash"].startswith("sha256:")


def test_the_engine_fills_when_the_vault_grants():
    with tempfile.TemporaryDirectory() as d:
        engine = build(d)[0]
        v = FakeVault()
        engine.orders = VaultedOrders(engine.orders, v, agent="btc", leverage=3,
                                      store=Path(d) / "permits.json")
        trace = engine.process(signal())
        assert trace.action == Verdict.ALLOW and len(engine.journal.open_tickets()) == 1
        assert POLICY is not None


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
