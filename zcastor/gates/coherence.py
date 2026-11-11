"""
Coherence.

SignalCoherence blocks same-timeframe contradictions: the model saying UP on 15m
and then DOWN on 15m shortly after is oscillation, not conviction, and trading
both sides of it pays the spread twice to end up flat.

The exception is a confirmed zone flip. When a zone is exhausted, the opposite
signal is not the model contradicting itself — it is the model correctly calling
the reversal. Suppressing it would kill the single highest-value setup the system
produces.

ModelCoherence is ADVISORY. It observes recent win rate and records it, but a
low win rate is a reason to investigate, not a reason for the system to
unilaterally stop trading — that judgement is yours, and the dead-man gate is the
automated backstop.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class SignalCoherenceGate(Gate):
    name = "signal_coherence"

    def evaluate(self, ctx: GateContext) -> Decision:
        direction = ctx.need("direction").lower()
        timeframe = str(ctx.signal.get("timeframe", "1h"))

        status, reason = ctx.risk.check_signal_coherence(direction, timeframe)
        status = str(status or "").lower()

        if status != "caution":
            return allow(coherence=status)

        is_flip = (
            bool(ctx.signal.get("zone_flip_pending", False))
            or ctx.get("zone_status") == "elevated"
        )

        if is_flip:
            return allow(
                coherence="caution_overridden",
                reason=reason,
                zone_status=ctx.get("zone_status", ""),
            )

        return suppress("coherence_block", detail_reason=reason)


class ModelCoherenceGate(Gate):
    name = "model_coherence"
    advisory = True

    def evaluate(self, ctx: GateContext) -> Decision:
        status, win_rate, n = ctx.risk.check_model_coherence()
        return allow(
            model_status=str(status or "").lower(),
            win_rate=round(float(win_rate or 0.0), 4),
            sample_n=int(n or 0),
        )
