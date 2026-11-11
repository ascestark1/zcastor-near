"""
Market layer tests.

A fake MT5 stands in for the rpyc bridge — same duck-typed surface, including the
numpy-scalar wrapping that real MT5 returns over rpyc, since that unwrapping is
exactly the kind of thing that works locally and breaks on the bridge.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.market.mt5 import MarketData  # noqa: E402
from zcastor.market.session import SessionOpens, current_session  # noqa: E402
from zcastor.market import snapshot  # noqa: E402
from zcastor.record import proof  # noqa: E402


class Numpyish:
    """Mimics a numpy scalar: has .item(), float() alone would be wrong."""

    def __init__(self, v):
        self._v = v

    def item(self):
        return self._v


class FakeMT5:
    TIMEFRAME_M1 = 1

    def __init__(self, *, tick=True, balance=1000.0, positions=(), deals=(),
                 rates=()):
        self._tick = tick
        self._balance = balance
        self._positions = positions
        self._deals = deals
        self._rates = rates
        self.calls = {"tick": 0, "account": 0, "positions": 0}

    def symbol_info_tick(self, symbol):
        self.calls["tick"] += 1
        if not self._tick:
            return None
        return SimpleNamespace(ask=Numpyish(62860.0), bid=Numpyish(62840.0),
                               time=Numpyish(1_750_000_000))

    def account_info(self):
        self.calls["account"] += 1
        if self._balance is None:
            raise RuntimeError("bridge closed")
        return SimpleNamespace(balance=Numpyish(self._balance),
                               margin_free=Numpyish(800.0),
                               equity=Numpyish(self._balance), login=1, server="s",
                               currency="USD")

    def positions_get(self, symbol=None):
        self.calls["positions"] += 1
        return self._positions

    def history_deals_get(self, position=None):
        return self._deals

    def order_calc_margin(self, order_type, symbol, volume, price):
        return Numpyish(123.45)

    def copy_rates_range(self, symbol, tf, frm, to):
        return self._rates


def deal(profit=0.0, swap=0.0, commission=0.0, fee=0.0, entry=1, volume=0.01,
         price=63000.0, time=1_750_000_100):
    return SimpleNamespace(profit=profit, swap=swap, commission=commission,
                           fee=fee, entry=entry, volume=volume, price=price,
                           time=time)


def market(**kw):
    return MarketData(FakeMT5(**kw), symbol="BTCUSD", volume=0.05)


# ── ticks ─────────────────────────────────────────────────────────────────────

def test_numpy_scalars_are_unwrapped():
    tick = market().get_tick()
    assert tick["ask"] == 62860.0 and isinstance(tick["ask"], float)
    assert tick["spread_pts"] == 20


def test_missing_tick_returns_an_error_not_an_exception():
    assert "error" in market(tick=False).get_tick()


def test_bridge_failure_is_reported_as_an_error():
    class Broken(FakeMT5):
        def symbol_info_tick(self, symbol):
            raise ConnectionError("rpyc closed")
    assert "error" in MarketData(Broken()).get_tick()


# ── caching ───────────────────────────────────────────────────────────────────

def test_one_decision_reads_the_broker_once():
    """v1 made four round trips per signal and saw four different instants."""
    fake = FakeMT5()
    md = MarketData(fake)
    md.begin()
    for _ in range(5):
        md.get_tick()
        md.get_balance()
        md.get_free_margin()
        md.get_open_positions()
    assert fake.calls == {"tick": 1, "account": 1, "positions": 1}


def test_begin_starts_a_fresh_decision():
    fake = FakeMT5()
    md = MarketData(fake)
    md.begin(); md.get_tick()
    md.begin(); md.get_tick()
    assert fake.calls["tick"] == 2


# ── account ───────────────────────────────────────────────────────────────────

def test_unreadable_balance_is_none_not_zero():
    """The dead-man gate must be able to tell 'broken' from 'broke'."""
    md = MarketData(FakeMT5(balance=None))
    assert md.get_balance() is None


def test_genuinely_empty_account_reads_as_zero():
    assert MarketData(FakeMT5(balance=0.0)).get_balance() == 0.0


def test_margin_for_is_not_cached():
    md = market()
    assert md.get_margin_for("UP", 0.05, 62860.0) == 123.45


# ── closed deals ──────────────────────────────────────────────────────────────

def test_closed_deal_sums_net_across_partial_closes():
    deals = [
        deal(profit=0.0, commission=-0.30, entry=0),        # entry
        deal(profit=6.00, commission=-0.15, volume=0.02, price=63000.0),
        deal(profit=4.00, commission=-0.15, swap=-0.20, volume=0.03,
             price=63100.0, time=1_750_000_200),
    ]
    result = MarketData(FakeMT5(deals=deals)).get_closed_deal(1)
    assert result["pnl"] == 9.20                     # 10.00 - 0.60 - 0.20
    assert result["close_price"] == 63060.0          # volume weighted
    assert result["close_time"] == 1_750_000_200     # latest closing deal
    assert result["win"] is True


def test_entry_only_position_is_not_closed():
    """None means retry, never a zero-PnL close."""
    md = MarketData(FakeMT5(deals=[deal(entry=0)]))
    assert md.get_closed_deal(1) is None


def test_no_deals_returns_none():
    assert MarketData(FakeMT5(deals=[])).get_closed_deal(1) is None


def test_history_failure_returns_none():
    class Broken(FakeMT5):
        def history_deals_get(self, position=None):
            raise ConnectionError("closed")
    assert MarketData(Broken()).get_closed_deal(1) is None


# ── positions ─────────────────────────────────────────────────────────────────

def test_positions_are_normalised():
    positions = [SimpleNamespace(ticket=Numpyish(111), type=Numpyish(0),
                                 volume=Numpyish(0.05), price_open=Numpyish(62800.0),
                                 sl=Numpyish(62600.0), tp=Numpyish(63200.0),
                                 profit=Numpyish(3.0))]
    out = MarketData(FakeMT5(positions=positions)).get_open_positions()
    assert out[0]["side"] == "BUY" and out[0]["ticket"] == 111


# ── sessions ──────────────────────────────────────────────────────────────────

def at(h, m=0):
    return lambda: datetime(2026, 8, 27, h, m, tzinfo=timezone.utc)


def test_session_open_is_fetched_once_per_day():
    calls = []

    def rates(frm, to):
        calls.append((frm, to))
        return [{"close": 62500.0}]

    s = SessionOpens(rates, now=at(10))
    assert s.open_price("London") == 62500.0
    assert s.open_price("London") == 62500.0
    assert len(calls) == 1


def test_session_not_yet_open_returns_zero():
    """Do not silently answer with yesterday's open."""
    s = SessionOpens(lambda f, t: [{"close": 1.0}], now=at(6))
    assert s.open_price("NY PM") == 0.0


