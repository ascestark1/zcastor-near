"""
Sessions — when they start, and what price they opened at.

Session open is fetched from the BROKER, not the dashboard. The dashboard prices
off an exchange feed; the trade fills at the broker's number. A London bias check
that compares a broker execution price against an exchange session open is
comparing two different instruments and will be wrong by the basis.

Cached per session-day. A session's open price does not change once the session
has opened, so one fetch per session per day is correct — not a performance hack.
Returns 0.0 when unavailable, and the gates treat 0.0 as "cannot assess" and skip
rather than refusing on a number they do not have.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("zcastor.market.session")

# Minutes past UTC midnight.
SESSION_START_UTC_MIN = {
    "Asian": 0,
    "Pre-London": 7 * 60 + 30,
    "London": 8 * 60,
    "NY Open": 13 * 60 + 30,
    "NY PM": 16 * 60,
    "NY Close": 20 * 60,
    "Overnight": 22 * 60,
}

# How far into the session to sample for the opening price.
OPEN_WINDOW_SECONDS = 5 * 60


class SessionOpens:
    """
    Resolves and caches session opening prices.

    `rates_fn(from_ts, to_ts)` returns a list of bar dicts with a "close" key —
    supplied by the market port so this class needs no broker of its own and is
    testable with a two-line fake.
    """

    def __init__(
        self,
        rates_fn: Callable[[float, float], list],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._rates = rates_fn
        self._now = now
        self._cache: dict[str, float] = {}
        self._lock = threading.RLock()

    def open_price(self, session: str) -> float:
        start_min = SESSION_START_UTC_MIN.get(str(session).strip())
        if start_min is None:
            return 0.0

        now = self._now()
        key = f"{now:%Y-%m-%d}|{session}"

        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        start = now.replace(hour=start_min // 60, minute=start_min % 60,
                            second=0, microsecond=0)

        # A signal arriving before the session has opened has no open to compare
        # against. Do not reach back to yesterday's — that would silently answer
        # a different question.
        if start > now:
            return 0.0

        try:
            bars = self._rates(start.timestamp(),
                               start.timestamp() + OPEN_WINDOW_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("session-open fetch failed (%s): %s", session, exc)
            return 0.0

        if not bars:
            return 0.0

        try:
            price = float(bars[0]["close"])
        except (KeyError, IndexError, TypeError, ValueError):
            logger.debug("session-open bar unreadable (%s)", session)
            return 0.0

        if price <= 0:
            return 0.0

        with self._lock:
            self._cache[key] = price
        return price

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


def current_session(now: Optional[datetime] = None) -> str:
    """
    Which session is running. Used for labelling and reconciliation; the
    dashboard's own `session` field remains authoritative for a given signal,
    because that is what the agent saw.
    """
    now = now or datetime.now(timezone.utc)
    minutes = now.hour * 60 + now.minute
    current, best = "Overnight", -1
    for name, start in SESSION_START_UTC_MIN.items():
        if start <= minutes and start > best:
            current, best = name, start
    return current
