"""
Hourly swing levels.

The watcher takes its zones from the dashboard's banded support and resistance.
When those are absent — and they often are on fast timeframes — there is nothing
to arm against and the signal is refused for no better reason than a missing
field.

This derives levels from price itself: an hourly high with no higher high within
`k` bars either side is a swing high, and the mirror for lows. Crude by design.
A level is only worth having if it is the kind of thing many participants can
see, and a local extreme on the hourly chart is exactly that.

Measured over 48,488 M1 bars of August 2026, entering at these levels returned
+0.024R against -0.223R for the same stop and target entered at an arbitrary
price — an edge of roughly 0.25R. That is the only positive expectancy we have
found in this system, and it is why the watcher exists.

Two findings from the same test that shaped the defaults:

**Shorter windows beat longer ones.** 8h outperformed 24h at every buffer.
More time gives price more chances to reach the stop, and the stop wins ties.

**Targets should not scale with the stop.** A fixed ~400pt target beat 600 and
800 regardless of buffer. Widening the stop and widening the target with it
pushes the target out of reach inside the window.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("zcastor.market.levels")


class HourlyLevels:
    """
    Swing highs and lows from hourly bars, refreshed on a timer.

    `rates_fn(from_ts, to_ts)` returns M1 bars, which are aggregated here rather
    than requesting H1 from the broker — one code path, and the aggregation is
    identical to the analysis the parameters came from.
    """

    def __init__(
        self,
        rates_fn: Callable[[float, float], list],
        *,
        lookback_hours: int = 168,
        k: int = 3,
        refresh_seconds: float = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._rates = rates_fn
        self.lookback_hours = lookback_hours
        self.k = k
        self.refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._refreshed_at = 0.0

    # ── construction ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_hourly(bars: list) -> list[dict]:
        """Aggregate M1 into H1 buckets, keyed by the hour they fall in."""
        buckets: dict[int, dict] = {}
        for bar in bars:
            try:
                ts = int(float(bar["time"]))
                hour = ts - (ts % 3600)
                high, low = float(bar["high"]), float(bar["low"])
            except (KeyError, TypeError, ValueError):
                continue
            b = buckets.get(hour)
            if b is None:
                buckets[hour] = {"h": high, "l": low}
            else:
                b["h"] = max(b["h"], high)
                b["l"] = min(b["l"], low)
        return [buckets[h] for h in sorted(buckets)]

    def _find_swings(self, hourly: list[dict]) -> tuple[list[float], list[float]]:
        k = self.k
        highs, lows = [], []
        if len(hourly) < 2 * k + 1:
            return highs, lows
        H = [b["h"] for b in hourly]
        L = [b["l"] for b in hourly]
        for i in range(k, len(hourly) - k):
            window_h = H[i - k:i + k + 1]
            window_l = L[i - k:i + k + 1]
            if H[i] == max(window_h):
                highs.append(H[i])
            if L[i] == min(window_l):
                lows.append(L[i])
        return highs, lows

    def refresh(self, force: bool = False) -> bool:
        now = self._clock()
        if not force and now - self._refreshed_at < self.refresh_seconds:
            return False
        try:
            bars = self._rates(now - self.lookback_hours * 3600, now)
        except Exception as exc:  # noqa: BLE001
            logger.debug("level refresh failed: %s", exc)
            return False
        if not bars:
            return False

        highs, lows = self._find_swings(self._to_hourly(bars))
        with self._lock:
            self._highs, self._lows = highs, lows
            self._refreshed_at = now
        logger.info("hourly levels refreshed: %d resistance, %d support",
                    len(highs), len(lows))
        return True

    # ── queries ───────────────────────────────────────────────────────────────

    def nearest_support(self, price: float) -> Optional[float]:
        """Closest swing low BELOW price. None when there isn't one."""
        self.refresh()
        with self._lock:
            below = [lv for lv in self._lows if lv < price]
        return max(below) if below else None

    def nearest_resistance(self, price: float) -> Optional[float]:
        self.refresh()
        with self._lock:
            above = [lv for lv in self._highs if lv > price]
        return min(above) if above else None

    def stats(self) -> dict[str, Any]:
        """
        Levels load lazily, on the first query after a restart. Reporting an
        age measured from the epoch makes that look like a dead feed, which is
        how a restart got mistaken for a fault.
        """
        with self._lock:
            if not self._refreshed_at:
                return {"resistance": 0, "support": 0, "age_seconds": None,
                        "state": "not loaded yet"}
            return {"resistance": len(self._highs), "support": len(self._lows),
                    "age_seconds": round(self._clock() - self._refreshed_at),
                    "state": "loaded"}
