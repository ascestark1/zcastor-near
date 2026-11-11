"""
Zone exhaustion, and flip confirmation.

An exhausted zone is not a bad signal. After N entries in one direction the move
is spent, and the high-probability trade is the flush-and-reclaim in the *other*
direction — which is a zone entry, not a market entry. So the gate ROUTES rather
than suppressing.

DELIBERATE CHANGE FROM v1: the gate no longer consults `WATCHER_ENABLED`. In v1
each routing gate read a deployment flag and chose route-or-suppress itself,
which put a config value inside a trading judgement and meant the recorded reason
depended on how the process happened to be launched. Here the gate always states
what it believes — "this belongs at the zone" — and the execution layer converts
a routed trace into a suppression with reason `watcher_disabled` if the watcher
is not running. The record then says both things truthfully: the gate routed, the
deployment could not honour it.

NOT IN THIS MODULE: closing exhausted same-direction positions. v1 did that
inline inside the zone gate. It is an action, not a decision, and it belongs in
`execution/`. Leaving it here would mean a gate placing orders — and would make
the gate untestable without a live broker.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, route, suppress


class ZoneGate(Gate):
    name = "zone"

    def evaluate(self, ctx: GateContext) -> Decision:
        price = ctx.need("mid")
        direction = ctx.need("direction").lower()
        timeframe = str(ctx.signal.get("timeframe", "1h"))

        status, count, reason = ctx.risk.check_zone(price, direction, timeframe)
        status = str(status or "").lower()

        ctx.put("zone_status", status)
        ctx.put("zone_count", count)

        # The dashboard also sends a zone_phase, computed from its own client-
        # side record of signals it emitted. Ours is computed from actual FILLS.
        # They will diverge — a signal that was refused never became an entry —
        # and the stop distance must follow the fills, so the server's phase
        # wins and the dashboard's survives only inside the origin hash.
        phase = getattr(ctx.risk, "zone_phase", None)
        if callable(phase):
            ctx.put("zone_phase", phase(price, direction, timeframe))

        if status == "blocked":
            return route(
                "exhaustion",
                zone_reason=reason,
                zone_count=count,
                timeframe=timeframe,
            )

        # "elevated" means the OPPOSITE direction is exhausted — this signal is
        # the flip, and it is the trade we want. "caution" is a warning only.
        return allow(zone_status=status, zone_count=count, zone_reason=reason)


class FlipConfirmGate(Gate):
    """
    The dashboard sets `zone_flip_pending` when the direction being signalled is
    exhausting. Entering that direction anyway is entering a move that is ending.

    The exception is `zone_status == "elevated"`: that means the opposite side is
    the exhausted one, so this signal *is* the confirmed flip and should proceed.
    """
    name = "flip_confirm"

    def evaluate(self, ctx: GateContext) -> Decision:
        flip_pending = bool(ctx.signal.get("zone_flip_pending", False))
        zone_status = ctx.get("zone_status", "")

        if not flip_pending:
            return allow(flip_pending=False)

        if zone_status == "elevated":
            return allow(flip_pending=True, flip_confirmed=True)

        return suppress(
            "flip_pending_no_confirmation",
            zone_status=zone_status or "unknown",
        )
