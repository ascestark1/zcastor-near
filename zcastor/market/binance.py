"""
Binance market data — the same interface the MT5 port implements.

Nothing above this file changes. The gate stack, the record layer, the anchoring
and the resolver all sit behind `MarketData`, so a second venue is a new
implementation rather than a new system. That was the point of making it a port
in the first place, and this is the first time it has been tested.

Two things differ from MT5 and both matter more than they look.

**Spread.** XM quotes BTC at roughly 40 dollars wide. Binance quotes it under a
dollar. Every conclusion in our August analysis was driven by that 40-dollar
tax: a 5m trade there needs a 69% win rate to break even, because half the stop
distance goes to the broker before price moves. The same geometry on Binance
needs closer to 52%. The strategy did not change; the venue did.

**Positions.** Spot has no position concept — a buy is an asset balance, not an
open trade with a stop attached. So position state is reconstructed from our own
journal rather than queried, and stops are enforced by the watcher's polling
loop rather than resting at the exchange. That is a real difference in failure
mode: if this process dies with a position open, nothing at the venue closes it.
Stated plainly because it is the sort of thing that is discovered at the worst
possible moment.

Credentials come from BINANCE_API_KEY and BINANCE_API_SECRET. Never a file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

logger = logging.getLogger("zcastor.market.binance")

MAINNET = "https://api.binance.com"
TESTNET = "https://testnet.binance.vision"

TIMEOUT = 10


class BinanceError(RuntimeError):
    """The venue refused or could not be reached."""


class BinanceClient:
    """Thin signed-REST client. No SDK, so there is nothing to drift."""

    def __init__(self, *, api_key: str = "", api_secret: str = "",
                 base_url: str = TESTNET) -> None:
        self.api_key = api_key or os.environ.get("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.environ.get("BINANCE_API_SECRET", "")
        self.base_url = base_url.rstrip("/")

    # ── transport ─────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(),
                             hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    def request(self, method: str, path: str, params: Optional[dict] = None,
                signed: bool = False) -> Any:
        params = dict(params or {})
        if signed:
            if not (self.api_key and self.api_secret):
                raise BinanceError("BINANCE_API_KEY and BINANCE_API_SECRET "
                                   "must be set for signed requests")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = self._sign(params)
        else:
            query = urllib.parse.urlencode(params)

        url = f"{self.base_url}{path}"
        headers = {"User-Agent": "Zcastor/2.0"}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        if method == "GET":
            request = urllib.request.Request(f"{url}?{query}", headers=headers)
        else:
            request = urllib.request.Request(
                url, data=query.encode(), headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            raise BinanceError(f"HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            raise BinanceError(str(exc)) from exc


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class BinanceMarketData:
    """Read-only view of Binance. Same surface as the MT5 port."""

    def __init__(self, client: BinanceClient, *, symbol: str = "BTCUSDT",
                 volume: float = 0.001, quote: str = "USDT",
                 journal: Any = None, session_opens: Any = None) -> None:
        self._c = client
        self.symbol = symbol
        self.volume = volume
        self.quote = quote
        self._journal = journal
        self._sessions = session_opens
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}

    # ── decision lifecycle ────────────────────────────────────────────────────

    def begin(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cached(self, key: str, producer: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = producer()
        with self._lock:
            self._cache[key] = value
        return value

    # ── prices ────────────────────────────────────────────────────────────────

    def get_tick(self) -> dict:
        return self._cached("tick", self._fetch_tick)

    def _fetch_tick(self) -> dict:
        try:
            book = self._c.request("GET", "/api/v3/ticker/bookTicker",
                                   {"symbol": self.symbol})
        except BinanceError as exc:
            logger.warning("get_tick failed: %s", exc)
            return {"error": str(exc)}

        ask, bid = _f(book.get("askPrice")), _f(book.get("bidPrice"))
        if ask <= 0 or bid <= 0:
            return {"error": "book has no usable ask/bid"}

        return {
            "price": (ask + bid) / 2,
            "ask": ask,
            "bid": bid,
            # Points are whole quote units, matching the MT5 convention, so
            # every threshold in policy means the same thing on both venues.
            "spread_pts": round(ask - bid, 2),
            # Binance gives no book timestamp, so the stale-feed gate compares
            # the mid instead: a frozen book has an unchanging mid.
            "time": round(ask + bid, 4),
        }

    def get_rates_range(self, from_ts: float, to_ts: float) -> list:
        """M1 klines, shaped like the MT5 port's bars."""
        try:
            rows = self._c.request("GET", "/api/v3/klines", {
                "symbol": self.symbol, "interval": "1m",
                "startTime": int(from_ts * 1000), "endTime": int(to_ts * 1000),
                "limit": 1000,
            })
        except BinanceError as exc:
            logger.debug("get_rates_range failed: %s", exc)
            return []
        return [{"time": r[0] / 1000, "open": _f(r[1]), "high": _f(r[2]),
                 "low": _f(r[3]), "close": _f(r[4])} for r in rows]

    def session_open_price(self, session: str) -> float:
        if self._sessions is None:
            return 0.0
        return self._sessions.open_price(session)

    # ── account ───────────────────────────────────────────────────────────────

    def _account(self) -> Optional[dict]:
        return self._cached("account", self._fetch_account)

    def _fetch_account(self) -> Optional[dict]:
        try:
            return self._c.request("GET", "/api/v3/account", signed=True)
        except BinanceError as exc:
            logger.warning("account fetch failed: %s", exc)
            return None

    def get_balance(self) -> Optional[float]:
        """
        Free quote balance. None means unreadable, NOT zero — the dead-man gate
        has to be able to tell a broken connection from an empty account.
        """
        account = self._account()
        if account is None:
            return None
        for bal in account.get("balances", []):
            if bal.get("asset") == self.quote:
                return _f(bal.get("free")) + _f(bal.get("locked"))
        return 0.0

    def get_free_margin(self) -> Optional[float]:
        """Spot has no margin; free quote balance is the equivalent constraint."""
        account = self._account()
        if account is None:
            return None
        for bal in account.get("balances", []):
            if bal.get("asset") == self.quote:
                return _f(bal.get("free"))
        return 0.0

    def get_equity(self) -> Optional[float]:
        return self.get_balance()

    def get_margin_for(self, direction: str, volume: float,
                       price: float) -> float:
        """Spot buys cost the full notional; there is no leverage to divide by."""
        return float(volume) * float(price)

    # ── positions ─────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list:
        """
        Spot has no positions, so open trades come from our own journal.

        This is the one place the two venues genuinely differ. On MT5 the broker
        is the source of truth and the close poller diffs against it. Here the
        journal IS the source of truth, which means a position closed manually
        at the exchange would not be noticed. Acceptable for a single-operator
        system; worth knowing before anyone else touches the account.
        """
        if self._journal is None:
            return []
        out = []
        for ticket in self._journal.open_tickets():
            trade = self._journal.get(ticket) or {}
            out.append({
                "ticket": int(ticket),
                "side": "BUY" if str(trade.get("direction", "")).upper() == "UP"
                        else "SELL",
                "volume": _f(trade.get("volume")),
                "open_price": _f(trade.get("entry_price")),
                "sl": _f(trade.get("sl")), "tp": _f(trade.get("tp")),
                "profit": 0.0,
            })
        return out

    def get_closed_deal(self, ticket: int) -> Optional[dict]:
        """
        Realised result from the venue's own fill history.

        Sums across every fill on the order, net of commission, converting
        commission to quote currency when it was charged in the base asset.
        None means not closed yet and the caller must retry — never a zero.
        """
        try:
            fills = self._c.request("GET", "/api/v3/myTrades", {
                "symbol": self.symbol, "orderId": int(ticket)}, signed=True)
        except BinanceError as exc:
            logger.warning("fill history failed for %s: %s", ticket, exc)
            return None

        if not fills:
            return None

        qty = sum(_f(f["qty"]) for f in fills) or 1.0
        quote_total = sum(_f(f["quoteQty"]) for f in fills)
        price = quote_total / qty
        commission = sum(
            _f(f["commission"]) * (price if f.get("commissionAsset") != self.quote
                                   else 1.0)
            for f in fills)

        return {
            "ticket": int(ticket),
            "pnl": round(-commission, 4),   # entry side: cost only
            "close_price": round(price, 2),
            "close_time": int(max(f["time"] for f in fills) / 1000),
            "win": False,
            "qty": qty,
        }

    # ── proof of state ────────────────────────────────────────────────────────

    def state(self, session: str = "") -> dict:
        tick = self.get_tick()
        return {
            "tick_time": tick.get("time", 0.0),
            "ask": tick.get("ask", 0.0),
            "bid": tick.get("bid", 0.0),
            "spread_pts": tick.get("spread_pts", 0.0),
            "balance": self.get_balance(),
            "free_margin": self.get_free_margin(),
            "open_positions": len(self.get_open_positions()),
            "session": session,
            "session_open": self.session_open_price(session) if session else None,
        }


def connect(*, testnet: bool = True, symbol: str = "BTCUSDT",
            volume: float = 0.001, journal: Any = None,
            session_opens: Any = None) -> BinanceMarketData:
    """
    Open a Binance connection. Testnet by default, deliberately: a venue
    integrated in days should not meet real money by accident.
    """
    client = BinanceClient(base_url=TESTNET if testnet else MAINNET)
    data = BinanceMarketData(client, symbol=symbol, volume=volume,
                             journal=journal, session_opens=session_opens)
    tick = data.get_tick()
    if "error" in tick:
        raise BinanceError(f"cannot reach Binance: {tick['error']}")
    logger.info("Binance connected (%s) — %s ask %.2f spread %.2f",
                "testnet" if testnet else "LIVE", symbol,
                tick["ask"], tick["spread_pts"])
    return data