def test_unknown_session_returns_zero():
    s = SessionOpens(lambda f, t: [{"close": 1.0}], now=at(10))
    assert s.open_price("Narnia") == 0.0


def test_rates_failure_returns_zero_so_gates_skip():
    def boom(f, t):
        raise ConnectionError("bridge down")
    assert SessionOpens(boom, now=at(10)).open_price("London") == 0.0


def test_empty_rates_returns_zero():
    assert SessionOpens(lambda f, t: [], now=at(10)).open_price("London") == 0.0


def test_current_session_boundaries():
    assert current_session(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)) == "London"
    assert current_session(datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)) == "Asian"
    assert current_session(datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)) == "Overnight"


# ── snapshot ──────────────────────────────────────────────────────────────────

def payload(**over):
    p = {"direction": "UP", "confidence": "high", "timeframe": "15m",
         "session": "London", "entry_price": 62850.0}
    p.update(over)
    return p


def test_origin_hash_is_the_payload_hash():
    snap = snapshot.capture(payload(), received_at="2026-08-27T09:00:00Z")
    assert snap.origin_hash == proof.origin_hash(payload())


def test_snapshot_does_not_mutate_or_fill_the_payload():
    p = payload()
    snap = snapshot.capture(p, received_at="t")
    assert snap.payload == p
    assert "atr" not in snap.payload, "absent fields must stay absent"


def test_extra_dashboard_field_changes_the_origin_hash():
    """A dashboard build that adds a field must be visible in the record."""
    a = snapshot.capture(payload(), received_at="t").origin_hash
    b = snapshot.capture(payload(new_indicator=1.0), received_at="t").origin_hash
    assert a != b


def test_missing_fields_are_recorded_not_acted_on():
    snap = snapshot.capture(payload(), received_at="t")
    assert "atr" in snap.missing_fields
    assert "direction" not in snap.missing_fields


def test_dashboard_version_mismatch_is_detectable():
    snap = snapshot.capture(payload(dashboard_version="16.0"), received_at="t")
    assert not snapshot.version_matches(snap, "16.1")
    assert snapshot.version_matches(snap, "16.0")
    assert snapshot.version_matches(snap, ""), "no pin means no check"


# ── state ─────────────────────────────────────────────────────────────────────

def test_state_describes_one_instant():
    md = MarketData(FakeMT5(), session_opens=SessionOpens(
        lambda f, t: [{"close": 62500.0}], now=at(10)))
    md.begin()
    state = md.state(session="London")
    assert state["ask"] == 62860.0 and state["session_open"] == 62500.0
    assert proof.state_hash(**state).startswith("sha256:")


def test_state_hash_changes_with_conditions():
    a = MarketData(FakeMT5()); a.begin()
    b = MarketData(FakeMT5(balance=50.0)); b.begin()
    assert proof.state_hash(**a.state()) != proof.state_hash(**b.state())


# ── server time ───────────────────────────────────────────────────────────────

class ServerClockMT5:
    def __init__(self, utc_now, offset_hours):
        self.utc_now, self.off = utc_now, offset_hours * 3600

    def symbol_info_tick(self, symbol):
        return type("T", (), {"time": self.utc_now + self.off,
                              "ask": 80000.0, "bid": 79960.0})()

    def copy_rates_range(self, *a):
        return [{"time": self.utc_now + self.off, "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5}]


def market_at(offset_hours, utc_now=1_790_000_000):
    from zcastor.market.mt5 import MarketData
    md = MarketData(ServerClockMT5(utc_now, offset_hours))
    md._clock = lambda: utc_now
    return md, utc_now


def test_summer_server_offset_is_detected():
    assert market_at(3)[0].server_offset() == 3 * 3600


def test_winter_offset_is_detected_not_assumed():
    """XM runs UTC+2 in winter; a hardcoded +3 is wrong half the year."""
    assert market_at(2)[0].server_offset() == 2 * 3600


def test_bar_times_come_back_in_utc():
    md, now = market_at(3)
    assert md.get_rates_range(0, 1)[0]["time"] == now


def test_a_stale_tick_still_rounds_to_the_right_offset():
    from zcastor.market.mt5 import MarketData
    now = 1_790_000_000
    md = MarketData(ServerClockMT5(now - 47, 3))
    md._clock = lambda: now
    assert md.server_offset() == 3 * 3600


def test_an_implausible_offset_is_refused():
    md, _ = market_at(0)
    md._mt5.off = 400 * 86400
    md._offset = None
    assert md.server_offset() == 0


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
