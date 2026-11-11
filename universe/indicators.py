"""
Volatility measures for ticker profiles. Pure functions over bar dicts.

Bars are {"t": epoch seconds, "open", "high", "low", "close"} — what
history.load returns. Resampling aligns to UTC boundaries, the same clock the
sessions and the dashboard blocks use.
"""

from __future__ import annotations

import statistics
from typing import Mapping, Optional, Sequence


def resample(bars: Sequence[Mapping], minutes: int) -> list[dict]:
    """Aggregate 1-minute bars into `minutes` bars on UTC boundaries."""
    if minutes <= 1:
        return [dict(b) for b in bars]
    span = minutes * 60
    out: list[dict] = []
    cur: Optional[dict] = None
    for b in bars:
        bucket = int(b["t"]) // span * span
        if cur is None or cur["t"] != bucket:
            if cur is not None:
                out.append(cur)
            cur = {"t": bucket, "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"], "n": 1}
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["n"] += 1
    if cur is not None:
        out.append(cur)
    return out


def true_ranges(bars: Sequence[Mapping]) -> list[float]:
    out = []
    prev_close = None
    for b in bars:
        hl = b["high"] - b["low"]
        if prev_close is None:
            out.append(hl)
        else:
            out.append(max(hl, abs(b["high"] - prev_close), abs(b["low"] - prev_close)))
        prev_close = b["close"]
    return out


def atr_series(bars: Sequence[Mapping], period: int = 14) -> list[float]:
    """Wilder's ATR. Empty until `period` bars exist."""
    tr = true_ranges(bars)
    if len(tr) < period:
        return []
    atr = sum(tr[:period]) / period
    out = [atr]
    for x in tr[period:]:
        atr = (atr * (period - 1) + x) / period
        out.append(atr)
    return out


def median_atr(bars: Sequence[Mapping], period: int = 14) -> Optional[float]:
    """
    Median of the ATR series over the window, not its last value. A profile is
    meant to describe the contract over a month, and the last value describes
    whatever the last hour happened to be.
    """
    s = atr_series(bars, period)
    return statistics.median(s) if s else None


def session_ranges(bars_1m: Sequence[Mapping],
                   starts_utc_min: Mapping[str, int]) -> dict[str, Optional[float]]:
    """
    Median high-low range of each session across the days in the window.

    Sessions are [start, next start) in UTC minutes, wrapping at midnight —
    the same table zcastor/market/session.py uses, so a profile's "London
    range" means exactly what the exhaustion gate means by it.
    """
    order = sorted(starts_utc_min.items(), key=lambda kv: kv[1])
    bounds = []
    for i, (name, start) in enumerate(order):
        end = order[(i + 1) % len(order)][1]
        bounds.append((name, start, end))

    def which(minute: int) -> str:
        for name, start, end in bounds:
            if start < end and start <= minute < end:
                return name
            if start > end and (minute >= start or minute < end):
                return name
        return order[0][0]

    per: dict[tuple[str, int], list[float]] = {}
    for b in bars_1m:
        t = int(b["t"])
        minute = (t % 86_400) // 60
        name = which(minute)
        day = t // 86_400
        # A session that wraps midnight belongs to the day it started on.
        start = dict(starts_utc_min)[name]
        if minute < start:
            day -= 1
        key = (name, day)
        hl = per.setdefault(key, [b["high"], b["low"]])
        hl[0] = max(hl[0], b["high"])
        hl[1] = min(hl[1], b["low"])

    ranges: dict[str, list[float]] = {name: [] for name, _ in order}
    for (name, _day), (hi, lo) in per.items():
        ranges[name].append(hi - lo)
    return {name: (statistics.median(v) if v else None) for name, v in ranges.items()}
