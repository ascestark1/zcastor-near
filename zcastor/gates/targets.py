"""
Target computation.

A Stage, not a gate: it computes rather than judges. It returns ALLOW when it
produces usable targets and REJECT when it cannot — a signal whose TP/SL could
not be derived is an unusable input, not a decision the system made.

The arithmetic itself lives behind `ctx.targets`, an adapter, so this stage stays
testable without the whole tp_optimizer. Everything downstream reads `tp`, `sl`,
`tp_pts` and `sl_pts` from `derived` — they are computed exactly once, here.

Anchoring note: distances are measured from `execution_price`, never the mid.
v1 mixed the two — some gates measured from the fill price, others from the mid,
so the same trade had two different R:R values depending on which gate you asked.
"""

from __future__ import annotations

from .base import GateContext, Decision, Stage, allow, reject


class TargetStage(Stage):
    name = "targets"

    def evaluate(self, ctx: GateContext) -> Decision:
        execution_price = ctx.need("execution_price")

        # Server-side phase wins where the zone gate has produced one; see
        # gates/zone.py for why the dashboard's own value is not authoritative.
        signal = {**ctx.signal,
                  "zone_phase": ctx.get("zone_phase") or
                  ctx.signal.get("zone_phase", "")}
        tp = ctx.targets.compute_tp(signal, fallback_price=execution_price)
        sl = ctx.targets.compute_sl(signal, fallback_price=execution_price)

        if tp is None or sl is None:
            return reject("targets_uncomputable",
                          tp_missing=tp is None, sl_missing=sl is None)

        tp, sl = float(tp), float(sl)
        tp_pts = abs(tp - execution_price)
        sl_pts = abs(execution_price - sl)

        if sl_pts <= 0:
            return reject("stop_distance_zero", sl=sl, execution_price=execution_price)

        ctx.put("tp", tp)
        ctx.put("sl", sl)
        ctx.put("tp_pts", tp_pts)
        ctx.put("sl_pts", sl_pts)

        return allow(tp_pts=round(tp_pts), sl_pts=round(sl_pts),
                     rr=round(tp_pts / sl_pts, 2))
