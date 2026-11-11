"""
Hyperliquid public market data — the desk's only market data source.

Everything here is public and keyless: `POST /info` needs no account, so this
module can never place an order or read a balance. Order placement lives in its
own module and signs with a trade-only agent key.

Two layers:

- `HLInfo`: the raw `/info` API with a weight budget. Hyperliquid limits REST
  traffic by WEIGHT per IP per minute (1200 at the time of writing). Most info
  requests weigh 20, `l2Book` and `allMids` weigh 2, and `candleSnapshot` adds
  one unit per 60 candles returned. The client counts what it spends and sleeps
  into the next minute before it reaches its budget.
- `BinanceView`: the same data in the shapes the rest of the desk was written
  against (contract list, 24h tickers, premium index, open interest, klines),
  so the universe tools, the profile generator and the dashboard keep working
  with Hyperliquid underneath and without a Binance call anywhere.

Symbols are Hyperliquid coin names, exactly as Hyperliquid spells them: `BTC`,
`ETH`, `kPEPE` (Hyperliquid's 1000× contract). Margin is USDC.

Two semantic differences worth knowing, both handled here rather than hidden:

- **Funding is hourly on Hyperliquid, 8-hourly on Binance.** The dashboard's
  funding thresholds were set on 8-hour rates, so `premium_index` reports the
  hourly rate × 8 as `lastFundingRate` and the raw hourly rate as
  `hourlyFundingRate`.
- **Candle history is short.** Hyperliquid serves only the most recent
  candles per interval (about 5,000), so 1-minute history reaches back a few
  days. Profiles are built from 5-minute bars instead.

The transport is injectable so the tests run with no network.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("zcastor.market.hyperliquid")

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"


def api_base(testnet: bool = False) -> str:
    """HYPERLIQUID_API_URL overrides (a local stand-in for tests); else main/test net."""
    import os
    return os.environ.get("HYPERLIQUID_API_URL") or (TESTNET if testnet else MAINNET)

# Hyperliquid allows 1200/min per IP. The hub, the universe tools and the
# engine may share one IP, so each client keeps well under it.
DEFAULT_WEIGHT_BUDGET = 800

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
    "3d": 259_200_000, "1w": 604_800_000, "1M": 2_592_000_000,
}
# Hyperliquid keeps roughly this many recent candles per interval.
MAX_CANDLES = 5000
FUNDING_TO_8H = 8

LIGHT = {"l2Book", "allMids", "clearinghouseState", "orderStatus",
         "spotClearinghouseState", "exchangeStatus"}

Transport = Callable[[Mapping[str, Any]], Any]


class HLHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


def urllib_transport(base: str = MAINNET, timeout: float = 20.0) -> Transport:
    def call(body: Mapping[str, Any]) -> Any:
        req = urllib.request.Request(
            f"{base}/info", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "zcastor-near"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace") if exc.fp else ""
            raise HLHTTPError(exc.code, text) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise HLHTTPError(0, f"network: {getattr(exc, 'reason', exc)}") from None
    return call


def request_weight(body: Mapping[str, Any], result: Any = None) -> int:
    kind = str(body.get("type", ""))
    if kind in LIGHT:
        return 2
    weight = 20
    if kind == "candleSnapshot" and isinstance(result, list):
        weight += len(result) // 60
    return weight


class HLInfo:
    def __init__(self, transport: Optional[Transport] = None, *,
                 weight_budget: int = DEFAULT_WEIGHT_BUDGET,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 max_retries: int = 5) -> None:
        self._transport = transport or urllib_transport()
        self._budget = weight_budget
        self._sleep = sleep
        self._clock = clock
        self._retries = max_retries
        self._minute = int(clock() // 60)
        self.used_weight = 0
        self.requests = 0

    def _throttle(self, upcoming: int) -> None:
        minute = int(self._clock() // 60)
        if minute != self._minute:
            self._minute, self.used_weight = minute, 0
        if self.used_weight + upcoming > self._budget:
            wait = 61 - (self._clock() % 60)
            logger.info("weight %d near budget %d; sleeping %.0fs",
                        self.used_weight, self._budget, wait)
            self._sleep(wait)
            self._minute, self.used_weight = int(self._clock() // 60), 0

    def post(self, body: Mapping[str, Any]) -> Any:
        delay = 1.0
        for attempt in range(self._retries + 1):
            self._throttle(request_weight(body))
            try:
                result = self._transport(body)
            except HLHTTPError as exc:
                if exc.status in (0, 429) or exc.status >= 500:
                    if attempt == self._retries:
                        if exc.status == 0:
                            raise RuntimeError(
                                f"cannot reach Hyperliquid ({exc}). Check the "
                                "connection and run the same command again.") from None
                        raise
                    if exc.status == 429:
                        self.used_weight = self._budget
                    logger.warning("%s %s; retrying in %.0fs",
                                   exc.status, body.get("type"), delay)
                    self._sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise
            self.requests += 1
            self.used_weight += request_weight(body, result)
            return result
        raise RuntimeError("unreachable")

    # ── endpoints ─────────────────────────────────────────────────────────────

    def meta_and_ctxs(self) -> tuple[list[dict], list[dict]]:
        """Contract specs and live context for every perp, index-aligned."""
        meta, ctxs = self.post({"type": "metaAndAssetCtxs"})
        return list(meta.get("universe", [])), list(ctxs)

    def candles(self, coin: str, interval: str, start_ms: int,
                end_ms: int) -> list[dict]:
        return self.post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": int(start_ms), "endTime": int(end_ms)}}) or []

    def l2_book(self, coin: str) -> dict:
        return self.post({"type": "l2Book", "coin": coin}) or {}

    def funding_history(self, coin: str, start_ms: int,
                        end_ms: Optional[int] = None) -> list[dict]:
        body: dict[str, Any] = {"type": "fundingHistory", "coin": coin,
                                "startTime": int(start_ms)}
        if end_ms is not None:
            body["endTime"] = int(end_ms)
        return self.post(body) or []


# ── price rules ──────────────────────────────────────────────────────────────

def tick_size(price: float, sz_decimals: int) -> str:
    """
    The smallest valid price step at `price`. Hyperliquid perp prices may have
    at most 5 significant figures and at most (6 - szDecimals) decimals, and an
    integer price is always valid. So the tick depends on the price level.
    """
    if price <= 0:
        return "0"
    sig = 10.0 ** (math.floor(math.log10(price)) - 4)
    dec = 10.0 ** -(6 - int(sz_decimals))
    tick = min(1.0, max(sig, dec))
    return format(Decimal(repr(tick)).normalize(), "f")


def kline_row(c: Mapping[str, Any]) -> list:
    """A Hyperliquid candle as a Binance-style kline array."""
    close, vol = str(c["c"]), str(c["v"])
    quote = float(close) * float(vol)
    return [int(c["t"]), str(c["o"]), str(c["h"]), str(c["l"]), close, vol,
            int(c["T"]), repr(quote), int(c.get("n", 0)), "0", "0", "0"]


# ── the view the rest of the desk reads ──────────────────────────────────────

class BinanceView:
    """
    Hyperliquid data in the shapes the universe tools and the dashboard read.

    Context data (prices, volume, funding, open interest) comes from one
    `metaAndAssetCtxs` call and is cached for `ctx_ttl` seconds, so asking for
    thirty symbols costs one request.
    """

    def __init__(self, info: HLInfo, *, ctx_ttl: float = 3.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.info = info
        self._ttl = ctx_ttl
        self._clock = clock
        self._ctx_at = -1e18
        self._specs: dict[str, dict] = {}
        self._ctxs: dict[str, dict] = {}

    @property
    def used_weight(self) -> int:
        return self.info.used_weight

    @property
    def requests(self) -> int:
        return self.info.requests

    def refresh(self, force: bool = False) -> None:
        if not force and self._clock() - self._ctx_at < self._ttl:
            return
        specs, ctxs = self.info.meta_and_ctxs()
        self._specs = {s["name"]: s for s in specs}
        self._ctxs = {s["name"]: c for s, c in zip(specs, ctxs)}
        self._ctx_at = self._clock()

    def coins(self) -> list[str]:
        self.refresh()
        return list(self._specs)

    def ctx(self, coin: str) -> dict:
        self.refresh()
        return self._ctxs.get(coin, {})

    def spec(self, coin: str) -> dict:
        self.refresh()
        return self._specs.get(coin, {})

    # ── Binance shapes ──

    def exchange_info(self) -> dict:
        self.refresh()
        out = []
        for name, s in self._specs.items():
            ctx = self._ctxs.get(name, {})
            px = float(ctx.get("markPx") or 0)
            szd = int(s.get("szDecimals", 0))
            tick = tick_size(px, szd) if px else None
            step = format(Decimal(1).scaleb(-szd), "f")
            out.append({
                "symbol": name,
                "status": "BREAK" if s.get("isDelisted") else "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDC", "marginAsset": "USDC",
                "underlyingType": "COIN",
                "onboardDate": None,           # Hyperliquid does not publish it
                "pricePrecision": max(0, 6 - szd),
                "quantityPrecision": szd,
                "maxLeverage": s.get("maxLeverage"),
                "onlyIsolated": bool(s.get("onlyIsolated", False)),
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": tick,
                     "minPrice": tick},
                    {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step,
                     "maxQty": None},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": step,
                     "maxQty": None},
                    {"filterType": "MIN_NOTIONAL", "notional": "10"},
                ],
            })
        return {"symbols": out}

    def ticker_24h(self, coin: str) -> dict:
        c = self.ctx(coin)
        last = c.get("midPx") or c.get("markPx")
        return {"symbol": coin, "lastPrice": str(last or "0"),
                "openPrice": str(c.get("prevDayPx") or last or "0"),
                "quoteVolume": str(c.get("dayNtlVlm") or "0"),
                "volume": str(c.get("dayBaseVlm") or "0")}

    def tickers_24h(self) -> list:
        return [self.ticker_24h(coin) for coin in self.coins()]

    def premium(self, coin: str) -> dict:
        c = self.ctx(coin)
        hourly = float(c.get("funding") or 0)
        return {"symbol": coin, "markPrice": str(c.get("markPx") or "0"),
                "indexPrice": str(c.get("oraclePx") or "0"),
                "lastFundingRate": repr(hourly * FUNDING_TO_8H),
                "hourlyFundingRate": repr(hourly),
                "fundingIntervalHours": 1}

    def premium_index(self) -> list:
        return [self.premium(coin) for coin in self.coins()]

    def open_interest(self, coin: str) -> dict:
        return {"symbol": coin,
                "openInterest": str(self.ctx(coin).get("openInterest") or "0")}

    def book_ticker(self, coin: str) -> dict:
        book = self.info.l2_book(coin)
        levels = book.get("levels") or [[], []]
        bid = levels[0][0]["px"] if levels and levels[0] else "0"
        ask = levels[1][0]["px"] if len(levels) > 1 and levels[1] else "0"
        return {"symbol": coin, "bidPrice": str(bid), "askPrice": str(ask)}

    def book_tickers(self, coins: Optional[list[str]] = None) -> list:
        return [self.book_ticker(c) for c in (coins or self.coins())]

    def funding_history(self, coin: str, start_ms: int, end_ms: int) -> list:
        """Hourly settlements, in Binance's field names. Rates are HOURLY."""
        out, cursor = [], int(start_ms)
        while cursor < end_ms:
            batch = self.info.funding_history(coin, cursor, end_ms)
            if not batch:
                break
            out += [{"symbol": coin, "fundingRate": r["fundingRate"],
                     "fundingTime": int(r["time"])} for r in batch]
            last = int(batch[-1]["time"])
            if last <= cursor:
                break
            cursor = last + 1
        return out

    def klines(self, coin: str, interval: str, start_ms: Optional[int] = None,
               end_ms: Optional[int] = None, limit: int = 500) -> list:
        step = INTERVAL_MS[interval]
        end = int(end_ms if end_ms is not None else self._clock() * 1000)
        start = int(start_ms if start_ms is not None
                    else end - step * max(1, min(limit, MAX_CANDLES)))
        rows = [kline_row(c) for c in self.info.candles(coin, interval, start, end)]
        rows = [r for r in rows if start <= r[0] <= end]
        rows.sort(key=lambda r: r[0])
        if start_ms is not None:
            return rows[:limit]
        return rows[-limit:]
