"""
Binance adapter tests.

The claim being tested is that a second venue is an implementation detail: the
gate stack, record layer and anchoring must work unchanged behind it. So the
tests check interface compatibility as much as behaviour.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.execution.binance_orders import BinanceOrderPlacer  # noqa: E402
from zcastor.market.binance import (  # noqa: E402
    BinanceError, BinanceMarketData,
)


class FakeClient:
    """Records requests and replays canned responses."""

    def __init__(self, responses=None, raises=None):
        self.calls = []
        self._r = responses or {}
        self._raises = raises

    def request(self, method, path, params=None, signed=False):
        self.calls.append((method, path, dict(params or {}), signed))
        if self._raises:
            raise self._raises
        for key, value in self._r.items():
            if key in path:
                return value
        return {}


BOOK = {"askPrice": "78500.10", "bidPrice": "78499.60"}
ACCOUNT = {"balances": [{"asset": "USDT", "free": "500.0", "locked": "0.0"},
                        {"asset": "BTC", "free": "0.01", "locked": "0.0"}]}
FILTERS = {"symbols": [{"filters": [
    {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
    {"filterType": "NOTIONAL", "minNotional": "10.0"}]}]}


def market(**kw):
    return BinanceMarketData(FakeClient({"bookTicker": BOOK,
                                         "account": ACCOUNT}), **kw)


# ── prices ────────────────────────────────────────────────────────────────────

def test_the_tick_matches_the_mt5_shape():
    """Same keys, so no gate needs to know which venue produced it."""
    tick = market().get_tick()
    assert set(tick) >= {"price", "ask", "bid", "spread_pts", "time"}
    assert tick["ask"] == 78500.10 and tick["bid"] == 78499.60


def test_the_spread_is_a_fraction_of_the_mt5_one():
    """
    The finding that matters commercially. XM quotes BTC ~40 wide; this is
    under a dollar, which moves the 5m break-even win rate from 69% to ~52%.
    """
    assert market().get_tick()["spread_pts"] == 0.5


def test_a_venue_error_is_returned_not_raised():
    md = BinanceMarketData(FakeClient(raises=BinanceError("503")))
    assert "error" in md.get_tick()


# ── account ───────────────────────────────────────────────────────────────────

def test_balance_reads_the_quote_asset():
    assert market().get_balance() == 500.0


def test_unreadable_balance_is_none_not_zero():
    """The dead-man gate must tell a broken connection from an empty account."""
    md = BinanceMarketData(FakeClient(raises=BinanceError("timeout")))
    assert md.get_balance() is None


def test_spot_margin_is_the_full_notional():
    assert market().get_margin_for("UP", 0.001, 78500.0) == 78.5


def test_one_decision_reads_the_venue_once():
    client = FakeClient({"bookTicker": BOOK, "account": ACCOUNT})
    md = BinanceMarketData(client)
    md.begin()
    for _ in range(4):
        md.get_tick(); md.get_balance()
    paths = [c[1] for c in client.calls]
    assert paths.count("/api/v3/ticker/bookTicker") == 1
    assert paths.count("/api/v3/account") == 1


# ── positions ─────────────────────────────────────────────────────────────────

class FakeJournal:
    def __init__(self, trades):
        self._t = trades

    def open_tickets(self):
        return list(self._t)

    def get(self, ticket):
        return self._t.get(ticket)


def test_positions_come_from_the_journal_because_spot_has_none():
    j = FakeJournal({7: {"direction": "UP", "volume": 0.001,
                         "entry_price": 78500.0, "sl": 77900.0, "tp": 78900.0}})
    positions = market(journal=j).get_open_positions()
    assert positions[0]["ticket"] == 7 and positions[0]["side"] == "BUY"


def test_no_journal_means_no_positions():
    assert market().get_open_positions() == []


# ── fills ─────────────────────────────────────────────────────────────────────

def test_commission_in_the_base_asset_is_converted():
    fills = [{"qty": "0.001", "quoteQty": "78.5", "price": "78500",
              "commission": "0.000001", "commissionAsset": "BTC",
              "time": 1_700_000_000_000}]
    md = BinanceMarketData(FakeClient({"myTrades": fills}))
    deal = md.get_closed_deal(7)
    assert deal["close_price"] == 78500.0
    assert deal["pnl"] < 0            # commission is a cost


def test_no_fills_means_not_closed_yet():
    """None must mean retry, never a zero-PnL close."""
    md = BinanceMarketData(FakeClient({"myTrades": []}))
    assert md.get_closed_deal(7) is None


# ── orders ────────────────────────────────────────────────────────────────────

def placer(responses=None):
    r = {"exchangeInfo": FILTERS}
    r.update(responses or {})
    return BinanceOrderPlacer(FakeClient(r), volume=0.001)


def test_quantity_is_rounded_to_the_step_size():
    """An unrounded quantity is rejected outright and reads like a failed order."""
    p = BinanceOrderPlacer(FakeClient({"exchangeInfo": FILTERS}),
                           volume=0.0012345678)
    assert p._round_qty(0.0012345678) == 0.00123


def test_an_order_below_the_venue_minimum_is_refused_locally():
    p = BinanceOrderPlacer(FakeClient({"exchangeInfo": FILTERS}),
                           volume=0.00001)
    out = p.market_order(side="BUY", price=100.0, sl=90.0)
    assert "below venue minimum" in out["error"]


def test_a_fill_reports_the_average_price():
    order = {"orderId": 4242, "fills": [
        {"price": "78500.00", "qty": "0.0005"},
        {"price": "78502.00", "qty": "0.0005"}]}
    out = placer({"order": order}).market_order(
        side="BUY", price=78500.0, sl=77900.0, tp=78900.0)
    assert out["ticket"] == 4242 and out["price"] == 78501.0


def test_stops_are_recorded_but_not_sent_to_the_venue():
    """
    Spot will not hold a stop. The watcher enforces it, which means a dead
    process leaves a position unprotected — stated here so it is not
    discovered later.
    """
    client = FakeClient({"exchangeInfo": FILTERS,
                         "order": {"orderId": 1, "fills": []}})
    p = BinanceOrderPlacer(client, volume=0.001)
    out = p.market_order(side="BUY", price=78500.0, sl=77900.0, tp=78900.0)
    sent = [c for c in client.calls if c[0] == "POST"][0][2]
    assert "stopPrice" not in sent and sent["type"] == "MARKET"
    assert out["sl"] == 77900.0


def test_a_rejected_order_returns_an_error_not_an_exception():
    p = BinanceOrderPlacer(FakeClient(raises=BinanceError("insufficient balance")),
                           volume=0.001)
    assert "error" in p.market_order(side="BUY", price=78500.0, sl=1.0)


def test_closing_uses_the_original_fill_quantity():
    """self.volume would leave a residue after a partial fill."""
    client = FakeClient({"exchangeInfo": FILTERS,
                         "myTrades": [{"qty": "0.0007", "quoteQty": "55",
                                       "price": "78500", "commission": "0",
                                       "commissionAsset": "USDT", "time": 1}],
                         "order": {"orderId": 99, "fills": []}})
    BinanceOrderPlacer(client, volume=0.001).close_position(7, "BUY")
    post = [c for c in client.calls if c[0] == "POST"][0][2]
    assert post["side"] == "SELL" and post["quantity"] == 0.0007


# ── interface compatibility ───────────────────────────────────────────────────

def test_it_satisfies_everything_the_gates_call():
    from zcastor.market.mt5 import MarketData
    required = ["get_tick", "get_balance", "get_free_margin", "get_margin_for",
                "get_open_positions", "get_closed_deal", "session_open_price",
                "get_rates_range", "state", "begin"]
    for name in required:
        assert hasattr(BinanceMarketData, name), name
        assert hasattr(MarketData, name), name


def test_the_placer_matches_the_mt5_placer():
    from zcastor.execution.orders import OrderPlacer
    for name in ("market_order", "close_position", "volume"):
        assert hasattr(BinanceOrderPlacer, name) or name == "volume"
        assert hasattr(OrderPlacer, name) or name == "volume"


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
