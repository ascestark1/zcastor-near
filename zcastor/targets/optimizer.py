"""
Target optimiser — where the stop and the target come from.

Ported from v1's `tp_optimizer`, with one structural change: **every constant now
lives in `policy.json`**. In v1 the tables were module-level literals, so the
anchored policy record described only part of the decision function and two
signals with identical origin hashes could produce different targets with no
visible cause. Proof of Process has to cover the whole reasoning, tables
included.

The stop, in order
------------------
1. Each timeframe has a natural noise range. The stop must land inside it
   whatever ATR says — a 5m stop of 400 points is not a 5m trade.
2. Zone phase places it within that range: fresh gets room (0.90 of the way to
   max), exhausting gets a tight exit (0.30).
3. ATR contributes, scaled by regime — volatile widens, ranging tightens — and
   is clamped to the timeframe bounds before it is blended 60/40 with the phase
   target.
4. If the opposing level sits inside the timeframe bounds, it anchors the stop
   at 70% weight plus a buffer. Structure beats arithmetic when structure is
   actually there. Outside the bounds it is ignored rather than obeyed.

The target
----------
Structure first: the opposing level minus a buffer, but only if that clears
minimum R:R. Otherwise a session-based R:R multiple. Then the timeframe ceiling
caps it, the R:R floor lifts it, and in ranging or volatile conditions on 5m and
15m an additional cap applies — those regimes do not deliver sustained moves, and
targeting one is how you turn a winning read into a losing trade.

Both return None rather than a guess when there is no usable price. The
TargetStage turns that into a REJECT.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger("zcastor.targets")

_TF_ALIASES = {
    "5": "5m", "5m": "5m",
    "15": "15m", "15m": "15m",
    "30": "30m", "30m": "30m",
    "1h": "1h", "60": "1h", "60m": "1h",
    "4h": "4h", "240": "4h", "240m": "4h",
}
_PHASES = ("fresh", "active", "exhausting")


def normalise_tf(tf: Any) -> str:
    return _TF_ALIASES.get(str(tf).strip().lower(), "1h")


class TargetOptimizer:
    """
    Pure computation over a signal and the policy. No I/O, no clock, no broker —
    so the same inputs always produce the same targets, which is what makes a
    dossier's thesis reproducible by someone else.
    """

    def __init__(self, policy: Mapping[str, Any]) -> None:
        cfg = dict(policy.get("targets") or {})
        self.min_rr = float(cfg.get("min_risk_reward", 1.5))
        self.sl_range = {k: tuple(v) for k, v in
                         (cfg.get("tf_sl_range_pts") or {}).items()}
        self.tp_ceiling = dict(cfg.get("tf_tp_ceiling_pts") or {})
        self.phase_frac = dict(cfg.get("phase_fraction") or {})
        self.session_rr = dict(cfg.get("session_rr") or {})
        self.atr_bias = dict(cfg.get("regime_atr_bias") or {})
        self.rr_cap = dict(cfg.get("regime_rr_cap") or {})
        self.rr_cap_tfs = {str(t) for t in (cfg.get("regime_rr_cap_timeframes") or [])}
        self.buffer = float(cfg.get("sr_buffer_pts", 15))
        self.phase_blend = float(cfg.get("phase_blend", 0.6))
        self.sr_blend = float(cfg.get("sr_blend", 0.7))
        self.atr_weight = float(cfg.get("atr_half_weight", 0.5))

    # ── inputs ────────────────────────────────────────────────────────────────

    def _phase(self, signal: Mapping[str, Any]) -> str:
        p = str(signal.get("zone_phase", "fresh")).strip().lower()
        return p if p in _PHASES else "fresh"

    def _bounds(self, tf: str) -> tuple[float, float]:
        lo, hi = self.sl_range.get(tf, (180, 350))
        return float(lo), float(hi)

    @staticmethod
    def _level(signal: Mapping[str, Any], key: str) -> float:
        """
        Read a level in points.

        Absence must not become zero. v1 wrote
        `float(signal.get(a) if up else signal.get(b) or 0)`, where `or 0` binds
        to the else branch only — an UP signal with no resistance passed
        float(None) and crashed live on 15 Jun.
        """
        raw = signal.get(key)
        if raw is None or raw == "":
            return 0.0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return value if value > 0 else 0.0

    # ── stop ──────────────────────────────────────────────────────────────────

    def sl_pts(self, signal: Mapping[str, Any]) -> float:
        tf = normalise_tf(signal.get("timeframe", "1h"))
        lo, hi = self._bounds(tf)

        frac = float(self.phase_frac.get(self._phase(signal), 0.9))
        phase_target = lo + (hi - lo) * frac

        atr = float(signal.get("atr") or 200)
        bias = float(self.atr_bias.get(
            str(signal.get("regime", "unknown")).strip().lower(), 1.0))
        atr_scaled = max(lo, min(hi, atr * self.atr_weight * bias))

        blended = self.phase_blend * phase_target + (1 - self.phase_blend) * atr_scaled
        stop = max(lo, min(hi, blended))

        direction = str(signal.get("direction", "")).strip().upper()
        opposing = self._level(
            signal,
            "nearest_support_pts" if direction == "UP" else "nearest_resistance_pts",
        )

        if opposing > 0:
            anchored = opposing + self.buffer
            if lo <= anchored <= hi:
                stop = max(lo, min(hi, self.sr_blend * anchored
                                   + (1 - self.sr_blend) * stop))
            else:
                logger.debug("sl: level %.0f outside %s bounds %.0f-%.0f — ignored",
                             anchored, tf, lo, hi)

        return float(stop)

    def compute_sl(self, signal: Mapping[str, Any],
                   fallback_price: Optional[float] = None) -> Optional[float]:
        direction = str(signal.get("direction", "")).strip().upper()
        entry = float(fallback_price or signal.get("entry_price") or 0)
        if entry <= 0:
            return None
        if direction not in ("UP", "DOWN"):
            return None

        distance = self.sl_pts(signal)
        return float(entry - distance if direction == "UP" else entry + distance)

    # ── target ────────────────────────────────────────────────────────────────

    def compute_tp(self, signal: Mapping[str, Any],
                   fallback_price: Optional[float] = None) -> Optional[float]:
        direction = str(signal.get("direction", "")).strip().upper()
        entry = float(fallback_price or signal.get("entry_price") or 0)
        if entry <= 0 or direction not in ("UP", "DOWN"):
            return None

        stop = self.sl_pts(signal)
        tf = normalise_tf(signal.get("timeframe", "1h"))
        ceiling = float(self.tp_ceiling.get(tf, 800))

        session_rr = float(self.session_rr.get(
            str(signal.get("session", "")).strip(), self.min_rr))
        rr = max(session_rr, self.min_rr)

        level = self._level(
            signal,
            "nearest_resistance_pts" if direction == "UP" else "nearest_support_pts",
        )

        # Structure first, but only if it clears minimum R:R. A level closer
        # than the stop times min_rr is not a target, it is an obstacle.
        if level > 0 and (level - self.buffer) >= stop * self.min_rr:
            target = level - self.buffer
        else:
            target = stop * rr

        target = min(target, ceiling)
        target = max(target, stop * self.min_rr)

        regime = str(signal.get("regime", "unknown")).strip().lower()
        cap = self.rr_cap.get(regime)
        if cap is not None and tf in self.rr_cap_tfs:
            target = max(min(target, stop * float(cap)), stop * self.min_rr)

        if target <= 0:
            target = stop * self.min_rr

        return float(entry + target if direction == "UP" else entry - target)

    # ── introspection ─────────────────────────────────────────────────────────

    def explain(self, signal: Mapping[str, Any],
                price: float) -> dict[str, Any]:
        """
        Why these numbers. Goes in the dossier's thesis block so a reader can
        follow the derivation without re-running the code.
        """
        tf = normalise_tf(signal.get("timeframe", "1h"))
        lo, hi = self._bounds(tf)
        stop = self.sl_pts(signal)
        tp = self.compute_tp(signal, fallback_price=price)
        tp_pts = abs(tp - price) if tp is not None else 0.0
        return {
            "timeframe": tf,
            "phase": self._phase(signal),
            "sl_bounds_pts": [lo, hi],
            "sl_pts": round(stop),
            "tp_pts": round(tp_pts),
            "risk_reward": round(tp_pts / stop, 2) if stop else None,
            "tp_ceiling_pts": self.tp_ceiling.get(tf),
            "regime": str(signal.get("regime", "unknown")).strip().lower(),
            "session_rr": self.session_rr.get(
                str(signal.get("session", "")).strip()),
        }
