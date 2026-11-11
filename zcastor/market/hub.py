"""
Market hub: one Hyperliquid connection, any number of dashboards.

The dashboard was written against Binance's REST paths. Rather than rewrite the
signal code (and risk changing a signal), the hub answers those same paths on
localhost with Hyperliquid data:

    GET /fapi/v1/klines?symbol=ETH&interval=15m&limit=500[&endTime=ms]
    GET /fapi/v1/ticker/24hr?symbol=ETH
    GET /fapi/v1/premiumIndex?symbol=ETH      (funding as an 8-hour rate)
    GET /fapi/v1/openInterest?symbol=ETH
    GET /v5/market/tickers?symbol=ETH         (cross-check: Hyperliquid's oracle price)
    GET /desk/scale?symbol=ETH                (BTC points -> this perp's price units)
    GET /health

Thirty dashboards each refreshing every few seconds would blow through
Hyperliquid's rate limit if each called it. Through the hub they cost:

- one `metaAndAssetCtxs` call every `ctx_ttl` seconds (prices, volume,
  funding, open interest and oracle price for every perp at once), made by a
  background thread, so ticker requests never wait on the network;
- one candle call per (symbol, interval) when its cache goes stale, fetching
  only the bars since the last one it holds.

The hub binds to 127.0.0.1 only. It holds no key and can't trade.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .hyperliquid import (INTERVAL_MS, MAX_CANDLES, BinanceView, HLInfo, api_base,
                          kline_row, urllib_transport)

logger = logging.getLogger("zcastor.market.hub")

DEFAULT_PORT = 8170
ROOT = Path(__file__).resolve().parents[2]
REFERENCE = "BTC"


class UnknownSymbol(ValueError):
    pass


class Hub:
    def __init__(self, view: BinanceView, *, clock: Callable[[], float] = time.time,
                 history_ttl: float = 900.0,
                 profiles: Optional[Path] = None) -> None:
        self.view = view
        self.profiles = Path(profiles) if profiles else ROOT / "data" / "profiles"
        self._clock = clock
        self._hl = threading.Lock()          # one Hyperliquid call at a time
        self._cache_lock = threading.Lock()
        self._live: dict[tuple[str, str], dict] = {}
        self._pages: dict[tuple, tuple[float, list]] = {}
        self._history_ttl = history_ttl
        self.ctx_error: Optional[str] = None
        self.served = 0

    # ── context (prices, funding, OI) ─────────────────────────────────────────

    def refresh_ctx(self) -> None:
        try:
            with self._hl:
                self.view.refresh(force=True)
            self.ctx_error = None
        except Exception as exc:  # noqa: BLE001
            self.ctx_error = str(exc)
            logger.warning("context refresh failed: %s", exc)

    def _known(self, coin: str) -> str:
        if not self.view._specs:
            self.refresh_ctx()
        if coin not in self.view._specs:
            raise UnknownSymbol(coin)
        return coin

    def ticker(self, coin: str) -> dict:
        return self.view.ticker_24h(self._known(coin))

    def premium(self, coin: str) -> dict:
        return self.view.premium(self._known(coin))

    def open_interest(self, coin: str) -> dict:
        return self.view.open_interest(self._known(coin))

    def cross_check(self, coin: str) -> dict:
        c = self.view._ctxs.get(self._known(coin), {})
        oracle = str(c.get("oraclePx") or "0")
        return {"retCode": 0, "retMsg": "OK", "result": {"category": "linear",
                "list": [{"symbol": coin, "markPrice": oracle, "lastPrice": oracle,
                          "source": "hyperliquid-oracle"}]}}

    def scale(self, coin: str) -> dict:
        """
        How many of this perp's price units one BTC point is worth: the 1h ATR
        ratio from its profile, or, until a profile exists, the price ratio
        (which assumes it moves as much as BTC in percent; the profile is better).
        """
        self._known(coin)
        if coin == REFERENCE:
            return {"symbol": coin, "scale": 1.0, "basis": "reference"}
        path = self.profiles / f"{coin}.json"
        if path.exists():
            try:
                doc = json.loads(path.read_text())
                s = (doc.get("btc_scale") or {}).get("by_tf", {}).get("1h")
                if s and doc.get("venue") == "hyperliquid":
                    return {"symbol": coin, "scale": float(s),
                            "basis": f"profile atr_1h {doc.get('generated_at', '')}"}
            except (OSError, ValueError):
                pass
        px = float(self.view._ctxs.get(coin, {}).get("markPx") or 0)
        ref = float(self.view._ctxs.get(REFERENCE, {}).get("markPx") or 0)
        if px > 0 and ref > 0:
            return {"symbol": coin, "scale": px / ref, "basis": "price_ratio (no profile)"}
        raise ValueError(f"no scale for {coin}: no profile and no prices yet")

    # ── candles ───────────────────────────────────────────────────────────────

    @staticmethod
    def _ttl(interval: str) -> float:
        # Refresh a live series often enough to catch a new bar, never more
        # than once every 5 seconds or less than once a minute.
        return max(5.0, min(60.0, INTERVAL_MS[interval] / 1000 / 10))

    def klines(self, coin: str, interval: str, limit: int = 500,
               end_ms: Optional[int] = None) -> list:
        self._known(coin)
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval {interval!r}")
        limit = max(1, min(int(limit), MAX_CANDLES))
        if end_ms is not None:
            return self._history_page(coin, interval, limit, int(end_ms))
        return self._live_series(coin, interval, limit)

    def _history_page(self, coin, interval, limit, end_ms) -> list:
        step = INTERVAL_MS[interval]
        key = (coin, interval, limit, end_ms // step)
        now = self._clock()
        with self._cache_lock:
            hit = self._pages.get(key)
            if hit and now - hit[0] < self._history_ttl:
                return hit[1]
        with self._hl:
            rows = self.view.klines(coin, interval, end_ms=end_ms, limit=limit)
        with self._cache_lock:
            self._pages[key] = (now, rows)
            if len(self._pages) > 2000:
                for k in sorted(self._pages, key=lambda k: self._pages[k][0])[:500]:
                    del self._pages[k]
        return rows

    def _live_series(self, coin, interval, limit) -> list:
        key = (coin, interval)
        now = self._clock()
        with self._cache_lock:
            entry = self._live.get(key)
            fresh = entry and now - entry["at"] < self._ttl(interval)
            if fresh and entry["depth"] >= limit:
                return entry["rows"][-limit:]
        with self._hl:
            # Another thread may have refreshed it while this one waited.
            with self._cache_lock:
                entry = self._live.get(key)
                if entry and now - entry["at"] < self._ttl(interval) \
                        and entry["depth"] >= limit:
                    return entry["rows"][-limit:]
            end = int(self._clock() * 1000)
            if entry and entry["depth"] >= limit and entry["rows"]:
                since = entry["rows"][-1][0]          # re-fetch the open bar
                fresh_rows = [kline_row(c) for c in
                              self.view.info.candles(coin, interval, since, end)]
                merged = {r[0]: r for r in entry["rows"]}
                merged.update({r[0]: r for r in fresh_rows})
                rows = [merged[t] for t in sorted(merged)][-max(entry["depth"], limit):]
                depth = entry["depth"]
            else:
                rows = self.view.klines(coin, interval, end_ms=end, limit=limit)
                depth = limit
            with self._cache_lock:
                self._live[key] = {"rows": rows, "at": self._clock(), "depth": depth}
        return rows[-limit:]

    def health(self) -> dict:
        age = self._clock() - self.view._ctx_at
        return {"ok": self.ctx_error is None and age < 30,
                "source": "hyperliquid",
                "coins": len(self.view._specs),
                "context_age_seconds": round(age, 1) if age < 1e9 else None,
                "context_error": self.ctx_error,
                "weight_this_minute": self.view.info.used_weight,
                "requests": self.view.info.requests,
                "cached_series": len(self._live),
                "served": self.served}


# ── HTTP ─────────────────────────────────────────────────────────────────────

def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "zcastor-hub"

        def log_message(self, fmt, *args):  # quiet: thirty tabs, every 6 seconds
            logger.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.end_headers()

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            sym = q.get("symbol", "")
            try:
                if url.path == "/health":
                    return self._send(200, hub.health())
                if url.path == "/fapi/v1/klines":
                    end = q.get("endTime")
                    body = hub.klines(sym, q.get("interval", "1h"),
                                      int(q.get("limit", 500)),
                                      int(end) if end else None)
                elif url.path == "/fapi/v1/ticker/24hr":
                    body = hub.ticker(sym)
                elif url.path == "/fapi/v1/premiumIndex":
                    body = hub.premium(sym)
                elif url.path == "/fapi/v1/openInterest":
                    body = hub.open_interest(sym)
                elif url.path == "/v5/market/tickers":
                    body = hub.cross_check(sym)
                elif url.path == "/desk/scale":
                    body = hub.scale(sym)
                else:
                    return self._send(404, {"code": -1, "msg": f"no route {url.path}"})
            except UnknownSymbol:
                return self._send(400, {"code": -1121, "msg": f"Invalid symbol {sym!r}."})
            except ValueError as exc:
                return self._send(400, {"code": -1100, "msg": str(exc)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s failed: %s", url.path, exc)
                return self._send(502, {"code": -1001, "msg": f"hyperliquid: {exc}"})
            hub.served += 1
            self._send(200, body)

    return Handler


def serve(hub: Hub, port: int = DEFAULT_PORT, ctx_ttl: float = 3.0,
          ready: Optional[threading.Event] = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(hub))
    server.daemon_threads = True
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            hub.refresh_ctx()
            stop.wait(ctx_ttl)

    hub.refresh_ctx()
    threading.Thread(target=poll, name="hub-ctx", daemon=True).start()
    server._zc_stop = stop  # type: ignore[attr-defined]
    if ready:
        ready.set()
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m zcastor.market.hub")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--testnet", action="store_true",
                    help="read Hyperliquid testnet instead of mainnet")
    ap.add_argument("--ctx-seconds", type=float, default=3.0)
    ap.add_argument("--weight-budget", type=int, default=800)
    ap.add_argument("--profiles", default=str(ROOT / "data" / "profiles"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    base = api_base(args.testnet)
    info = HLInfo(urllib_transport(base), weight_budget=args.weight_budget)
    hub = Hub(BinanceView(info, ctx_ttl=args.ctx_seconds), profiles=Path(args.profiles))
    server = serve(hub, args.port, args.ctx_seconds)
    h = hub.health()
    print(f"  market hub on http://127.0.0.1:{args.port}  "
          f"(Hyperliquid at {base}, "
          f"{h['coins']} perps{'' if h['ok'] else ', NOT READY: ' + str(h['context_error'])})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
