"""
Hyperliquid paper venue: real Hyperliquid prices, simulated fills.

The engine's venue until Hyperliquid order signing lands (orders on Hyperliquid
are signed requests from a trade-only agent key, which is NEAR's job to issue).
Until then the desk trades on paper, so nothing touches an exchange account and
no exchange key is needed:

- prices come from Hyperliquid's public order book (`l2Book`, 2 weight);
- bars for hourly levels and session opens come from Hyperliquid candles
  (1-minute when the window fits in Hyperliquid's ~5,000-candle history,
  5-minute otherwise; both start on the session boundaries, so a session open
  is the same either way);
- fills, stops, targets and the balance live in a paper book on disk
  (`data/paper_book.jsonl`), which survives restarts.

How a paper fill is priced, stated rather than discovered:

- a market order fills at the current ask (buy) or bid (sell);
- a target fills at the target price (it would rest on the book);
- a stop fills at the bid/ask seen when the stop is found crossed, which is at
  or beyond the stop: positions are checked whenever the engine looks at them
  (every close poll, about 20 s), so a fast move fills worse than the stop, as
  it would at a real venue;
- each fill pays the taker fee (`fee_rate`, Hyperliquid base tier 0.045%).

Same surface as the MT5 and Binance adapters, so the engine and gates don't
know which venue they're on.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .hyperliquid import HLInfo, MAX_CANDLES

logger = logging.getLogger("zcastor.market.hl_paper")

TAKER_FEE = 0.00045


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class PaperBook:
    """Paper positions and their results, as an append-only event file."""

    def __init__(self, path: Path, *, start_balance: float = 10_000.0,
                 fee_rate: float = TAKER_FEE,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.start_balance = float(start_balance)
        self.fee_rate = float(fee_rate)
        self._clock = clock
        self._lock = threading.RLock()
        self.open: dict[int, dict] = {}
        self.closed: dict[int, dict] = {}
        self._next = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue                      # a torn last line after a crash
            t = int(ev["ticket"])
            self._next = max(self._next, t + 1)
            if ev["event"] == "open":
                self.open[t] = ev
            elif ev["event"] == "close":
                self.open.pop(t, None)
                self.closed[t] = ev

    def _append(self, ev: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, sort_keys=True) + "\n")

    def fill(self, *, coin: str, side: str, qty: float, price: float,
             sl: float, tp: Optional[float]) -> dict:
        with self._lock:
            t = self._next
            self._next += 1
            ev = {"event": "open", "ticket": t, "coin": coin, "side": side,
                  "qty": qty, "price": price, "sl": sl, "tp": tp,
                  "fee": round(qty * price * self.fee_rate, 8),
                  "time": self._clock()}
            self._append(ev)
            self.open[t] = ev
            return ev

    def close(self, ticket: int, price: float, reason: str) -> Optional[dict]:
        with self._lock:
            pos = self.open.pop(int(ticket), None)
            if pos is None:
                return None
            sign = 1 if pos["side"] == "BUY" else -1
            fee = pos["qty"] * price * self.fee_rate
            pnl = sign * (price - pos["price"]) * pos["qty"] - pos["fee"] - fee
            ev = {"event": "close", "ticket": int(ticket), "coin": pos["coin"],
                  "price": price, "reason": reason, "fee": round(fee, 8),
                  "pnl": round(pnl, 8), "time": self._clock()}
            self._append(ev)
            self.closed[int(ticket)] = ev
            return ev

    def realised(self) -> float:
        with self._lock:
            return sum(c["pnl"] for c in self.closed.values())

    def balance(self) -> float:
        return self.start_balance + self.realised()


class HyperliquidMarketData:
    def __init__(self, info: HLInfo, book: PaperBook, *, coin: str = "BTC",
                 volume: float = 0.001, leverage: float = 3.0,
                 session_opens: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.info = info
        self.book = book
        self.symbol = coin
        self.volume = volume
        self.leverage = max(1.0, float(leverage))
        self._sessions = session_opens
        self._clock = clock
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}

    def begin(self) -> None:
        with self._lock:
            self._cache.clear()

    # ── prices ────────────────────────────────────────────────────────────────

    def get_tick(self) -> dict:
        with self._lock:
            if "tick" in self._cache:
                return self._cache["tick"]
        tick = self._fetch_tick()
        with self._lock:
            self._cache["tick"] = tick
        return tick

    def _fetch_tick(self) -> dict:
        try:
            book = self.info.l2_book(self.symbol)
            levels = book.get("levels") or [[], []]
            bid = _f(levels[0][0]["px"]) if levels[0] else 0.0
            ask = _f(levels[1][0]["px"]) if len(levels) > 1 and levels[1] else 0.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_tick failed: %s", exc)
            return {"error": str(exc)}
        if ask <= 0 or bid <= 0:
            return {"error": "book has no usable ask/bid"}
        return {"price": (ask + bid) / 2, "ask": ask, "bid": bid,
                "spread_pts": ask - bid,
                # Hyperliquid stamps the book, so the stale-feed gate gets a
                # real timestamp, in seconds like MT5's.
                "time": _f(book.get("time")) / 1000 or round(ask + bid, 8)}

    def get_rates_range(self, from_ts: float, to_ts: float) -> list:
        span_min = (to_ts - from_ts) / 60
        interval = "1m" if span_min <= MAX_CANDLES * 0.7 else "5m"
        try:
            rows = self.info.candles(self.symbol, interval, int(from_ts * 1000),
                                     int(to_ts * 1000))
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_rates_range failed: %s", exc)
            return []
        return [{"time": int(c["t"]) / 1000, "open": _f(c["o"]), "high": _f(c["h"]),
                 "low": _f(c["l"]), "close": _f(c["c"])} for c in rows]

    def session_open_price(self, session: str) -> float:
        if self._sessions is None:
            return 0.0
        return self._sessions.open_price(session)

    # ── account (paper) ───────────────────────────────────────────────────────

    def _used_margin(self) -> float:
        return sum(p["qty"] * p["price"] / self.leverage
                   for p in self.book.open.values())

    def get_balance(self) -> Optional[float]:
        return self.book.balance()

    def get_free_margin(self) -> Optional[float]:
        return self.book.balance() - self._used_margin()

    def get_equity(self) -> Optional[float]:
        tick = self.get_tick()
        if "error" in tick:
            return self.book.balance()
        upnl = 0.0
        for p in self.book.open.values():
            if p["coin"] != self.symbol:
                continue
            sign = 1 if p["side"] == "BUY" else -1
            exit_px = tick["bid"] if sign > 0 else tick["ask"]
            upnl += sign * (exit_px - p["price"]) * p["qty"]
        return self.book.balance() + upnl

    def get_margin_for(self, direction: str, volume: float, price: float) -> float:
        return float(volume) * float(price) / self.leverage

    # ── positions ─────────────────────────────────────────────────────────────

    def _settle_stops(self) -> None:
        """Close any paper position whose stop or target the market has crossed."""
        if not any(p["coin"] == self.symbol for p in self.book.open.values()):
            return
        # Its own fresh price: gates read positions mid-decision, and clearing
        # the decision's cached tick would let the price move under them.
        tick = self._fetch_tick()
        if "error" in tick:
            return
        for t, p in list(self.book.open.items()):
            if p["coin"] != self.symbol:
                continue
            sl, tp = _f(p.get("sl")), _f(p.get("tp"))
            if p["side"] == "BUY":
                if sl and tick["bid"] <= sl:
                    self.book.close(t, tick["bid"], "stop")
                elif tp and tick["bid"] >= tp:
                    self.book.close(t, tp, "target")
            else:
                if sl and tick["ask"] >= sl:
                    self.book.close(t, tick["ask"], "stop")
                elif tp and tick["ask"] <= tp:
                    self.book.close(t, tp, "target")

    def get_open_positions(self) -> list:
        self._settle_stops()
        return [{"ticket": t, "side": p["side"], "volume": p["qty"],
                 "open_price": p["price"], "sl": _f(p.get("sl")),
                 "tp": _f(p.get("tp")), "profit": 0.0}
                for t, p in sorted(self.book.open.items())
                if p["coin"] == self.symbol]

    def get_closed_deal(self, ticket: int) -> Optional[dict]:
        c = self.book.closed.get(int(ticket))
        if c is None:
            return None
        return {"ticket": int(ticket), "pnl": round(c["pnl"], 4),
                "close_price": c["price"], "close_time": int(c["time"]),
                "win": c["pnl"] > 0, "qty": None, "reason": c["reason"]}

    def state(self, session: str = "") -> dict:
        tick = self.get_tick()
        return {"tick_time": tick.get("time", 0.0), "ask": tick.get("ask", 0.0),
                "bid": tick.get("bid", 0.0), "spread_pts": tick.get("spread_pts", 0.0),
                "balance": self.get_balance(), "free_margin": self.get_free_margin(),
                "open_positions": len(self.get_open_positions()),
                "session": session,
                "session_open": self.session_open_price(session) if session else None,
                "venue": "hyperliquid-paper"}


class PaperOrderPlacer:
    def __init__(self, market: HyperliquidMarketData, *, volume: float,
                 sz_decimals: Optional[int] = None) -> None:
        self.market = market
        self.volume = volume
        self._szd = sz_decimals

    def _qty(self) -> float:
        if self._szd is None:
            return float(self.volume)
        step = 10 ** -self._szd
        return float(f"{int(self.volume / step) * step:.{self._szd}f}")

    def market_order(self, *, side: str, price: float, sl: float,
                     tp: Optional[float] = None) -> dict:
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            return {"error": f"bad side {side!r}"}
        qty = self._qty()
        if qty <= 0:
            return {"error": "quantity rounds to zero"}
        self.market.begin()
        tick = self.market.get_tick()
        if "error" in tick:
            return {"error": f"no price: {tick['error']}"}
        fill = tick["ask"] if side == "BUY" else tick["bid"]
        if qty * fill < 10:
            return {"error": f"notional {qty * fill:.2f} below Hyperliquid's $10 minimum"}
        ev = self.market.book.fill(coin=self.market.symbol, side=side, qty=qty,
                                   price=fill, sl=float(sl), tp=tp)
        logger.info("PAPER filled ticket=%s %s %s %.6f @ %s (stop %s, target %s)",
                    ev["ticket"], side, self.market.symbol, qty, fill, sl, tp)
        return {"ticket": ev["ticket"], "side": side, "price": fill,
                "sl": float(sl), "tp": tp, "qty": qty, "paper": True}

    def close_position(self, ticket: int, side: str) -> dict:
        self.market.begin()
        tick = self.market.get_tick()
        if "error" in tick:
            return {"error": f"no price: {tick['error']}"}
        px = tick["bid"] if str(side).upper() == "BUY" else tick["ask"]
        ev = self.market.book.close(int(ticket), px, "manual")
        if ev is None:
            return {"error": "nothing to close"}
        return {"ticket": int(ticket), "close_price": px, "paper": True}
