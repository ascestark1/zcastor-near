"""
Deriving the zone and the structural stop.

This is where dashboard v16.1 earns its version bump. v16 sent only
`nearest_resistance_pts` / `nearest_support_pts` — distances, measured at the
moment the signal was emitted. By the time the watcher arms, price has moved, so
reconstructing the level as `price ± pts` places the zone at the wrong price.
v16.1 added `nearest_resistance_price` and `nearest_support_price`, the absolute
levels, which do not go stale.

So: use the absolute price when it is there, fall back to the distance when it is
not, and record which was used. A dossier should say whether its zone came from a
level or from arithmetic.

The stop is structural — beyond the level by `stop_buffer_pts`. That is the
whole point of routing: the trade is invalidated when the level fails, not when
an ATR-derived distance is crossed. It also means the stop can be wider than a
market stop, which is why the watcher re-checks viability at fill time rather
than trusting the gates' earlier arithmetic.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger("zcastor.entry.zones")

_BUY = ("up", "buy", "long")


class ZoneDerivation:
    def __init__(self, policy: Mapping[str, Any], levels: Any = None) -> None:
        cfg = dict(policy.get("watcher") or {})
        self.buffer = float(cfg.get("stop_buffer_pts", 120.0))
        self.min_pts = float(cfg.get("zone_min_pts", 80.0))
        self.max_pts = float(cfg.get("zone_max_pts", 600.0))
        self.max_stop = float(cfg.get("max_stop_pts", 720.0))
        # Swing levels derived from hourly bars, used when the dashboard sends
        # none. Measured at +0.25R over the same geometry entered arbitrarily,
        # so a signal with no dashboard level is worth arming, not discarding.
        self.levels = levels

    @staticmethod
    def _number(signal: Mapping[str, Any], key: str) -> Optional[float]:
        raw = signal.get(key)
        if raw is None or raw == "":
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _swing_level(self, price: float, is_buy: bool) -> Optional[float]:
        if self.levels is None:
            return None
        try:
            return (self.levels.nearest_support(price) if is_buy
                    else self.levels.nearest_resistance(price))
        except Exception as exc:  # noqa: BLE001
            logger.debug("hourly level lookup failed: %s", exc)
            return None

    def derive(self, signal: Mapping[str, Any], *,
               current_price: float) -> Optional[dict[str, Any]]:
        """
        Returns {zone_price, stop_price, source, distance_pts} or None.

        None means there is no usable level, and a zone entry without a level is
        just a market entry with extra steps — the caller should refuse rather
        than invent one.
        """
        is_buy = str(signal.get("direction", "")).strip().lower() in _BUY

        price_key = "nearest_support_price" if is_buy else "nearest_resistance_price"
        pts_key = "nearest_support_pts" if is_buy else "nearest_resistance_pts"

        absolute = self._number(signal, price_key)
        distance = self._number(signal, pts_key)

        if absolute is not None:
            zone = absolute
            source = "level_price"          # v16.1 — does not go stale
        elif distance is not None:
            zone = current_price - distance if is_buy else current_price + distance
            source = "level_pts"            # v16 fallback — already stale
            logger.debug("no absolute level for %s — derived zone from distance",
                         price_key)
        else:
            swing = self._swing_level(current_price, is_buy)
            if swing is None:
                return None
            zone, source = swing, "hourly_swing"

        gap = abs(current_price - zone)
        if gap < self.min_pts:
            logger.debug("zone %.2f is only %.0fpts away — too close to arm",
                         zone, gap)
            return None
        if gap > self.max_pts:
            logger.debug("zone %.2f is %.0fpts away — too far to be this signal's trade",
                         zone, gap)
            return None

        # The zone must be on the correct side of price. A "support" above the
        # market is a mislabelled level, and arming it would place a buy stop
        # where a buy limit was intended.
        if is_buy and zone >= current_price:
            logger.debug("support %.2f is above price %.2f — ignoring", zone, current_price)
            return None
        if not is_buy and zone <= current_price:
            logger.debug("resistance %.2f is below price %.2f — ignoring", zone, current_price)
            return None

        stop = zone - self.buffer if is_buy else zone + self.buffer

        if abs(zone - stop) > self.max_stop:
            logger.debug("structural stop %.0fpts exceeds ceiling", abs(zone - stop))
            return None

        return {"zone_price": zone, "stop_price": stop, "source": source,
                "distance_pts": round(gap)}
