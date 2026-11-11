"""
Dead-man backstop.

Halts trading when the account is down `deadman_floor_frac` of its start-of-day
balance. This is not risk management — drawdown is monitored manually and the
win-streak cooldown was deliberately removed. It is the backstop for an
unattended system whose operator is asleep.

Start-of-day is reconstructed as `balance - today's realised PnL` rather than
snapshotted at midnight, because the process restarts and a snapshot would not
survive it. Today's PnL is negative on a down day.

DELIBERATE DEVIATION FROM v1: when balance cannot be read, v1 returned "allowed"
and traded on. This version suppresses. If we cannot verify we are above the
floor, we do not trade. The cost of being wrong in v1's direction is trading
blind through a blown account; the cost in this direction is skipping one signal
during a transient broker glitch.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class DeadmanGate(Gate):
    name = "deadman"
    expensive = True          # reads balance from the broker

    def evaluate(self, ctx: GateContext) -> Decision:
        balance = ctx.market.get_balance()

        if balance is None or balance <= 0:
            return suppress("balance_unknown", balance=balance)

        today_pnl = float(ctx.risk.today_pnl())
        start_of_day = balance - today_pnl

        if start_of_day <= 0:
            return suppress("start_of_day_invalid", start_of_day=start_of_day)

        floor_frac = float(ctx.policy_value("deadman_floor_frac", 0.75))
        drawdown = -today_pnl / start_of_day     # positive when losing

        if drawdown >= floor_frac:
            return suppress(
                "deadman_halt",
                drawdown_frac=round(drawdown, 4),
                floor_frac=floor_frac,
                start_of_day=round(start_of_day, 2),
                balance=round(balance, 2),
            )

        ctx.put("balance", balance)
        ctx.put("start_of_day", start_of_day)
        return allow(drawdown_frac=round(drawdown, 4))
