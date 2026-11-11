"""
Regime vocabulary, shared by the regime-context and London gates.

The dashboard has emitted several spellings across v9–v16.1 ("range", "ranging",
"rangebound", "chop"). v1 normalised in one place and compared raw strings in
another, so a signal could be treated as ranging by one gate and unknown by the
next. One normaliser, used by both.
"""

from __future__ import annotations

_ALIASES = {
    "range": "ranging",
    "ranging": "ranging",
    "rangebound": "ranging",
    "range-bound": "ranging",
    "chop": "ranging",
    "choppy": "ranging",
    "trend": "trending",
    "trending": "trending",
    "volatile": "volatile",
    "volatility": "volatile",
    "expansion": "volatile",
}

# Regimes in which a timeframe is considered genuinely directional — enough to
# override a stale global ranging read, or to confirm a London SELL.
DIRECTIONAL = ("trending", "volatile")


def normalise_regime(value: object) -> str:
    if value is None:
        return "unknown"
    key = str(value).strip().lower()
    return _ALIASES.get(key, key or "unknown")


def is_directional(value: object) -> bool:
    return normalise_regime(value) in DIRECTIONAL
