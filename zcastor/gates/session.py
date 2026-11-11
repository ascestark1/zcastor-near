"""
Session gate.

Some sessions are measured losers. NY PM was blocked, unblocked, then re-blocked
on 7–9 Jun after an 11% hit rate and roughly −845 points per signal. NY Close is
thin and gappy.

The blocked list is policy, not code — it is exactly the kind of parameter that
should be visible in the anchored policy record, so a counterparty can see which
sessions the system refused to trade and when that changed.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class SessionGate(Gate):
    name = "session"

    def evaluate(self, ctx: GateContext) -> Decision:
        session = str(ctx.signal.get("session", "")).strip()
        blocked = ctx.policy_value("blocked_sessions", ["NY PM", "NY Close"])

        # Compare case-insensitively: the dashboard has shipped both "NY PM"
        # and "NY pm" across builds, and an unmatched string here silently
        # unblocks a session that measurement says loses money.
        normalised = {str(s).strip().lower() for s in blocked}

        if session and session.lower() in normalised:
            return suppress("session_blocked", session=session)

        ctx.put("session", session)
        return allow(session=session or "unknown")
