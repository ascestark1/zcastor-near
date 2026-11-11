"""
Regime context.

In a ranging market, chasing a breakout on 5m, 30m or 4H buys the middle of a
range and stops out at its edge. Only 1h and 15m earned the right to trade at
market in ranging conditions.

Two overrides matter, and both were learned the hard way:

1. **Own-timeframe override.** A global "ranging" read can be stale — it is often
   derived from a higher timeframe that has not updated. If the signal's own
   timeframe reports trending or volatile, that is the more current view and the
   signal is allowed. A stale 4H read must not kill a live 15m move.

2. **Route, don't kill.** A ranging market is precisely where the range *edges*
   are the good entries. Rather than suppress, hand it to the watcher to enter at
   the level with confirmation. Same reasoning as the zone gate, and the same
   deliberate omission of the `WATCHER_ENABLED` flag.

The gate only engages when block confidence is weak. A ranging read backed by a
high-confidence block is a different situation and is left to the other gates.
"""

from __future__ import annotations

from ._regime import is_directional, normalise_regime
from .base import Gate, GateContext, Decision, allow, route


class RegimeContextGate(Gate):
    name = "regime_context"

    def evaluate(self, ctx: GateContext) -> Decision:
        global_regime = normalise_regime(ctx.signal.get("regime"))
        tf_regime = normalise_regime(
            ctx.signal.get("tf_regime", ctx.signal.get("regime"))
        )
        timeframe = str(ctx.signal.get("timeframe", "1h")).strip().lower()
        block_conf = str(ctx.signal.get("block_confidence", "high")).strip().lower()

        allowed_tfs = {
            str(t).lower()
            for t in ctx.policy_value("ranging_allowed_timeframes", ["1h", "15m"])
        }
        weak_block = {
            str(c).lower()
            for c in ctx.policy_value(
                "ranging_weak_block_confidences", ["low", "none", "flat", "med"]
            )
        }

        ctx.put("tf_regime", tf_regime)
        own_tf_directional = is_directional(tf_regime)

        if global_regime != "ranging":
            return allow(regime=global_regime, tf_regime=tf_regime)

        if timeframe in allowed_tfs:
            return allow(regime="ranging", timeframe=timeframe, reason="tf_allowed")

        if own_tf_directional:
            return allow(
                regime="ranging",
                tf_regime=tf_regime,
                reason="own_tf_directional_override",
            )

        if block_conf not in weak_block:
            return allow(
                regime="ranging",
                block_confidence=block_conf,
                reason="block_confidence_strong",
            )

        return route(
            "fresh",
            regime="ranging",
            tf_regime=tf_regime,
            timeframe=timeframe,
            block_confidence=block_conf,
        )
