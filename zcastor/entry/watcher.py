"""
The entry watcher.

Holds armed zone entries and fires them when price confirms. This is the piece
the whole routing design exists for: on the same signals, market entry into a
50pt spread bled money while zone entry simulated positive. The gates identify
which signals belong at a level; this places them there.

Four ways an armed entry ends:

  fill      price confirmed the level. Order goes in with the STRUCTURAL stop.
  cancel    the level broke. No trade — the thesis was wrong before it cost us.
  expire    the timeframe's window passed without price reaching the zone.
  sweep     max hold or UTC day boundary. An entry armed yesterday is reasoning
            about a market that no longer exists.

Viability is re-checked at fill time, not trusted from arming time. The gates
deliberately skip the market-quality checks for routed signals because they
judge a market stop; the structural stop is different and often wider, so the
real risk question can only be answered here, with the real number.

Nothing in this module decides *whether* a signal should be a zone entry. The
gates did that. This decides *when*.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from .pending import PendingEntry
from .zones import ZoneDerivation

logger = logging.getLogger("zcastor.entry.watcher")


class EntryWatcher:
    def __init__(
        self,
        *,
        policy: Mapping[str, Any],
        market: Any,
        orders: Any,
        on_fill: Optional[Callable[[PendingEntry, dict], None]] = None,
        on_close: Optional[Callable[[PendingEntry, str], None]] = None,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        levels: Any = None,
        cooldown: Any = None,
    ) -> None:
        cfg = dict(policy.get("watcher") or {})
        self.policy = policy
        self.market = market
        self.orders = orders
        self.on_fill = on_fill
        self.on_close = on_close
        self._clock = clock
        self._now = now

        self.max_hold_hours = float(cfg.get("max_hold_hours", 24))
        self.day_boundary_close = bool(cfg.get("day_boundary_close", True))
        self.tp_r = float(cfg.get("tp_r", 1.33))
        # A FIXED target, not a multiple of the stop. Widening the stop and
        # widening the target with it pushes the target out of reach inside the
        # entry window: measured, a 400pt target beat 600 and 800 at every
        # buffer. Falls back to the R multiple only if no fixed value is set.
        self.target_pts = float(cfg.get("target_pts", 0))

        self.zones = ZoneDerivation(policy, levels=levels)
        self.cooldown = cooldown
        self._cooldown_seconds = float(policy.get("entry_cooldown_seconds", 900))
        self._cooldown_pts = float(policy.get("entry_cooldown_distance_pts", 400))
        self._pending: dict[str, PendingEntry] = {}
        self._lock = threading.RLock()
        self._armed_day = self._now().strftime("%Y-%m-%d")

    # ── arming ────────────────────────────────────────────────────────────────

    def arm(self, *, signal_id: str, signal: Mapping[str, Any],
            route_kind: str, derived: Mapping[str, Any]) -> dict:
        """
        Arm a zone entry. Called by the engine on a ROUTE verdict.

        Returns a description of what was armed, or {"error": ...} when no
        usable level exists — a zone entry with no level is a market entry with
        extra steps, and the engine records that honestly rather than pretending.
        """
        price = float(derived.get("execution_price") or derived.get("mid") or 0)
        if price <= 0:
            return {"error": "no price to arm against"}

        zone = self.zones.derive(signal, current_price=price)
        if zone is None:
            return {"error": "no usable level"}

        direction = str(derived.get("direction", signal.get("direction", ""))).lower()
        entry = PendingEntry.create(
            policy=self.policy,
            clock=self._clock,
            signal_id=signal_id,
            direction=direction,
            zone_price=zone["zone_price"],
            stop_price=zone["stop_price"],
            timeframe=str(signal.get("timeframe", "1h")),
            session=str(signal.get("session", "")),
            route_kind=route_kind or "fresh",
            signal=dict(signal),
            derived=dict(derived),
        )
        # Which kind of level this armed against. The +0.25R measurement was
        # specifically for HOURLY SWING levels; the dashboard's banded S&R is a
        # different thing and has never been measured. Without carrying the
        # source, the ledger cannot tell which one it is scoring.
        entry.level_source = zone["source"]

        with self._lock:
            if signal_id in self._pending:
                return {"error": "already armed"}
            self._pending[signal_id] = entry

        logger.info("armed %s %s at %.2f (stop %.2f, %s, %s)",
                    signal_id, direction.upper(), entry.zone_price,
                    entry.stop_price, entry.route_kind, zone["source"])

        return {"armed": True, "zone_price": entry.zone_price,
                "stop_price": entry.stop_price, "stop_pts": round(entry.stop_pts),
                "source": zone["source"], "distance_pts": zone["distance_pts"],
                "expiry_min": entry.expiry_min}

    def cancel(self, signal_id: str, reason: str) -> bool:
        with self._lock:
            entry = self._pending.pop(signal_id, None)
        if entry is None:
            return False
        logger.info("cancelled %s: %s", signal_id, reason)
        if self.on_close:
            try:
                self.on_close(entry, reason)
            except Exception:  # noqa: BLE001
                logger.exception("on_close hook failed")
        return True

    # ── polling ───────────────────────────────────────────────────────────────

    def poll(self) -> dict[str, int]:
        """
        One cycle. Never raises — the watcher must not take down the process
        that is still accepting signals.
        """
        self._sweep()

        with self._lock:
            entries = list(self._pending.values())
        if not entries:
            return {"pending": 0, "filled": 0, "cancelled": 0, "expired": 0}

        tick = self.market.get_tick()
        if not isinstance(tick, dict) or "error" in tick:
            logger.debug("no tick this cycle — holding all pending entries")
            return {"pending": len(entries), "filled": 0, "cancelled": 0,
                    "expired": 0, "stalled": 1}

        now = self._clock()
        filled = cancelled = expired = 0

        for entry in entries:
            if entry.expired(now):
                self.cancel(entry.signal_id, "expired")
                expired += 1
                continue

            # Evaluate against the side that would actually fill.
            price = float(tick["ask"] if entry.is_buy else tick["bid"])
            verdict = entry.evaluate(price)

            if verdict == "cancel":
                self.cancel(entry.signal_id, "level_broke")
                cancelled += 1
            elif verdict == "fill":
                if self._fire(entry, price):
                    filled += 1
                else:
                    cancelled += 1

        with self._lock:
            remaining = len(self._pending)
        return {"pending": remaining, "filled": filled,
                "cancelled": cancelled, "expired": expired}

    # ── firing ────────────────────────────────────────────────────────────────

    def _fire(self, entry: PendingEntry, price: float) -> bool:
        """
        Place the order. Viability is re-checked HERE with the structural stop,
        because that is the distance actually being risked and no earlier gate
        ever saw it.
        """
        ok, reason = self._viable(entry)
        if not ok:
            self.cancel(entry.signal_id, f"not_viable_at_fill:{reason}")
            return False

        # Two armed entries at nearby levels can confirm minutes apart — one
        # trade at double size, with the correlation hidden.
        if self.cooldown is not None and self.cooldown.blocks(
                price, "BUY" if entry.is_buy else "SELL",
                self._cooldown_seconds, self._cooldown_pts):
            self.cancel(entry.signal_id, "entry_cooldown")
            return False

        target = self._target(entry, price)
        result = self.orders.market_order(
            side="BUY" if entry.is_buy else "SELL",
            price=price, sl=entry.stop_price, tp=target,
        )

        if "error" in result:
            logger.error("watcher fill failed for %s: %s",
                         entry.signal_id, result["error"])
            self.cancel(entry.signal_id, f"order_failed:{result['error']}")
            return False

        with self._lock:
            self._pending.pop(entry.signal_id, None)

        logger.info("watcher filled %s at %.2f (zone %.2f, stop %.0fpts)",
                    entry.signal_id, price, entry.zone_price, entry.stop_pts)

        if self.on_fill:
            try:
                self.on_fill(entry, result)
            except Exception:  # noqa: BLE001
                logger.exception("on_fill hook failed for %s", entry.signal_id)
        return True

    def _viable(self, entry: PendingEntry) -> tuple[bool, str]:
        balance = self.market.get_balance()
        if not balance or balance <= 0:
            return False, "balance_unavailable"

        volume = float(getattr(self.orders, "volume", 0.01))
        risk = entry.stop_pts * volume
        cap = float(self.policy.get("max_trade_risk_frac", 0.30))
        if risk / balance > cap:
            return False, f"risk_{risk / balance:.2f}_above_{cap}"

        free_margin = self.market.get_free_margin()
        if not free_margin or free_margin <= 0:
            return False, "free_margin_unavailable"
        return True, ""

    def _target(self, entry: PendingEntry, price: float) -> Optional[float]:
        if entry.tp_hint:
            return float(entry.tp_hint)
        distance = self.target_pts or entry.stop_pts * self.tp_r
        return price + distance if entry.is_buy else price - distance

    # ── sweeps ────────────────────────────────────────────────────────────────

    def _sweep(self) -> None:
        """Max hold, and the UTC day boundary."""
        now = self._clock()
        limit = self.max_hold_hours * 3600

        with self._lock:
            stale = [e.signal_id for e in self._pending.values()
                     if (now - e.created_ts) > limit]
        for signal_id in stale:
            self.cancel(signal_id, "max_hold")

        if not self.day_boundary_close:
            return

        today = self._now().strftime("%Y-%m-%d")
        if today == self._armed_day:
            return

        with self._lock:
            carried = list(self._pending)
        for signal_id in carried:
            self.cancel(signal_id, "day_boundary")
        self._armed_day = today

    # ── reporting ─────────────────────────────────────────────────────────────

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def snapshot(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            return [e.snapshot(now) for e in self._pending.values()]
