"""
A fake Hyperliquid /info endpoint for tests. Deterministic: candles are a
seeded random walk per coin, aligned to interval boundaries, and only the most
recent MAX_CANDLES per interval exist, as on the real API.
"""

import math
import random

from zcastor.market.hyperliquid import INTERVAL_MS, MAX_CANDLES

COINS = [
    # name, szDecimals, maxLeverage, price, day notional volume, OI (coins)
    ("BTC", 5, 40, 110_000.0, 2.5e9, 30_000.0),
    ("ETH", 4, 25, 2_400.0, 1.2e9, 500_000.0),
    ("SOL", 2, 20, 180.0, 4.0e8, 2_000_000.0),
    ("kPEPE", 0, 10, 0.0105, 6.0e7, 4.0e9),
    ("TINY", 1, 3, 1.5, 2.0e5, 10_000.0),
]
DELISTED = ("OLD", 2, 3, 5.0, 0.0, 0.0)


class FakeHL:
    def __init__(self, now_ms=1_761_300_000_000, fail=None):
        self.now_ms = now_ms
        self.calls = []
        self.fail = fail or {}

    def _spec_ctx(self):
        specs, ctxs = [], []
        for name, szd, lev, px, vol, oi in COINS + [DELISTED]:
            spec = {"name": name, "szDecimals": szd, "maxLeverage": lev}
            if name == "OLD":
                spec["isDelisted"] = True
            specs.append(spec)
            ctxs.append({"funding": "0.0000125", "openInterest": str(oi),
                         "prevDayPx": repr(px * 0.98), "dayNtlVlm": repr(vol),
                         "premium": "0.0001", "oraclePx": repr(px * 0.9999),
                         "markPx": repr(px), "midPx": repr(px * 1.00001),
                         "dayBaseVlm": repr(vol / px)})
        return specs, ctxs

    def _price(self, coin):
        for c in COINS + [DELISTED]:
            if c[0] == coin:
                return c[3]
        raise KeyError(coin)

    def _candles(self, coin, interval, start, end):
        step = INTERVAL_MS[interval]
        newest = self.now_ms // step * step
        oldest = newest - step * (MAX_CANDLES - 1)
        t = max(start // step * step, oldest)
        if t < start:
            t += step
        out = []
        base = self._price(coin)
        while t <= min(end, newest):
            rnd = random.Random(f"{coin}{interval}{t}")
            drift = math.sin(t / step / 50.0) * 0.01
            o = base * (1 + drift)
            c = o * (1 + rnd.uniform(-0.002, 0.002))
            h = max(o, c) * (1 + rnd.uniform(0, 0.002))
            low = min(o, c) * (1 - rnd.uniform(0, 0.002))
            out.append({"t": t, "T": t + step - 1, "s": coin, "i": interval,
                        "o": f"{o:.6g}", "c": f"{c:.6g}", "h": f"{h:.6g}",
                        "l": f"{low:.6g}", "v": f"{rnd.uniform(1, 100):.4f}",
                        "n": rnd.randint(10, 999)})
            t += step
        return out

    def __call__(self, body):
        self.calls.append(body)
        kind = body["type"]
        if kind in self.fail:
            raise self.fail[kind]
        if kind == "metaAndAssetCtxs":
            specs, ctxs = self._spec_ctx()
            return [{"universe": specs}, ctxs]
        if kind == "candleSnapshot":
            r = body["req"]
            return self._candles(r["coin"], r["interval"], r["startTime"], r["endTime"])
        if kind == "l2Book":
            px = self._price(body["coin"])
            return {"coin": body["coin"], "time": self.now_ms, "levels": [
                [{"px": f"{px * 0.99995:.6g}", "sz": "1", "n": 1}],
                [{"px": f"{px * 1.00005:.6g}", "sz": "1", "n": 1}]]}
        if kind == "fundingHistory":
            start, end = body["startTime"], body.get("endTime", self.now_ms)
            out, t = [], (start // 3_600_000 + 1) * 3_600_000
            while t <= end and len(out) < 500:
                out.append({"coin": body["coin"], "fundingRate": "0.0000125",
                            "premium": "0.0001", "time": t})
                t += 3_600_000
            return out
        raise ValueError(f"fake HL: unknown type {kind}")
