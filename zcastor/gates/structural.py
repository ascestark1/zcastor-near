"""
Structural level gate.

Refuses a market entry when no hourly swing level sits within reach of price.

The finding behind it, from era-3 trades (policy 2.2.0, 5 to 21 September):
nineteen market entries lost $29.57. Twelve of them had no hourly swing within
400 points, and those twelve account for ALL of the loss: -$35.31. The seven
with a level nearby made +$5.74. The losses are concentrated in trades with
nothing structural underneath them.

Replayed against rerouting everything to the watcher:

    route everything to the watcher   -$29.57 -> -$8.40
    this gate, market entries intact  -$29.57 -> +$5.74

Rerouting also filtered, but it changed the entry mechanics as well, and on the
six trades where that happened it made them worse. A filter keeps what helped.

SHADOW BY DEFAULT. Nineteen trades from one regime is a hypothesis with a clear
mechanism, not a proven edge. Running advisory, the gate records every
objection in the decision log without refusing anything, so its judgement
accumulates out of sample. Enforce it only once that record agrees.

Routed signals are exempt: a zone entry already requires a level.
"""

from __future__ import annotations

from typing import Any

from .base import Decision, Gate, GateContext, allow, suppress


class StructuralLevelGate(Gate):
    name = "structural_level"
    runs_when_routed = False

    def __init__(self, levels: Any = None, *, enforce: bool = False) -> None:
        self.levels = levels
        self.enforce = enforce
        # The pipeline records an advisory gate's objection as ADVISORY rather
        # than letting it bind — the same mechanism the edge ledger uses.
        self.advisory = not enforce

    def evaluate(self, ctx: GateContext) -> Decision:
        if self.levels is None:
            # No level source, no opinion. Refusing here would halt all
            # trading whenever the level feed is unavailable.
            return allow(checked=False, reason="no level source")

        price = ctx.need("execution_price")
        is_buy = ctx.need("is_buy")
        reach = float(ctx.policy_value("structural_level_max_pts", 400))

        try:
            level = (self.levels.nearest_support(price) if is_buy
                     else self.levels.nearest_resistance(price))
        except Exception as exc:  # noqa: BLE001
            return allow(checked=False, reason=f"level lookup failed: {exc}")

        distance = None if level is None else abs(price - level)
        if distance is not None and distance <= reach:
            return allow(level=round(level, 2), distance_pts=round(distance))

        # Object whenever there is something to object to. Whether it binds is
        # the pipeline's call, made from `advisory`, so the judgement is
        # recorded identically in shadow and enforcement and the two records
        # can be compared.
        return suppress("no_structural_level",
                        nearest_level=None if level is None else round(level, 2),
                        distance_pts=None if distance is None else round(distance),
                        reach_pts=reach)
