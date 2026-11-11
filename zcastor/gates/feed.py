"""
Feed integrity — the one gate that carries memory.

Detecting a frozen feed requires knowing what the last tick looked like and when
we first saw it, so this gate holds state. That is deliberate and contained: it
is the only stateful gate, the state is two scalars, and the clock is injectable
so the behaviour is testable without sleeping.

Live incident this encodes (11 Jun, ~16:46–17:33 UTC): the MT5 tick froze at
62842.25 for 27 minutes while the dashboard kept firing signals. The engine
traded, committed and registered outcomes against a dead price for half an hour.
A frozen feed is garbage in — refuse until ticks move again.

Also produces the price derivations every later gate depends on:
`ask`, `bid`, `mid`, `execution_price`, `spread_pts`.
"""

from __future__ import annotations

import time
from typing import Callable

from .base import Gate, GateContext, Decision, allow, reject


class FeedGate(Gate):
    name = "feed"

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._last_tick_time: float | None = None
        self._first_seen_at: float | None = None

    def evaluate(self, ctx: GateContext) -> Decision:
        tick = ctx.market.get_tick()

        if not isinstance(tick, dict) or "error" in tick:
            err = tick.get("error") if isinstance(tick, dict) else "not_a_dict"
            return reject("tick_unavailable", error=str(err))

        tick_time = float(tick.get("time") or 0)
        now = self._clock()

        if tick_time:
            if tick_time == self._last_tick_time:
                frozen_for = now - (self._first_seen_at or now)
                limit = float(ctx.policy_value("stale_tick_seconds", 120))
                if frozen_for > limit:
                    return reject(
                        "stale_feed",
                        frozen_seconds=round(frozen_for),
                        limit_seconds=limit,
                        price=float(tick.get("price") or 0),
                    )
            else:
                self._last_tick_time = tick_time
                self._first_seen_at = now

        mid = float(tick.get("price") or 0)
        if mid == 0:
            return reject("zero_price")

        ask = float(tick.get("ask") or mid)
        bid = float(tick.get("bid") or mid)
        if ask <= 0 or bid <= 0 or ask < bid:
            return reject("tick_incoherent", ask=ask, bid=bid)

        # Execution price is the side actually paid, not the mid. v1 learned
        # this the hard way: quoting the mid understated entry cost by half the
        # spread on every single trade.
        execution_price = ask if ctx.need("is_buy") else bid

        # Round-trip cost expressed in points, so every downstream gate
        # compares like with like regardless of how the venue charges.
        # A spread-only broker charges 40pts and no commission; an exchange
        # charging 0.1% per side on an 80,000 instrument costs ~160pts and
        # quotes a 0.01 spread. Judging either on spread alone gets one of
        # them badly wrong.
        commission_rate = float(ctx.policy_value("commission_rate", 0.0))
        commission_pts = mid * commission_rate * 2 if commission_rate else 0.0
        ctx.put("commission_pts", round(commission_pts, 2))
        ctx.put("cost_pts", round((ask - bid) + commission_pts, 2))

        ctx.put("tick_time", tick_time)
        ctx.put("ask", ask)
        ctx.put("bid", bid)
        ctx.put("mid", mid)
        ctx.put("execution_price", execution_price)
        ctx.put("spread_pts", round(ask - bid))

        return allow(execution_price=execution_price, spread_pts=round(ask - bid),
                     commission_pts=round(commission_pts, 2),
                     cost_pts=round((ask - bid) + commission_pts, 2))
