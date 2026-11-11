"""
Range adequacy and risk:reward.

Both judge a MARKET entry against a MARKET stop, so both declare
`runs_when_routed = False`. This is the fix for the v1 bug: there the guard was
an `and not route_to_watcher` clause that only survived on the spread gate when
the enclosing block was split into three statements, leaving these two able to
kill a routed signal using a stop it was never going to use.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class RangeAdequacyGate(Gate):
    """
    Does the market have room for the trade to breathe before it hits the stop?

    If the nearest opposing level is closer than the stop distance, price has to
    pass through structure to reach the target but only needs open air to reach
    the stop. Trap probability is high.

    A MISSING LEVEL MEANS UNKNOWN, NOT ZERO. v1 crashed live on 15 Jun 00:51
    because `signal.get(a) if up else signal.get(b) or 0` binds `or 0` to the
    else branch only, so an UP signal with no resistance passed float(None). The
    fix there was parentheses; here the absence is handled explicitly, because a
    level of "unknown" and a level of "zero points away" are opposite claims and
    should never share a representation.
    """
    name = "range_adequacy"
    runs_when_routed = False

    def evaluate(self, ctx: GateContext) -> Decision:
        direction = ctx.need("direction")
        sl_pts = ctx.need("sl_pts")

        key = "nearest_resistance_pts" if direction == "UP" else "nearest_support_pts"
        raw = ctx.signal.get(key)

        if raw is None or raw == "":
            return allow(available_pts=None, reason="level_unknown")

        try:
            available_pts = float(raw)
        except (TypeError, ValueError):
            return allow(available_pts=None, reason="level_unparseable")

        if available_pts <= 0:
            return allow(available_pts=available_pts, reason="level_unknown")

        ctx.put("available_pts", available_pts)

        multiple = float(ctx.policy_value("range_adequacy_multiple", 1.0))
        if available_pts < sl_pts * multiple:
            return suppress(
                "range_below_sl",
                available_pts=round(available_pts),
                sl_pts=round(sl_pts),
                required=round(sl_pts * multiple),
            )

        return allow(available_pts=round(available_pts), sl_pts=round(sl_pts))


class RiskRewardGate(Gate):
    """
    Sanity check on the computed targets. The optimiser enforces a minimum R:R
    internally, so this catches the edge cases where TP was clamped by a
    timeframe ceiling or a structural level and came back below the floor.
    """
    name = "risk_reward"
    runs_when_routed = False

    def evaluate(self, ctx: GateContext) -> Decision:
        tp_pts = ctx.need("tp_pts")
        sl_pts = ctx.need("sl_pts")
        minimum = float(ctx.policy_value("min_risk_reward", 1.0))

        rr = tp_pts / sl_pts if sl_pts > 0 else 0.0

        if rr < minimum:
            return suppress("insufficient_rr", rr=round(rr, 2), minimum=minimum,
                            tp_pts=round(tp_pts), sl_pts=round(sl_pts))

        return allow(rr=round(rr, 2))
