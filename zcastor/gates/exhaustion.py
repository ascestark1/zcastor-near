"""
Session exhaustion.

If price has already travelled more than `session_exhaustion_frac` of the
session's typical range in the direction being signalled, the move has largely
happened. Entering now buys the last third and takes the full retrace.

Measured in broker prices against the broker's session open — not exchange
prices. The trade fills at the broker's number, so that is the number that
decides whether the move is spent.

`runs_when_routed = False`. A session that has spent its range is not the same
claim as a zone that has exhausted: the watcher's exhaustion-flush entry is
*designed* to trade the end of a move, on confirmation of the reversal. Applying
this gate to a routed signal would suppress precisely the setup the routing
existed to capture.

The typical-range table is policy and is anchored with it. The values are still
placeholders tuned by eye, not measured from desk data — worth stating plainly
in any dossier that cites this gate.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress

_UP = ("up", "buy", "long")


class SessionExhaustionGate(Gate):
    name = "session_exhaustion"
    runs_when_routed = False
    expensive = True          # fetches the broker's session open

    def evaluate(self, ctx: GateContext) -> Decision:
        session = str(ctx.signal.get("session", ""))
        table = ctx.policy_value("session_typical_range_pts", {}) or {}
        typical = float(table.get(session, 0) or 0)

        if typical <= 0:
            return allow(session=session or "unknown", reason="no_typical_range")

        session_open = float(ctx.market.session_open_price(session) or 0.0)
        if session_open <= 0:
            return allow(session=session, reason="session_open_unavailable")

        execution_price = ctx.need("execution_price")
        direction = ctx.need("direction").lower()

        signed_move = execution_price - session_open
        in_dir_move = signed_move if direction in _UP else -signed_move

        frac = float(ctx.policy_value("session_exhaustion_frac", 0.6))
        threshold = typical * frac

        if in_dir_move >= threshold:
            return suppress(
                "session_exhausted",
                session=session,
                in_dir_move_pts=round(in_dir_move),
                threshold_pts=round(threshold),
                typical_pts=round(typical),
            )

        return allow(session=session, in_dir_move_pts=round(in_dir_move),
                     threshold_pts=round(threshold))
