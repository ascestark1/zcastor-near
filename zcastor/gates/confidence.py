"""
Confidence — policy threshold.

Only `high` trades. This is a decision made under policy, not a malformed input,
so it SUPPRESSES and becomes a refusal record. The dashboard emits medium and low
signals too; the fact that the system declines nearly all of them is exactly the
behaviour the accountability thesis wants evidence of.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress

# Ordered weakest to strongest so a threshold comparison is meaningful.
_RANK = {"low": 0, "medium": 1, "high": 2}


class ConfidenceGate(Gate):
    name = "confidence"

    def evaluate(self, ctx: GateContext) -> Decision:
        raw = str(ctx.signal.get("confidence", "")).strip().lower()
        minimum = str(ctx.policy_value("min_confidence", "high")).strip().lower()

        got = _RANK.get(raw)
        floor = _RANK.get(minimum)

        if floor is None:
            # A policy file naming an unknown confidence level is an operator
            # error. Fail closed rather than silently trading everything.
            return suppress("policy_invalid_min_confidence", configured=minimum)

        if got is None:
            return suppress("confidence_unrecognised", received=raw or "<empty>")

        if got < floor:
            return suppress(f"confidence_{raw}", required=minimum)

        ctx.put("confidence", raw)
        return allow(confidence=raw)
