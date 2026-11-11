"""
Position stack limit.

Caps concurrent exposure. With five positions open on one instrument, a single
adverse move hits every one of them — the correlation between them is 1.0, so
"five positions" is really one position at five times the size, without the
margin arithmetic ever having said so.

Suppresses: the system is choosing not to add exposure it already holds.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class StackLimitGate(Gate):
    name = "stack_limit"
    expensive = True          # queries live positions from the broker

    def evaluate(self, ctx: GateContext) -> Decision:
        live = ctx.positions.live()
        count = len(live)
        limit = int(ctx.policy_value("max_position_stack", 5))

        if count >= limit:
            return suppress("max_stack", open_positions=count, limit=limit)

        ctx.put("open_positions", count)
        return allow(open_positions=count)
