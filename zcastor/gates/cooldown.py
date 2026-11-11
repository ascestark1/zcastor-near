"""
Entry cooldown.

Two signals arriving close together on one instrument are not two trades. They
are one trade at double size, with the correlation hidden from every risk check
that looks at position count rather than exposure.

This happened. On 31 August two orders filled in the same second at 77948.55 and
77941.25, then two more a minute apart at 77861 and 77867. Four entries on
essentially one setup, and they cost 16.53 of a 51.46 total loss — a third of it,
from a defect rather than from being wrong about direction.

The stack limit did not catch it because five positions were permitted and only
four were open. Position count is the wrong measure: five entries at the same
price in the same minute is one position at five times the size, and no gate in
the stack was asking about that.

So: after a fill, refuse further market entries for a cooldown period. The
watcher is exempt — an armed entry has already waited for its level and its
fill is the confirmation, not an impulse.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .base import Gate, GateContext, Decision, allow, suppress


def _side(direction: str) -> str:
    """
    One vocabulary for direction.

    The gate context carries UP/DOWN; order results carry BUY/SELL. This gate
    compared one against the other, so "UP" != "BUY" was always true and every
    same-direction entry was classified as a reversal and let through. The
    cooldown never blocked a single entry in production. Its tests passed
    because they used SELL on both sides — they agreed with each other, not
    with the code that calls them.
    """
    d = str(direction).strip().upper()
    return {"UP": "BUY", "LONG": "BUY", "DOWN": "SELL", "SHORT": "SELL"}.get(d, d)


class EntryCooldownGate(Gate):
    name = "entry_cooldown"
    # A routed signal fires only when price confirms at a level, which is its
    # own timing control. Cooling those down would suppress the one entry type
    # that measured positive.
    runs_when_routed = False

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._last_fill: float = 0.0
        self._last_price: float = 0.0
        self._last_direction: str = ""

    def record_fill(self, price: float, direction: str) -> None:
        """Called by the engine after an order fills."""
        self._last_fill = self._clock()
        self._last_price = float(price)
        self._last_direction = _side(direction)

    def blocks(self, price: float, direction: str,
               seconds: float = 900, distance_pts: float = 400) -> bool:
        """
        Would a fill here compound the last one?

        Watcher fills never pass through the gate pipeline — the watcher waits
        for a level and places the order itself, sometimes hours after the
        signal — so this lets it ask the question the gate asks.
        """
        if not self._last_fill:
            return False
        if self._clock() - self._last_fill >= seconds:
            return False
        if _side(direction) != self._last_direction:
            return False
        return abs(float(price) - self._last_price) <= distance_pts

    def evaluate(self, ctx: GateContext) -> Decision:
        if not self._last_fill:
            return allow(cooldown=False)

        seconds = float(ctx.policy_value("entry_cooldown_seconds", 900))
        elapsed = self._clock() - self._last_fill
        if elapsed >= seconds:
            return allow(cooldown=False, since_last_fill=round(elapsed))

        # Inside the window, only refuse entries that would compound the same
        # exposure: same direction, and close enough in price to be the same
        # trade. An opposite-direction entry is a reversal, not a double-up.
        direction = _side(ctx.need("direction"))
        if direction != self._last_direction:
            return allow(cooldown=True, reason="opposite_direction")

        price = ctx.need("execution_price")
        distance = abs(price - self._last_price)
        threshold = float(ctx.policy_value("entry_cooldown_distance_pts", 400))
        if distance > threshold:
            return allow(cooldown=True, distance_pts=round(distance))

        return suppress(
            "entry_cooldown",
            since_last_fill=round(elapsed),
            cooldown_seconds=round(seconds),
            distance_pts=round(distance),
            last_direction=self._last_direction,
        )
