"""
The Hyperliquid paper venue: real prices (here, a fake Hyperliquid), simulated fills.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "market"))

from fake_hl import FakeHL  # noqa: E402

from zcastor.data.journal import Journal  # noqa: E402
from zcastor.execution.booking import Booker, ClosePoller  # noqa: E402
from zcastor.market.hl_paper import (TAKER_FEE, HyperliquidMarketData,  # noqa: E402
                                     PaperBook, PaperOrderPlacer)
from zcastor.market.hyperliquid import HLInfo  # noqa: E402

NOW = 1_761_300_000.0


class Movable(FakeHL):
    """A fake whose BTC book can be moved by the test."""

    def __init__(self):
        super().__init__(now_ms=int(NOW * 1000))
        self.mid = 110_000.0

    def __call__(self, body):
        if body["type"] == "l2Book":
            self.calls.append(body)
            return {"coin": body["coin"], "time": self.now_ms, "levels": [
                [{"px": str(self.mid - 0.5), "sz": "1", "n": 1}],
                [{"px": str(self.mid + 0.5), "sz": "1", "n": 1}]]}
        return super().__call__(body)


def venue(d, volume=0.001):
    fake = Movable()
    info = HLInfo(fake, sleep=lambda s: None, clock=lambda: NOW)
    book = PaperBook(Path(d) / "paper_book.jsonl", start_balance=1000.0,
                     clock=lambda: NOW)
    md = HyperliquidMarketData(info, book, coin="BTC", volume=volume, leverage=5,
                               clock=lambda: NOW)
    return md, PaperOrderPlacer(md, volume=volume, sz_decimals=5), fake


def test_tick_from_the_hyperliquid_book_with_its_own_timestamp():
    with tempfile.TemporaryDirectory() as d:
        md, _, _ = venue(d)
        t = md.get_tick()
        assert t["bid"] == 109_999.5 and t["ask"] == 110_000.5
        assert t["spread_pts"] == 1.0 and t["time"] == NOW


def test_a_buy_fills_at_the_ask_and_a_sell_at_the_bid():
    with tempfile.TemporaryDirectory() as d:
        md, orders, _ = venue(d)
        buy = orders.market_order(side="BUY", price=0, sl=109_000, tp=111_000)
        sell = orders.market_order(side="SELL", price=0, sl=111_000, tp=109_000)
        assert buy["price"] == 110_000.5 and sell["price"] == 109_999.5
        assert buy["paper"] is True and buy["ticket"] != sell["ticket"]


def test_orders_below_ten_dollars_are_refused_like_hyperliquid_does():
    with tempfile.TemporaryDirectory() as d:
        md, orders, _ = venue(d, volume=0.00005)        # $5.50
        assert "minimum" in orders.market_order(side="BUY", price=0, sl=1)["error"]


def test_a_target_fills_at_the_target_and_a_stop_at_the_market():
    with tempfile.TemporaryDirectory() as d:
        md, orders, fake = venue(d)
        a = orders.market_order(side="BUY", price=0, sl=109_000, tp=111_000)["ticket"]
        b = orders.market_order(side="SELL", price=0, sl=110_500, tp=108_000)["ticket"]
        fake.mid = 111_200.0               # through the BUY's target and the SELL's stop
        assert md.get_open_positions() == []
        win, loss = md.get_closed_deal(a), md.get_closed_deal(b)
        assert win["close_price"] == 111_000 and win["reason"] == "target" and win["win"]
        assert loss["close_price"] == 111_200.5 and loss["reason"] == "stop"
        assert not loss["win"]


def test_pnl_is_net_of_the_taker_fee_on_both_sides():
    with tempfile.TemporaryDirectory() as d:
        md, orders, fake = venue(d)
        t = orders.market_order(side="BUY", price=0, sl=100_000, tp=111_000)["ticket"]
        fake.mid = 111_500.0
        md.get_open_positions()
        deal = md.get_closed_deal(t)
        gross = (111_000 - 110_000.5) * 0.001
        fees = 0.001 * (110_000.5 + 111_000) * TAKER_FEE
        assert abs(deal["pnl"] - round(gross - fees, 4)) < 1e-9
        assert abs(md.get_balance() - (1000 + gross - fees)) < 1e-6


def test_margin_is_notional_over_leverage():
    with tempfile.TemporaryDirectory() as d:
        md, orders, _ = venue(d)
        assert md.get_margin_for("UP", 0.001, 110_000) == 22.0
        orders.market_order(side="BUY", price=0, sl=100_000)
        assert abs(md.get_free_margin() - (1000 - 0.001 * 110_000.5 / 5)) < 1e-9


def test_the_book_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        md, orders, fake = venue(d)
        t1 = orders.market_order(side="BUY", price=0, sl=100_000, tp=111_000)["ticket"]
        orders.market_order(side="BUY", price=0, sl=100_000)
        fake.mid = 111_100.0
        md.get_open_positions()            # t1 hits its target
        md2, orders2, _ = venue(d)
        assert [p["ticket"] for p in md2.book.open.values()] == [t1 + 1]
        assert md2.get_closed_deal(t1)["reason"] == "target"
        assert orders2.market_order(side="BUY", price=0, sl=1)["ticket"] == t1 + 2


def test_the_close_poller_books_a_paper_close_through_the_journal():
    with tempfile.TemporaryDirectory() as d:
        md, orders, fake = venue(d)
        journal = Journal(Path(d) / "trades.jsonl")
        fill = orders.market_order(side="BUY", price=0, sl=109_000, tp=111_000)
        journal.open_trade(ticket=fill["ticket"], signal_id="sig_x", direction="UP",
                           timeframe="15m", session="London", route="market",
                           entry_price=fill["price"], sl=fill["sl"], tp=fill["tp"],
                           volume=fill["qty"])
        booker = Booker(market=md, journal=journal, ledger=None, on_closed=None,
                        policy_hash="sha256:test")
        poller = ClosePoller(market=md, journal=journal, booker=booker)
        assert poller.poll() == []
        fake.mid = 108_000.0
        booked = poller.poll()
        assert len(booked) == 1 and journal.open_tickets() == []


def test_positions_are_checked_with_a_fresh_price_without_touching_the_decision():
    with tempfile.TemporaryDirectory() as d:
        md, orders, fake = venue(d)
        orders.market_order(side="BUY", price=0, sl=109_000, tp=111_000)
        md.begin()
        decided = md.get_tick()["price"]
        fake.mid = 110_300.0
        md.get_open_positions()            # reads 110,300 for stops
        assert md.get_tick()["price"] == decided


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
