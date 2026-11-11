"""
Trade viability — the last gate before an order is placed.

Two protective checks:

1. **Per-trade risk cap.** Dollar risk is `sl_pts × volume` under the system's
   $1/point/lot convention. If one stop-out would take more than
   `max_trade_risk_frac` of the balance, refuse. On a small account this gate
   refuses a great deal — that is the account being too small for the stop, and
   the honest response is to say so rather than to widen the cap.

2. **Margin cover.** Required margin must sit inside free margin with a buffer.
   This is what stopped the insufficient-margin order failures.

Both fail closed. If balance or free margin cannot be read, the gate refuses
rather than guessing — the same principle as the dead-man gate, and unlike v1
which returned "viable" on an unreadable balance.

`runs_when_routed = False`: the watcher re-checks viability at fire time with the
structural stop, which is the distance that will actually be risked.

`expensive = True`: three broker calls. Ordered last so they happen only when the
trade would otherwise fire.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class ViabilityGate(Gate):
    name = "viability"
    runs_when_routed = False
    expensive = True

    def evaluate(self, ctx: GateContext) -> Decision:
        sl_pts = ctx.need("sl_pts")
        direction = ctx.need("direction")
        execution_price = ctx.need("execution_price")

        volume = float(ctx.market.volume)
        balance = ctx.market.get_balance()
        free_margin = ctx.market.get_free_margin()
        required_margin = float(
            ctx.market.get_margin_for(direction, volume, execution_price) or 0.0
        )

        if not balance or balance <= 0:
            return suppress("balance_unavailable")
        if not free_margin or free_margin <= 0:
            return suppress("free_margin_unavailable")

        # Cost is part of the risk. A fee-charging venue takes it whether the
        # trade wins or loses, so a stop-out costs stop plus fee.
        cost_pts = float(ctx.get("cost_pts") or ctx.get("spread_pts") or 0)
        dollar_risk = (sl_pts + cost_pts) * volume
        risk_frac = dollar_risk / balance
        cap = float(ctx.policy_value("max_trade_risk_frac", 0.30))

        if risk_frac > cap:
            return suppress(
                "risk_above_cap",
                dollar_risk=round(dollar_risk, 2),
                balance=round(balance, 2),
                risk_frac=round(risk_frac, 4),
                cap=cap,
            )

        # Fall back to the risk amount when the broker gives us no margin figure.
        need = required_margin if required_margin > 0 else dollar_risk
        buffer = float(ctx.policy_value("margin_buffer_frac", 0.95))

        if need > free_margin * buffer:
            return suppress(
                "insufficient_margin",
                required=round(need, 2),
                free_margin=round(free_margin, 2),
                buffer=buffer,
            )

        ctx.put("dollar_risk", dollar_risk)
        return allow(dollar_risk=round(dollar_risk, 2),
                     cost_pts=round(cost_pts, 2),
                     risk_frac=round(risk_frac, 4))
