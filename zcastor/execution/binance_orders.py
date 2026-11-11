"""
Binance order placement.

Same surface as the MT5 placer: `market_order` and `close_position`, so the
execution engine does not know which venue it is trading.

One difference that has to be stated rather than discovered. On MT5 a stop and a
target rest at the broker and execute whether or not our process is alive. Spot
has no such thing — a buy is an asset balance, and there is nothing at the venue
that knows a stop was intended.

So stops here are enforced by the watcher's polling loop. If this process dies
with a position open, **nothing closes it**. That is an acceptable trade for a
single-operator system on testnet, and it is the first thing to fix before real
capital. An exchange-side OCO would close the gap; it is not built yet, and
pretending otherwise in a demo would be the wrong kind of shortcut.

Quantities are rounded to the symbol's step size. An unrounded quantity is
rejected outright, which reads in the logs like a failed order rather than a
formatting error.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..market.binance import BinanceError, _f

logger = logging.getLogger("zcastor.execution.binance_orders")


class BinanceOrderPlacer:
    def __init__(self, client: Any, *, symbol: str = "BTCUSDT",
                 volume: float = 0.001) -> None:
        self._c = client
        self.symbol = symbol
        self.volume = volume
        self._step: Optional[float] = None
        self._min_notional: float = 0.0

    # ── symbol rules ──────────────────────────────────────────────────────────

    def _load_filters(self) -> None:
        if self._step is not None:
            return
        try:
            info = self._c.request("GET", "/api/v3/exchangeInfo",
                                   {"symbol": self.symbol})
            filters = info["symbols"][0]["filters"]
        except (BinanceError, KeyError, IndexError) as exc:
            logger.warning("could not read symbol filters: %s", exc)
            self._step, self._min_notional = 0.00001, 0.0
            return
        for f in filters:
            if f["filterType"] == "LOT_SIZE":
                self._step = _f(f["stepSize"], 0.00001)
            elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                self._min_notional = _f(f.get("minNotional"), 0.0)
        self._step = self._step or 0.00001

    def _round_qty(self, qty: float) -> float:
        self._load_filters()
        step = self._step or 0.00001
        rounded = int(qty / step) * step
        # Float division leaves trailing noise that the venue rejects.
        return float(f"{rounded:.8f}")

    # ── orders ────────────────────────────────────────────────────────────────

    def market_order(self, *, side: str, price: float, sl: float,
                     tp: Optional[float] = None) -> dict:
        """
        Place a market order. Returns {"ticket": ...} or {"error": ...}.

        `sl` and `tp` are recorded for the journal and enforced by the watcher.
        They are not sent to the venue, because spot will not hold them.
        """
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            return {"error": f"bad side {side!r}"}

        qty = self._round_qty(self.volume)
        if qty <= 0:
            return {"error": f"quantity rounds to zero at step {self._step}"}

        notional = qty * float(price)
        if self._min_notional and notional < self._min_notional:
            return {"error": f"notional {notional:.2f} below venue minimum "
                             f"{self._min_notional:.2f}"}

        try:
            result = self._c.request("POST", "/api/v3/order", {
                "symbol": self.symbol, "side": side,
                "type": "MARKET", "quantity": qty,
            }, signed=True)
        except BinanceError as exc:
            logger.error("order rejected: %s", exc)
            return {"error": str(exc)}

        fills = result.get("fills") or []
        filled_qty = sum(_f(f["qty"]) for f in fills) or qty
        avg = (sum(_f(f["price"]) * _f(f["qty"]) for f in fills) / filled_qty
               if fills else float(price))

        ticket = int(result.get("orderId", 0))
        logger.info("filled order=%s %s %.6f @ %.2f (stop %.2f, target %s)",
                    ticket, side, filled_qty, avg, sl, tp)
        return {"ticket": ticket, "side": side, "price": round(avg, 2),
                "sl": float(sl), "tp": tp, "qty": filled_qty}

    def close_position(self, ticket: int, side: str) -> dict:
        """
        Close by trading the opposite side for the same quantity.

        The quantity comes from the original order's fills rather than from
        `self.volume`, since a partial fill would otherwise leave a residue that
        nothing tracks.
        """
        try:
            fills = self._c.request("GET", "/api/v3/myTrades", {
                "symbol": self.symbol, "orderId": int(ticket)}, signed=True)
        except BinanceError as exc:
            return {"error": f"cannot read original fills: {exc}"}

        qty = self._round_qty(sum(_f(f["qty"]) for f in fills))
        if qty <= 0:
            return {"error": "nothing to close"}

        closing = "SELL" if str(side).upper() == "BUY" else "BUY"
        try:
            result = self._c.request("POST", "/api/v3/order", {
                "symbol": self.symbol, "side": closing,
                "type": "MARKET", "quantity": qty,
            }, signed=True)
        except BinanceError as exc:
            logger.error("close rejected for %s: %s", ticket, exc)
            return {"error": str(exc)}

        out_fills = result.get("fills") or []
        out_qty = sum(_f(f["qty"]) for f in out_fills) or qty
        avg = (sum(_f(f["price"]) * _f(f["qty"]) for f in out_fills) / out_qty
               if out_fills else 0.0)
        logger.info("closed %s with %s order=%s at %.2f",
                    ticket, closing, result.get("orderId"), avg)
        return {"ticket": int(ticket), "close_price": round(avg, 2),
                "close_order": int(result.get("orderId", 0))}
