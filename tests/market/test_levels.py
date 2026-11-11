"""Hourly swing level tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.market.levels import HourlyLevels  # noqa: E402


class Clock:
    def __init__(self, t=100_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def bars(shape):
    """One M1 bar per minute, hourly highs/lows following `shape`."""
    out, ts = [], 0
    for hi, lo in shape:
        for _ in range(60):
            out.append({"time": ts, "high": hi, "low": lo,
                        "open": lo, "close": hi})
            ts += 60
    return out


PEAK = [(100, 90), (110, 95), (120, 100), (130, 105),
        (150, 120),                              # swing high
        (130, 105), (120, 100), (110, 95), (100, 90)]

# A trough needs the LOWS to dip in the middle. Reversing PEAK does not give
# one: its lows peak in the centre too, so the same series is a high and no low.
TROUGH = [(150, 120), (140, 115), (130, 110), (120, 105),
          (110, 80),                             # swing low
          (120, 105), (130, 110), (140, 115), (150, 120)]


def test_a_local_high_is_found():
    lv = HourlyLevels(lambda f, t: bars(PEAK), k=3, clock=Clock())
    assert lv.nearest_resistance(120.0) == 150.0


def test_a_local_low_is_found():
    lv = HourlyLevels(lambda f, t: bars(TROUGH), k=3, clock=Clock())
    assert lv.nearest_support(200.0) == 80.0


def test_nothing_below_price_returns_none():
    lv = HourlyLevels(lambda f, t: bars(TROUGH), k=3, clock=Clock())
    assert lv.nearest_support(1.0) is None


def test_nothing_above_price_returns_none():
    lv = HourlyLevels(lambda f, t: bars(PEAK), k=3, clock=Clock())
    assert lv.nearest_resistance(10_000.0) is None


def test_m1_bars_are_aggregated_into_hours():
    lv = HourlyLevels(lambda f, t: bars(PEAK), k=3, clock=Clock())
    lv.refresh(force=True)
    assert lv.stats()["resistance"] >= 1


def test_levels_are_cached_between_refreshes():
    calls = []

    def rates(f, t):
        calls.append(1)
        return bars(PEAK)

    clock = Clock()
    lv = HourlyLevels(rates, k=3, refresh_seconds=900, clock=clock)
    lv.nearest_resistance(120.0)
    lv.nearest_resistance(120.0)
    assert len(calls) == 1
    clock.advance(901)
    lv.nearest_resistance(120.0)
    assert len(calls) == 2


def test_a_failing_feed_leaves_the_last_levels_in_place():
    """A broker hiccup must not erase levels the watcher is arming against."""
    state = {"fail": False}

    def rates(f, t):
        if state["fail"]:
            raise ConnectionError("bridge down")
        return bars(PEAK)

    clock = Clock()
    lv = HourlyLevels(rates, k=3, refresh_seconds=1, clock=clock)
    assert lv.nearest_resistance(120.0) == 150.0
    state["fail"] = True
    clock.advance(10)
    assert lv.nearest_resistance(120.0) == 150.0


def test_too_few_bars_yields_no_levels():
    lv = HourlyLevels(lambda f, t: bars(PEAK[:2]), k=3, clock=Clock())
    assert lv.nearest_resistance(0.0) is None


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
