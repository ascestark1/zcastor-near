"""
Direction — payload integrity.

The only gate in batch one whose failure is genuinely an unusable input rather
than a decision. A signal without a parseable direction is not a trade the
system declined; it is a malformed message. REJECT, no record.

Normalises the direction into `derived` so no later gate parses it again. v1 had
`direction`, `dir_lower` and repeated `.upper()` calls scattered through
process_signal, which is how a lowercase "up" from a dashboard build once slipped
past a comparison against "UP".
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, reject


class DirectionGate(Gate):
    name = "direction"

    def evaluate(self, ctx: GateContext) -> Decision:
        raw = ctx.signal.get("direction")
        if raw is None:
            return reject("direction_missing")

        normalised = str(raw).strip().upper()
        allowed = ctx.policy_value("allowed_directions", ["UP", "DOWN"])

        if normalised not in allowed:
            return reject("direction_invalid", received=str(raw))

        ctx.put("direction", normalised)
        ctx.put("is_buy", normalised == "UP")
        return allow(direction=normalised)
