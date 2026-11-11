"""
A pending zone entry — the state machine, and nothing else.

Pure: no broker, no clock of its own, no I/O. Given a price it returns wait,
fill, or cancel. That makes the interesting behaviour testable without a market,
which the v1 version was not.

Two routes, two different theses about what the level will do
-------------------------------------------------------------

**fresh** — the level is expected to HOLD. Price comes back to it, holds, and
continues. Fill when price reclaims by `confirm_pts` after touching the zone.
Cancel if it breaks through by `invalidate_pts`: support that breaks is not
support.

**exhaustion** — the level is expected to be SWEPT and then reclaimed. This is
the flush-reversal: the move overshoots, traps the last entrants, and reverses.
So a sweep is *required* before a fill, and a mere touch of the level is not a
reason to cancel — it is the setup working. Only a collapse far past the level
counts as a real breakdown.

Getting those two backwards is the difference between buying a reversal and
catching a falling knife. v1 had this right and it is carried over exactly.

The stop is structural — beyond the level plus a buffer — which is why the
market-quality gates skip routed signals. They would judge this entry against a
market stop it is never going to use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

_BUY = ("up", "buy", "long")


def expiry_minutes(policy: Mapping[str, Any], timeframe: str) -> float:
    cfg = dict(policy.get("watcher") or {})
    table = dict(cfg.get("expiry_min_by_tf") or {})
    return float(table.get(str(timeframe),
                           cfg.get("expiry_min_default", 90)))


@dataclass(slots=True)
class PendingEntry:
    """One armed entry waiting for price to reach and confirm its zone."""

    signal_id: str
    direction: str
    zone_price: float
    stop_price: float
    timeframe: str
    session: str
    route_kind: str = "fresh"
    tp_hint: Optional[float] = None
    signal: dict = field(default_factory=dict)
    derived: dict = field(default_factory=dict)

    created_ts: float = 0.0
    expiry_min: float = 90.0
    reached_zone: bool = False
    swept: bool = False

    level_source: str = ""
    confirm_pts: float = 40.0
    invalidate_pts: float = 60.0
    zone_touch_pts: float = 25.0

    @classmethod
    def create(cls, *, policy: Mapping[str, Any], clock: Callable[[], float] = time.time,
               **kw: Any) -> "PendingEntry":
        cfg = dict(policy.get("watcher") or {})
        entry = cls(
            confirm_pts=float(cfg.get("confirm_pts", 40.0)),
            invalidate_pts=float(cfg.get("invalidate_pts", 60.0)),
            zone_touch_pts=float(cfg.get("zone_touch_pts", 25.0)),
            **kw,
        )
        entry.created_ts = clock()
        entry.expiry_min = expiry_minutes(policy, entry.timeframe)
        return entry

    # ── geometry ──────────────────────────────────────────────────────────────

    @property
    def is_buy(self) -> bool:
        return str(self.direction).lower() in _BUY

    @property
    def confirm_price(self) -> float:
        return (self.zone_price + self.confirm_pts if self.is_buy
                else self.zone_price - self.confirm_pts)

    @property
    def invalidate_price(self) -> float:
        return (self.zone_price - self.invalidate_pts if self.is_buy
                else self.zone_price + self.invalidate_pts)

    @property
    def stop_pts(self) -> float:
        """The distance actually risked. Not the market stop the gates saw."""
        return abs(self.zone_price - self.stop_price)

    def expired(self, now: float) -> bool:
        return (now - self.created_ts) > self.expiry_min * 60

    # ── the state machine ─────────────────────────────────────────────────────

    def evaluate(self, price: float) -> str:
        """Returns 'wait' | 'fill' | 'cancel'. Mutates reached_zone / swept."""
        if self.route_kind == "exhaustion":
            return self._exhaustion(price)
        return self._fresh(price)

    def _exhaustion(self, price: float) -> str:
        if self.is_buy:
            if price <= self.zone_price:
                self.swept = self.reached_zone = True
            # Only a collapse WELL past the level is a breakdown. Touching it is
            # the thesis working, not failing.
            if price <= self.invalidate_price - self.invalidate_pts:
                return "cancel"
            if self.swept and price >= self.confirm_price:
                return "fill"
            return "wait"

        if price >= self.zone_price:
            self.swept = self.reached_zone = True
        if price >= self.invalidate_price + self.invalidate_pts:
            return "cancel"
        if self.swept and price <= self.confirm_price:
            return "fill"
        return "wait"

    def _fresh(self, price: float) -> str:
        if self.is_buy:
            if not self.reached_zone:
                if price <= self.zone_price + self.zone_touch_pts:
                    self.reached_zone = True
                return "wait"
            if price <= self.invalidate_price:
                return "cancel"          # support broke
            if price >= self.confirm_price:
                return "fill"            # reclaimed — level held
            return "wait"

        if not self.reached_zone:
            if price >= self.zone_price - self.zone_touch_pts:
                self.reached_zone = True
            return "wait"
        if price >= self.invalidate_price:
            return "cancel"              # resistance broke
        if price <= self.confirm_price:
            return "fill"                # rejected — level held
        return "wait"

    # ── reporting ─────────────────────────────────────────────────────────────

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "direction": self.direction,
            "route_kind": self.route_kind,
            "level_source": self.level_source,
            "timeframe": self.timeframe,
            "session": self.session,
            "zone_price": round(self.zone_price, 2),
            "confirm_price": round(self.confirm_price, 2),
            "invalidate_price": round(self.invalidate_price, 2),
            "stop_price": round(self.stop_price, 2),
            "stop_pts": round(self.stop_pts),
            "reached_zone": self.reached_zone,
            "swept": self.swept,
            "age_min": round((now - self.created_ts) / 60, 1),
            "expiry_min": self.expiry_min,
        }
