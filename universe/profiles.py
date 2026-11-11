"""
Ticker profiles — generated, never hand-built.

With dozens of perps, a profile written by hand per contract is a profile that
is wrong for most of them by next month. Each profile is computed from two
sources and nothing else:

  1. The venue's contract specification: tick size, lot step,
     minimum quantity and notional, precision.
  2. The contract's own recent history: ATR per timeframe, session ranges,
     funding, and a spread snapshot.

How BTC's settings carry over
-----------------------------
policy.json is calibrated on BTC in points (1 point = 1 USD on BTC). For
another contract, a volatility distance becomes

    value_in_price = value_in_btc_points × btc_point_value × scale[tf]
    scale[tf]      = ATR_tf(contract) / ATR_tf(BTC)

measured over the same window. BTC's scale is pinned to exactly 1.0 rather
than computed, so every BTC setting maps to itself and BTC decisions cannot
change because of this layer. That is the property the replay has to prove
once the gates read their distances through a profile.

Spread settings are costs, not volatility. They are not scaled by ATR; the
preview leaves them for the venue cost model and says so.

Each profile is written in canonical form with its checksum, and the index of
all profiles is one document — one record to anchor per refresh, so the
configuration every decision ran under is itself on the record (Proof of
Process, extended to configuration).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

from zcastor.market.session import SESSION_START_UTC_MIN
from zcastor.record.canonical import write_canonical

from . import history, indicators

GENERATOR = "universe/profiles.py 2"
TIMEFRAMES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
REFERENCE_TF = "1h"
REFERENCE_SYMBOL = "BTC"
COST_KEYS = ("spread",)     # any setting whose name contains these is a cost


# ── contract specification ────────────────────────────────────────────────────

def spec(info: Mapping[str, Any]) -> dict[str, Any]:
    filters = {f.get("filterType"): f for f in info.get("filters", [])}
    price = filters.get("PRICE_FILTER", {})
    lot = filters.get("LOT_SIZE", {})
    market = filters.get("MARKET_LOT_SIZE", {})
    notional = filters.get("MIN_NOTIONAL", {})
    # Kept as the strings the venue sent. "0.10" is a tick size; 0.1 is a float
    # that happens to be near one.
    return {
        "tick_size": price.get("tickSize"),
        "min_price": price.get("minPrice"),
        "step_size": lot.get("stepSize"),
        "min_qty": lot.get("minQty"),
        "max_qty": lot.get("maxQty"),
        "market_step_size": market.get("stepSize"),
        "market_max_qty": market.get("maxQty"),
        "min_notional": notional.get("notional"),
        "price_precision": info.get("pricePrecision"),
        "quantity_precision": info.get("quantityPrecision"),
        "underlying_type": info.get("underlyingType"),
        "listed_at": _day(info.get("onboardDate")),
    }


# ── volatility ────────────────────────────────────────────────────────────────

def volatility(bars_1m: list[dict], bar_minutes: int = 1) -> dict[str, Any]:
    """`bars_1m` may hold bars of any size; `bar_minutes` says which."""
    closes = [b["close"] for b in bars_1m]
    median_close = statistics.median(closes) if closes else None
    atr: dict[str, Optional[float]] = {}
    atr_pct: dict[str, Optional[float]] = {}
    for tf, minutes in TIMEFRAMES.items():
        if minutes < bar_minutes:
            atr[tf] = atr_pct[tf] = None
            continue
        v = indicators.median_atr(indicators.resample(bars_1m, minutes))
        atr[tf] = v
        atr_pct[tf] = (v / median_close * 100) if (v and median_close) else None
    ranges_1m = [b["high"] - b["low"] for b in bars_1m]
    return {
        "median_close": median_close,
        "bar_minutes": bar_minutes,
        "median_bar_range": statistics.median(ranges_1m) if ranges_1m else None,
        "atr14_median": atr,
        "atr14_median_pct": atr_pct,
        "session_range_median": indicators.session_ranges(bars_1m, SESSION_START_UTC_MIN),
    }


def btc_scale(vol: Mapping[str, Any], btc_vol: Mapping[str, Any],
              is_reference: bool) -> dict[str, Optional[float]]:
    if is_reference:
        return {tf: 1.0 for tf in TIMEFRAMES}
    out: dict[str, Optional[float]] = {}
    for tf in TIMEFRAMES:
        a = vol["atr14_median"].get(tf)
        b = btc_vol["atr14_median"].get(tf)
        out[tf] = (a / b) if (a and b) else None
    return out


# ── what BTC's policy would become here ───────────────────────────────────────

def round_to_tick(value: float, tick: Optional[str]) -> tuple[float, Optional[int]]:
    if not tick or Decimal(tick) == 0:
        return value, None
    t = Decimal(tick)
    ticks = (Decimal(repr(value)) / t).to_integral_value(rounding=ROUND_HALF_UP)
    return float((ticks * t).normalize()), int(ticks)


def _walk(o: Any, path: tuple = ()):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, path + (k,))
    else:
        yield path, o


def policy_preview(policy: Mapping[str, Any], scale: Mapping[str, Optional[float]],
                   tick: Optional[str], point_value: float = 1.0) -> dict[str, Any]:
    """
    Every point-denominated policy setting, translated. A preview only: the
    gates still read raw points until the ATR switch lands. Reviewing this
    table per contract is how the switch gets checked by eye before it is
    checked by replay.
    """
    out: dict[str, Any] = {}
    for path, value in _walk(policy):
        if not any("pts" in str(p) for p in path):
            continue
        key = ".".join(str(p) for p in path)
        if any(c in key for c in COST_KEYS):
            out[key] = {"btc_points": value, "basis": "cost",
                        "note": "set from the venue's spread and fees, not ATR"}
            continue
        tf = str(path[-1]) if str(path[-1]) in TIMEFRAMES else REFERENCE_TF
        s = scale.get(tf)
        if s is None:
            out[key] = {"btc_points": value, "basis": f"atr_{tf}", "price": None}
            continue
        values = value if isinstance(value, list) else [value]
        converted = [round_to_tick(float(v) * point_value * s, tick) for v in values]
        entry = {"btc_points": value, "basis": f"atr_{tf}",
                 "price": [c[0] for c in converted] if isinstance(value, list) else converted[0][0],
                 "ticks": [c[1] for c in converted] if isinstance(value, list) else converted[0][1]}
        out[key] = entry
    return out


# ── the profile ───────────────────────────────────────────────────────────────

def build_profile(symbol: str, info: Mapping[str, Any], bars_1m: list[dict], *,
                  btc_vol: Mapping[str, Any], policy: Mapping[str, Any],
                  as_of: datetime, point_value: float = 1.0,
                  book: Optional[Mapping[str, Any]] = None,
                  funding: Optional[Mapping[str, Any]] = None,
                  bar_minutes: int = 1, venue: str = "hyperliquid") -> dict[str, Any]:
    sp = spec(info)
    vol = volatility(bars_1m, bar_minutes)
    is_ref = symbol == REFERENCE_SYMBOL
    scale = btc_scale(vol, btc_vol, is_ref)
    return {
        "schema": "zcastor/ticker-profile",
        "schema_version": "1",
        "symbol": symbol,
        "venue": venue,
        "generated_at": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": GENERATOR,
        "stage": "reference" if is_ref else "profiled",
        "window": _window(bars_1m, bar_minutes),
        "spec": sp,
        "volatility": _finite(vol),
        "spread": _spread(book, sp.get("tick_size")),
        "funding": _finite(funding or {}),
        "btc_scale": {"reference_tf": REFERENCE_TF, "pinned": is_ref,
                      "point_value_usd": point_value, "by_tf": _finite(scale)},
        "policy_version": policy.get("policy_version"),
        "policy_preview": _finite(policy_preview(policy, scale, sp.get("tick_size"),
                                                 point_value)),
    }


def generate(client, universe: Mapping[str, Any], *, history_root: Path,
             out_root: Path, policy: Mapping[str, Any], days: int = 30,
             point_value: float = 1.0, with_funding: bool = True,
             now: Optional[datetime] = None, log=print, interval: str = "1m",
             venue: str = "hyperliquid") -> dict[str, Any]:
    bar_minutes = history.INTERVAL_MINUTES[interval]
    now = now or datetime.now(timezone.utc)
    symbols = [m["symbol"] for m in universe.get("members", [])]
    if REFERENCE_SYMBOL not in symbols:
        raise ValueError(f"{REFERENCE_SYMBOL} must be in the universe: every "
                         "other profile is scaled against it")
    infos = {s["symbol"]: s for s in client.exchange_info().get("symbols", [])}
    books = {b["symbol"]: b for b in client.book_tickers()}

    btc_bars = history.load(history_root, REFERENCE_SYMBOL, interval, days)
    if not btc_bars:
        raise ValueError(f"no {REFERENCE_SYMBOL} history under {history_root} — "
                         "run `python -m universe history` first")
    btc_vol = volatility(btc_bars, bar_minutes)

    entries, missing = [], []
    start_ms = int((now.timestamp() - days * 86_400) * 1000)
    end_ms = int(now.timestamp() * 1000)
    for sym in symbols:
        bars = history.load(history_root, sym, interval, days)
        if not bars or sym not in infos:
            missing.append(sym)
            log(f"  {sym:<14} skipped (no history or no spec)")
            continue
        funding = None
        if with_funding:
            rates = [float(r["fundingRate"]) for r in
                     client.funding_history(sym, start_ms, end_ms) if "fundingRate" in r]
            if rates:
                funding = {"settlements": len(rates),
                           "interval_hours": 1 if venue == "hyperliquid" else 8, "mean": statistics.fmean(rates),
                           "last": rates[-1], "max_abs": max(abs(r) for r in rates)}
        doc = build_profile(sym, infos[sym], bars, btc_vol=btc_vol, policy=policy,
                            as_of=now, point_value=point_value,
                            book=books.get(sym), funding=funding,
                            bar_minutes=bar_minutes, venue=venue)
        path = out_root / f"{sym}.json"
        digest = write_canonical(path, doc)
        entries.append({"symbol": sym, "checksum": digest, "path": f"{sym}.json"})
        s = doc["btc_scale"]["by_tf"].get(REFERENCE_TF)
        log(f"  {sym:<14} scale@{REFERENCE_TF} {s if s is not None else '—':<8}"
            f"  {digest[:19]}")

    index = {
        "schema": "zcastor/ticker-profiles",
        "schema_version": "1",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": GENERATOR,
        "universe_as_of": universe.get("as_of"),
        "policy_version": policy.get("policy_version"),
        "days": days,
        "profiles": entries,
        "missing": missing,
    }
    index["checksum"] = write_canonical(out_root / "index.json", index)
    return index


# ── helpers ───────────────────────────────────────────────────────────────────

def _spread(book: Optional[Mapping[str, Any]], tick: Optional[str]) -> dict[str, Any]:
    if not book:
        return {"snapshot": None}
    bid, ask = Decimal(book["bidPrice"]), Decimal(book["askPrice"])
    mid = (bid + ask) / 2
    out = {"snapshot": "single book-top read at generation time; not a median",
           "bid": book["bidPrice"], "ask": book["askPrice"],
           "bps": float((ask - bid) / mid * 10_000) if mid else None}
    if tick and Decimal(tick) > 0:
        out["ticks"] = int((ask - bid) / Decimal(tick))
    return out


def _window(bars: list[dict], bar_minutes: int = 1) -> dict[str, Any]:
    if not bars:
        return {"bars": 0}
    first, last = bars[0]["t"], bars[-1]["t"]
    expected = int(round((last - first) / 60 / bar_minutes)) + 1
    return {"from": _iso(first), "to": _iso(last), "bars": len(bars),
            "bar_minutes": bar_minutes,
            "missing_bars": max(expected - len(bars), 0)}


def _finite(o: Any) -> Any:
    """Canonical JSON refuses NaN and infinity; a profile must say 'unknown'."""
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_finite(v) for v in o]
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(ms) -> Optional[str]:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d") if ms else None
