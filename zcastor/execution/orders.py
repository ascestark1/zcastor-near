"""
Order placement — the only module in the system that writes to the broker.

Everything else holds a `MarketData`, which has no method that can place an
order. This is the one object with an `OrderPlacer`, and it lives behind the
execution engine rather than being reachable from a gate.

Two things carried from v1 that matter:

**Filling mode is negotiated, not assumed.** Brokers accept different modes and
reject the rest with retcode 10030. Trying IOC, then FOK, then RETURN is how a
working order survives a broker changing its mind. XM in particular does not
accept all three.

**A TP on the wrong side of price is dropped, not sent.** A BUY with TP below
the ask is rejected outright by MT5, losing the whole trade. Better to enter with
a stop and no target than not to enter at all — the close poller still books it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("zcastor.execution.orders")

RETCODE_DONE = 10009
RETCODE_BAD_FILLING = 10030


class OrderPlacer:
    def __init__(self, mt5: Any, *, symbol: str = "BTCUSD",
                 volume: float = 0.01, magic: int = 777001,
                 deviation: int = 20) -> None:
        self._mt5 = mt5
        self.symbol = symbol
        self.volume = volume
        self.magic = magic
        self.deviation = deviation

    # ── helpers ───────────────────────────────────────────────────────────────

    def _digits(self) -> int:
        try:
            info = self._mt5.symbol_info(self.symbol)
            return int(getattr(info, "digits", 2)) if info else 2
        except Exception:  # noqa: BLE001
            return 2

    def _filling_modes(self) -> list[tuple[str, Any]]:
        return [
            ("IOC", getattr(self._mt5, "ORDER_FILLING_IOC", 1)),
            ("FOK", getattr(self._mt5, "ORDER_FILLING_FOK", 0)),
            ("RETURN", getattr(self._mt5, "ORDER_FILLING_RETURN", 2)),
        ]

    def _last_error(self) -> str:
        try:
            return str(self._mt5.last_error())
        except Exception:  # noqa: BLE001
            return "unavailable"

    def _send(self, request: dict) -> dict:
        """Try each filling mode until one is not rejected for being unsupported."""
        result = None
        send_request = getattr(self._mt5, "send_request", None)

        for name, mode in self._filling_modes():
            request["type_filling"] = int(mode)
            try:
                # Build the dict in the remote interpreter where possible; a
                # proxy dict fails MT5's C-level inspection.
                result = (send_request(dict(request)) if callable(send_request)
                          else self._mt5.order_send(request))
            except Exception as exc:  # noqa: BLE001
                logger.error("order_send raised (%s): %s", name, exc)
                return {"error": f"order_send failed: {exc}"}

            if result is None:
                # None with no exception means MT5 refused the request itself.
                # last_error() is the only thing that says why.
                error = self._last_error()
                logger.error("order_send returned None (%s): last_error=%s",
                             name, error)
                return {"error": f"order_send returned None — {error}"}
            if int(getattr(result, "retcode", 0)) == RETCODE_BAD_FILLING:
                logger.debug("filling mode %s unsupported — trying next", name)
                continue
            break

        retcode = int(getattr(result, "retcode", 0))
        if retcode != RETCODE_DONE:
            comment = getattr(result, "comment", "")
            logger.error("order rejected: retcode=%s comment=%s", retcode, comment)
            return {"error": f"order rejected (retcode {retcode}): {comment}"}

        return {"ok": True, "result": result}

    # ── public ────────────────────────────────────────────────────────────────

    def market_order(self, *, side: str, price: float, sl: float,
                     tp: Optional[float] = None) -> dict:
        """
        Place a market order. Returns {"ticket": ...} or {"error": ...}.

        `price` is the execution price the decision was made against — the ask
        for a buy, the bid for a sell — so the order is priced with the same
        number the gates judged.
        """
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            return {"error": f"bad side {side!r}"}

        digits = self._digits()
        order_type = (getattr(self._mt5, "ORDER_TYPE_BUY", 0) if side == "BUY"
                      else getattr(self._mt5, "ORDER_TYPE_SELL", 1))

        request = {
            "action": getattr(self._mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": self.symbol,
            "volume": float(self.volume),
            "type": order_type,
            "price": float(price),
            "sl": float(round(sl, digits)),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "zcastor",
        }

        # A target on the wrong side of price gets the whole order rejected.
        if tp:
            wrong_side = (side == "BUY" and tp <= price) or \
                         (side == "SELL" and tp >= price)
            if wrong_side:
                logger.error("TP %.2f is on the wrong side of %s at %.2f — "
                             "entering without a target", tp, side, price)
            else:
                request["tp"] = float(round(tp, digits))

        sent = self._send(request)
        if "error" in sent:
            return sent

        result = sent["result"]
        ticket = int(getattr(result, "order", 0))
        logger.info("filled ticket=%s %s @ %.2f sl=%.2f tp=%s",
                    ticket, side, price, sl, request.get("tp"))
        return {"ticket": ticket, "side": side, "price": float(price),
                "sl": float(sl), "tp": request.get("tp")}

    def close_position(self, ticket: int, side: str) -> dict:
        """Close by opposing market order. `side` is the position's own side."""
        try:
            tick = self._mt5.symbol_info_tick(self.symbol)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"no tick: {exc}"}
        if tick is None:
            return {"error": "no tick"}

        closing_buy = str(side).upper() == "SELL"
        price = float(tick.ask if closing_buy else tick.bid)
        order_type = (getattr(self._mt5, "ORDER_TYPE_BUY", 0) if closing_buy
                      else getattr(self._mt5, "ORDER_TYPE_SELL", 1))

        volume = self.volume
        try:
            for p in (self._mt5.positions_get(symbol=self.symbol) or []):
                if int(getattr(p, "ticket", 0)) == int(ticket):
                    volume = float(getattr(p, "volume", self.volume))
                    break
        except Exception:  # noqa: BLE001
            pass

        sent = self._send({
            "action": getattr(self._mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": self.symbol,
            "volume": float(volume),
            "type": order_type,
            "position": int(ticket),
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "zcastor-close",
        })
        if "error" in sent:
            return sent
        logger.info("closed ticket=%s at %.2f", ticket, price)
        return {"ticket": int(ticket), "close_price": price}
