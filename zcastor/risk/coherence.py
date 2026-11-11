"""
Coherence, and the risk facade the gates actually hold.

SIGNAL coherence asks whether the model is contradicting itself on one
timeframe: UP on 15m, then DOWN on 15m minutes later. That is oscillation, and
trading both sides of it pays the spread twice to finish flat.

MODEL coherence asks whether recent predictions have been any good. It is
advisory by design — a low win rate is a reason for you to look, not for the
system to unilaterally stop. The dead-man gate is the automated backstop.

`RiskAdapter` is the single object gates receive. It exists so gates depend on
one small interface rather than on a zone tracker, a coherence file and a
journal, and so the whole risk surface can be faked in a test with four methods.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("zcastor.risk")


class SignalCoherence:
    """
    Recent directional calls per timeframe, held in memory.

    Deliberately not persisted. A contradiction matters within a session; a
    contradiction against something the model said two days ago is not
    information, and a file would quietly make it one.
    """

    def __init__(self, policy: Mapping[str, Any],
                 clock: Callable[[], float] = None) -> None:
        cfg = dict(policy.get("coherence") or {})
        self.window = int(cfg.get("window", 10))
        self._recent: dict[str, deque] = {}
        self._lock = threading.RLock()
        import time as _time
        self._clock = clock or _time.time

    def check(self, direction: str, timeframe: str) -> tuple[str, str]:
        tf = str(timeframe)
        direction = str(direction).lower()
        with self._lock:
            history = list(self._recent.get(tf, ()))

        if not history:
            return "ok", ""

        opposed = [d for d in history[-3:] if d != direction]
        if len(opposed) >= 2:
            return "caution", (f"{tf} contradiction — last {len(history[-3:])} "
                               f"calls disagree with {direction.upper()}")
        return "ok", ""

    def record(self, direction: str, timeframe: str) -> None:
        tf = str(timeframe)
        with self._lock:
            queue = self._recent.setdefault(tf, deque(maxlen=self.window))
            queue.append(str(direction).lower())


class ModelCoherence:
    """
    Rolling win rate over recent resolved signal outcomes.

    Reads the outcomes file the resolver writes. Missing or unreadable is "ok"
    with n=0 — an advisory check must never become a source of failure.
    """

    def __init__(self, policy: Mapping[str, Any], path: str | Path) -> None:
        cfg = dict(policy.get("coherence") or {})
        self.window = int(cfg.get("window", 10))
        self.min_wr = float(cfg.get("min_win_rate", 0.40))
        self.min_samples = int(cfg.get("min_samples", 5))
        self.path = Path(path)

    def check(self) -> tuple[str, float, int]:
        history = self._load()
        n = len(history)
        if n < self.min_samples:
            return "ok", 0.0, n

        recent = history[-self.window:]
        wins = sum(1 for r in recent if r)
        wr = wins / len(recent)
        status = "caution" if wr < self.min_wr else "ok"
        return status, wr, len(recent)

    def _load(self) -> list[bool]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("model coherence unreadable: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        return [bool(r.get("correct")) for r in data
                if isinstance(r, dict) and "correct" in r]


class RiskAdapter:
    """The `ctx.risk` the gates hold. Nothing else is exposed to them."""

    def __init__(
        self,
        *,
        zones: Any,
        signal_coherence: Any,
        model_coherence: Any,
        journal: Any = None,
    ) -> None:
        self.zones = zones
        self._signal = signal_coherence
        self._model = model_coherence
        self._journal = journal

    # ── the four methods gates call ──
    def check_zone(self, price: float, direction: str, timeframe: str) -> tuple:
        return self.zones.check(price, direction, timeframe)

    def check_signal_coherence(self, direction: str, timeframe: str) -> tuple:
        return self._signal.check(direction, timeframe)

    def check_model_coherence(self) -> tuple:
        return self._model.check()

    def zone_phase(self, price: float, direction: str, timeframe: str) -> str:
        """
        Zone fill state, for stop placement. Computed from actual FILLS, unlike
        the dashboard's own value which counts signals it emitted.
        """
        return self.zones.phase(price, direction, timeframe)

    def today_pnl(self) -> float:
        """
        Realised PnL since UTC midnight. Zero when there is no journal — the
        dead-man gate then reads start-of-day as equal to balance, which is the
        correct reading of "no trades booked today".
        """
        if self._journal is None:
            return 0.0
        return float(self._journal.today_pnl())

    # ── post-trade updates ──
    def record_entry(self, price: float, direction: str, timeframe: str,
                     bounce_pts: float = 0.0) -> None:
        self.zones.record_entry(price, direction, timeframe, bounce_pts)
        self._signal.record(direction, timeframe)


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
