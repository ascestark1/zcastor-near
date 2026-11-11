"""
Classifying what actually happened.

Pure: a reference price, a direction, geometry, and a window of M1 bars in;
a verdict out. No broker, no clock, no files — so every case below is a test
rather than an argument.

This measures SIGNAL truth, not execution truth. Booking says what the account
did; this says whether the direction was right. They are different questions and
conflating them is how you end up unable to tell a bad signal from a bad fill.

Three conventions that decide the answer, all matching how the engine actually
trades:

**Spread is charged where it is really paid.** A BUY enters at the ask, so entry
is `ref + spread`. A SELL enters at the bid and exits at the ask, so the spread
lands on the exit. Ignoring this flatters every result by half a spread — on a
40pt spread against a 300pt target that is a 13% error, which is enough to make
a losing strategy look breakeven.

**Same-bar stop and target resolves to the stop.** M1 bars do not say which came
first, and assuming the good one turns a coin flip into free money on paper.

**A signal with no target gets one derived from the stop**, at the same R:R the
engine would have used, rather than being scored on close-to-close. Scoring a
stopped-out trade by where price ended is measuring a trade nobody held.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

_BUY = ("up", "buy", "long")


def is_buy(direction: str) -> bool:
    return str(direction).strip().lower() in _BUY


def horizon_minutes(policy: Mapping[str, Any], timeframe: str) -> float:
    cfg = dict(policy.get("resolver") or {})
    table = dict(cfg.get("horizon_min_by_tf") or {})
    return float(table.get(str(timeframe).strip().lower(),
                           cfg.get("horizon_min_default", 120)))


def classify(
    *,
    ref_price: float,
    direction: str,
    rates: Sequence[Mapping[str, Any]],
    sl_pts: float = 0.0,
    tp_pts: float = 0.0,
    spread_pts: float = 50.0,
    volume: float = 0.01,
    default_rr: float = 4.0 / 3.0,
) -> Optional[dict[str, Any]]:
    """
    Returns the verdict, or None when there are no bars to judge from.

    None means "cannot say yet" and the caller must retry. It must never be
    read as a loss: an unmeasurable signal recorded as wrong would poison the
    model-coherence figure with the resolver's own failures.
    """
    if not rates:
        return None

    buy = is_buy(direction)
    spread = float(spread_pts or 0.0)
    entry = float(ref_price) + (spread if buy else 0.0)

    sl = float(sl_pts or 0.0)
    tp = float(tp_pts or 0.0)
    if sl > 0 and tp <= 0:
        tp = round(sl * float(default_rr))

    outcome, net_pts = "timeout", None

    if sl > 0 and tp > 0:
        for bar in rates:
            high, low = float(bar["high"]), float(bar["low"])
            if buy:
                # Stop checked first: a bar touching both is scored as the stop.
                if low <= entry - sl:
                    outcome, net_pts = "sl", -sl
                    break
                if high >= entry + tp:
                    outcome, net_pts = "tp", tp
                    break
            else:
                if high + spread >= entry + sl:
                    outcome, net_pts = "sl", -sl
                    break
                if low + spread <= entry - tp:
                    outcome, net_pts = "tp", tp
                    break

    last_close = float(rates[-1]["close"])
    if net_pts is None:
        net_pts = (last_close - entry) if buy else (entry - last_close - spread)

    peak = max(float(b["high"]) for b in rates)
    trough = min(float(b["low"]) for b in rates)
    mfe = max(0.0, (peak - ref_price) if buy else (ref_price - trough))
    mae = max(0.0, (ref_price - trough) if buy else (peak - ref_price))
    close_pts = (last_close - ref_price) if buy else (ref_price - last_close)

    return {
        "outcome": outcome,
        "won": net_pts > 0,
        # Was the DIRECTION right, ignoring geometry? A stopped-out signal that
        # ended in the money called the move correctly and sized it wrongly,
        # and those are different lessons.
        "correct": close_pts > 0,
        "net_pts": round(net_pts, 1),
        "net_usd": round(net_pts * float(volume), 2),
        "close_pts": round(close_pts, 1),
        "mfe_pts": round(mfe, 1),
        "mae_pts": round(mae, 1),
        "r_achieved": round(mfe / sl, 4) if sl > 0 else 0.0,
        "bars": len(rates),
    }
