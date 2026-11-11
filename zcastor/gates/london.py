"""
London direction bias.

Between 05:00 and 12:00 UTC, London is structurally bullish and naive SELLs ran
roughly 17% win rate. The first version of this gate confirmed SELLs against the
4H block, which lags badly — by the time the 4H block turns down, the move is
usually over.

This version requires two independent confirmations, both current:

1. **Intraday** — has the broker session actually moved down since its open? The
   move must clear noise: greater than twice the spread, or 50 points, whichever
   is larger. Broker-priced, not exchange-priced, because that is what fills at.
2. **Own timeframe** — is the signal's own timeframe genuinely directional rather
   than ranging chop that threw a noise SELL?

Both, or the SELL is suppressed. Exhaustion is handled separately by the session
exhaustion gate, so the pair implement "allow only if down AND not already spent".

`runs_when_routed = False`. This gate judges a *market* SELL into London
strength. A routed signal enters at a structural level on confirmation, which is
a different trade with different odds — the finding does not transfer. In v1 this
was an `and not route_to_watcher` clause repeated by hand; here it is one
declaration the pipeline enforces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ._regime import is_directional, normalise_regime
from .base import Gate, GateContext, Decision, allow, suppress


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LondonBiasGate(Gate):
    name = "london_bias"
    runs_when_routed = False

    def __init__(self, now: Callable[[], datetime] = _utc_now) -> None:
        self._now = now

    def evaluate(self, ctx: GateContext) -> Decision:
        if ctx.need("direction") != "DOWN":
            return allow(applicable=False)

        window = ctx.policy_value("london_window_utc", [5, 12])
        start, end = int(window[0]), int(window[1])
        hour = self._now().hour

        if not (start <= hour < end):
            return allow(applicable=False, utc_hour=hour)

        execution_price = ctx.need("execution_price")
        spread_pts = ctx.need("spread_pts")
        session = str(ctx.signal.get("session", ""))
        session_open = float(ctx.market.session_open_price(session) or 0.0)

        floor_pts = float(ctx.policy_value("london_min_down_pts", 50.0))
        multiple = float(ctx.policy_value("london_spread_multiple", 2.0))
        threshold = max(spread_pts * multiple, floor_pts)

        move_down = session_open - execution_price if session_open > 0 else 0.0
        intraday_down = session_open > 0 and move_down > threshold

        tf_regime = normalise_regime(
            ctx.signal.get("tf_regime", ctx.signal.get("regime"))
        )
        own_tf_directional = is_directional(tf_regime)

        if intraday_down and own_tf_directional:
            return allow(
                utc_hour=hour,
                move_down_pts=round(move_down),
                tf_regime=tf_regime,
            )

        return suppress(
            "london_sell_bias",
            utc_hour=hour,
            intraday_down=intraday_down,
            move_down_pts=round(move_down),
            threshold_pts=round(threshold),
            tf_regime=tf_regime,
            own_tf_directional=own_tf_directional,
        )
