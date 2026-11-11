"""
Zone memory.

A "zone" is a 300-point price bucket, tracked per direction and per timeframe.
Each time a trade is entered in a zone it is recorded; enough entries within the
window and the zone is spent.

Three ideas from v1 worth stating plainly, because they are the interesting part:

**Higher timeframes suppress lower ones.** If the 1h zone is exhausted, the 30m,
15m and 5m zones in the same direction are too — they are looking at the same
price from closer up. Without this, a spent move gets re-entered five times on
five timeframes and each one looks like a fresh signal.

**Time compression is exhaustion.** Four entries over two hours is a zone being
worked. Three entries in thirty minutes is a move being chased. The fast window
catches the second case before the slow count would.

**Weak bounces mean reducing capacity.** If price keeps returning to a level and
bouncing less each time, the level is failing even though the entry count is
still low. Average bounce under the threshold downgrades a fresh zone to caution.

State is in memory and deliberately so: entries older than the window are
irrelevant, and a restart losing two hours of zone history is a smaller problem
than a stale file claiming a zone is spent when the market has moved on.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

_TF_ALIASES = {
    "5": "5m", "5m": "5m", "15": "15m", "15m": "15m",
    "30": "30m", "30m": "30m", "1h": "1h", "60": "1h", "60m": "1h",
    "4h": "4h", "240": "4h", "240m": "4h",
}


def normalise_tf(tf: Any) -> str:
    key = str(tf).strip().lower()
    return _TF_ALIASES.get(key, key)


class ZoneTracker:
    def __init__(self, policy: Mapping[str, Any],
                 clock: Callable[[], float] = time.time) -> None:
        cfg = dict(policy.get("zones") or {})
        self.size = float(cfg.get("zone_size_pts", 300))
        self.window = float(cfg.get("window_seconds", 7200))
        self.fast_window = float(cfg.get("fast_window_seconds", 1800))
        self.caution_n = int(cfg.get("caution_entries", 2))
        self.block_n = int(cfg.get("block_entries", 4))
        self.compress_n = int(cfg.get("fast_compress_entries", 3))
        self.weak_bounce = float(cfg.get("weak_bounce_pts", 50))
        self.hierarchy = list(cfg.get("tf_hierarchy") or
                              ["5m", "15m", "30m", "1h", "4h"])
        self.suppresses = dict(cfg.get("tf_suppresses") or {})

        self._clock = clock
        self._entries: dict[str, list[dict[str, float]]] = {}
        self._lock = threading.RLock()

    # ── internals ─────────────────────────────────────────────────────────────

    def _key(self, price: float, direction: str, tf: str) -> str:
        bucket = int(price / self.size) * int(self.size)
        return f"{bucket}_{str(direction).lower()}_{normalise_tf(tf)}"

    def _live(self, price: float, direction: str, tf: str) -> list:
        key = self._key(price, direction, tf)
        cutoff = self._clock() - self.window
        with self._lock:
            entries = [e for e in self._entries.get(key, []) if e["ts"] > cutoff]
            self._entries[key] = entries
            return list(entries)

    def _compressed(self, entries: list) -> bool:
        cutoff = self._clock() - self.fast_window
        return len([e for e in entries if e["ts"] > cutoff]) >= self.compress_n

    @staticmethod
    def _avg_bounce(entries: list) -> float:
        values = [e["bounce_pts"] for e in entries if e["bounce_pts"] > 0]
        return sum(values) / len(values) if values else 0.0

    # ── public ────────────────────────────────────────────────────────────────

    def check(self, price: float, direction: str,
              timeframe: str = "1h") -> tuple[str, int, str]:
        """
        Returns (status, count, reason).

        status is one of:
          ok        nothing notable
          caution   the zone is being worked, or bounces are weakening
          blocked   spent — route to the watcher for a confirmed entry
          elevated  the OPPOSITE direction is spent, so this signal is the flip
        """
        tf = normalise_tf(timeframe)
        own = self._live(price, direction, tf)
        n = len(own)

        # Higher timeframes first — a spent 4h zone blocks everything under it.
        #
        # v1 wrote `for htf in hierarchy: if htf == tf: break`, and the
        # hierarchy is ordered ascending — so a 15m signal broke at index 1 and
        # only ever examined 5m, the timeframes BELOW it. The suppression never
        # fired. Walk strictly upward instead.
        try:
            own_rank = self.hierarchy.index(tf)
        except ValueError:
            own_rank = -1

        if own_rank >= 0:
            for htf in self.hierarchy[own_rank + 1:]:
                if tf not in self.suppresses.get(htf, []):
                    continue
                higher = self._live(price, direction, htf)
                if len(higher) >= self.block_n or self._compressed(higher):
                    return "blocked", len(higher), f"{htf} exhausted — suppressing {tf}"

        if n >= self.block_n or self._compressed(own):
            reason = (f"{tf} fast exhaustion ({n} entries in "
                      f"{int(self.fast_window / 60)}min)"
                      if self._compressed(own)
                      else f"{tf} zone exhausted ({n} entries)")
            return "blocked", n, reason

        if n >= self.caution_n:
            return "caution", n, (f"{tf} zone active ({n} entries, avg bounce "
                                  f"{self._avg_bounce(own):.0f}pts)")

        avg = self._avg_bounce(own)
        if 0 < avg < self.weak_bounce:
            return "caution", n, f"{tf} weak bounces ({avg:.0f}pts) — capacity reducing"

        opposite = "down" if str(direction).lower() == "up" else "up"
        other = self._live(price, opposite, tf)
        if len(other) >= self.block_n or self._compressed(other):
            return "elevated", n, f"opposite {opposite} exhausted — {direction} is the flip"

        return "ok", n, ""

    def record_entry(self, price: float, direction: str, timeframe: str,
                     bounce_pts: float = 0.0) -> None:
        """Called after a fill, never before. An intention is not an entry."""
        key = self._key(price, direction, timeframe)
        cutoff = self._clock() - self.window
        with self._lock:
            entries = [e for e in self._entries.get(key, []) if e["ts"] > cutoff]
            entries.append({"ts": self._clock(), "bounce_pts": float(bounce_pts)})
            self._entries[key] = entries

    def reset_zone(self, price: float) -> int:
        """
        Clear every direction and timeframe for this bucket.

        Used on a confirmed flip: once the market has genuinely turned, the
        old zone's history describes a regime that no longer exists.
        """
        bucket = int(price / self.size) * int(self.size)
        prefix = f"{bucket}_"
        with self._lock:
            keys = [k for k in self._entries if k.startswith(prefix)]
            for k in keys:
                self._entries.pop(k, None)
        return len(keys)

    def phase(self, price: float, direction: str, timeframe: str = "1h") -> str:
        """Zone phase, for the target optimiser's stop placement."""
        n = len(self._live(price, direction, timeframe))
        if n >= self.block_n:
            return "exhausting"
        if n >= self.caution_n:
            return "active"
        return "fresh"
