"""
Spread hard block — an absolute ceiling, above which noise exceeds signal.

Distinct from the spread *viability* gate that comes much later. This one asks a
question with no reference to the target: at 60+ points on BTC, the quote itself
is unreliable and no target makes the trade sensible. Viability asks the relative
question — what fraction of *this* target does the spread eat — and it lives in
`range.py` with the other market-quality gates.

Suppresses rather than rejects: the feed is healthy, the system is choosing not
to pay this cost. That is a decision, and it is recorded.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class SpreadHardGate(Gate):
    name = "spread_hard"

    def evaluate(self, ctx: GateContext) -> Decision:
        spread_pts = ctx.need("spread_pts")
        limit = float(ctx.policy_value("spread_hard_block_pts", 60))

        if spread_pts > limit:
            return suppress("spread_hard_limit", spread_pts=spread_pts, limit=limit)

        return allow(spread_pts=spread_pts)


class SpreadViabilityGate(Gate):
    """
    Spread relative to THIS trade's target.

    The absolute number is the wrong question. A 50pt spread against a 1000pt
    target costs 5% of the move; against an 86pt target it costs 58% and the
    trade cannot win. Pure cost ratio — regime-agnostic, direction-agnostic,
    signal-relative rather than timeframe-relative.

    `runs_when_routed = False`: a routed entry is placed at a structural level
    with a different target, so this ratio does not describe it.
    """
    name = "spread_viability"
    runs_when_routed = False

    def evaluate(self, ctx: GateContext) -> Decision:
        # Total cost, not spread. A venue quoting 0.01 and charging 0.1% per
        # side is not cheap; it is expensive in a different column.
        cost_pts = ctx.get("cost_pts")
        if cost_pts is None:
            cost_pts = ctx.need("spread_pts")
        tp_pts = ctx.need("tp_pts")
        ceiling = float(ctx.policy_value("spread_target_ratio_max", 0.30))

        if tp_pts <= 0:
            return suppress("target_zero", tp_pts=tp_pts)

        ratio = cost_pts / tp_pts
        ctx.put("spread_ratio", ratio)

        if ratio > ceiling:
            return suppress(
                "cost_too_high_for_target",
                cost_pts=cost_pts,
                spread_pts=ctx.get("spread_pts"),
                commission_pts=ctx.get("commission_pts", 0),
                tp_pts=round(tp_pts),
                ratio=round(ratio, 3),
                ceiling=ceiling,
            )

        return allow(ratio=round(ratio, 3), cost_pts=cost_pts)
