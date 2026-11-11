"""
Market data port.

Two classes, and the split is deliberate:

  MarketData   — reads. Ticks, balance, margin, positions, deal history.
  OrderPlacer  — writes. Orders, closes, cancellations.

Gates are handed a `MarketData` and nothing else. A gate therefore *cannot*
place an order, because the object it holds has no method to do so. In v1 the
zone gate closed positions inline; that was possible because everything shared
one connector. This is the same rule enforced structurally instead of by comment.

Per-decision caching
--------------------
v1 called `account_info()` separately in the drawdown gate and again in the
viability gate, and `get_open_positions()` twice more — four round trips over
rpyc for one signal, each returning a slightly different instant. `begin()`
starts a decision and clears the cache; within one decision, balance is read
once and every gate sees the same number.

That is not only faster, it is more correct: Proof of State claims the decision
was made under specific conditions, and it should be one set of conditions.

Credentials come from the environment. `config.json` in v1 held the MT5 password
in plaintext and was committed; nothing in v2 reads secrets from a tracked file.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("zcastor.market.mt5")

_BUY_WORDS = ("UP", "BUY", "LONG")


class MT5Unavailable(RuntimeError):
    """The bridge is not reachable or the terminal refused to initialise."""


def _f(value: Any, default: float = 0.0) -> float:
    """MT5 returns numpy scalars over rpyc; `.item()` unwraps them."""
    if value is None:
        return default
    try:
        return float(value.item() if hasattr(value, "item") else value)
    except (TypeError, ValueError):
        return default


def _rows(rates: Any) -> list:
    """
    Bring a rates array across the bridge in ONE transfer.

    Indexing a remote numpy array field by field costs one rpyc round trip per
    value. Measured 2 Oct 2026: 360 one-minute bars took 102s, 1,440 took 411s,
    so the week the hourly levels need (10,080 bars) took most of an hour and
    blocked startup. tolist() runs on the remote side and obtain() pickles the
    plain result back in a single call; the field names come with it.

    Falls back to the old element-by-element path if the bulk transfer fails,
    so a bridge without pickling still works, only slowly.
    """
    try:
        from rpyc.utils.classic import obtain           # noqa: PLC0415
    except ImportError:                                 # local arrays, tests
        def obtain(value: Any) -> Any:
            return value
    try:
        names = tuple(obtain(rates.dtype.names))
        return [dict(zip(names, row)) for row in obtain(rates.tolist())]
    except Exception as exc:  # noqa: BLE001
        logger.warning("bulk rates transfer failed (%s); reading per element", exc)
        return list(rates)


class MarketData:
    """Read-only view of the broker."""

    def __init__(
        self,
        mt5: Any,
        *,
        symbol: str = "BTCUSD",
        volume: float = 0.01,
        session_opens: Any = None,
    ) -> None:
        self._mt5 = mt5
        self.symbol = symbol
        self.volume = volume
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}
        self._offset: Optional[int] = None
        self._clock = time.time
        self._sessions = session_opens

    # ── decision lifecycle ────────────────────────────────────────────────────

    def begin(self) -> None:
        """Start a new decision. Everything cached is discarded."""
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
            tick = self._mt5.symbol_info_tick(self.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_tick failed: %s", exc)
            return {"error": str(exc)}

        if tick is None:
            return {"error": f"no tick data for {self.symbol}"}

        ask, bid = _f(getattr(tick, "ask", None)), _f(getattr(tick, "bid", None))
        if ask <= 0 or bid <= 0:
            return {"error": "tick has no usable ask/bid"}

        return {
            "price": (ask + bid) / 2,
            "ask": ask,
            "bid": bid,
            "spread_pts": round(ask - bid),
            # Broker-frame epoch. The feed gate compares it across calls, so it
            # only has to be internally consistent, not aligned to our clock.
            "time": _f(getattr(tick, "time", 0)),
        }

    # ── time ──────────────────────────────────────────────────────────────────

    def server_offset(self) -> int:
        """
        Seconds by which MT5 timestamps run ahead of true UTC.

        MT5 reports deal and bar times as SERVER time packaged as a Unix
        timestamp. XM runs UTC+3 in summer and UTC+2 in winter, so this is
        detected from a live tick rather than hardcoded.

        Unnoticed for weeks: the journal held opened_at in UTC and close_time
        in server time, so every trade appeared three hours longer than it was
        and every reconstruction against prices overran the real close.
        """
        if self._offset is not None:
            return self._offset
        try:
            tick = self._mt5.symbol_info_tick(self.symbol)
            server = int(_f(getattr(tick, "time", 0)))
        except Exception:  # noqa: BLE001
            server = 0
        if not server:
            return 0
        offset = int(round((server - int(self._clock())) / 900.0)) * 900
        # No real timezone is more than 14 hours from UTC. Anything further is
        # a bad reading, and applying it would shift every timestamp silently.
        if abs(offset) > 14 * 3600:
            logger.warning("implausible MT5 server offset %+.1fh — ignoring",
                           offset / 3600)
            return 0
        self._offset = offset
        logger.info("MT5 server time is UTC%+.2fh", offset / 3600)
        return offset

    def to_utc(self, server_ts: float) -> int:
        return int(server_ts) - self.server_offset() if server_ts else 0

    def get_rates_range(self, from_ts: float, to_ts: float) -> list:
        try:
            rates = self._mt5.copy_rates_range(
                self.symbol, self._m1(), int(from_ts), int(to_ts)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_rates_range failed: %s", exc)
            return []
        if rates is None:
            return []
        return [{"time": float(self.to_utc(_f(r["time"]))), "open": _f(r["open"]),
                 "high": _f(r["high"]), "low": _f(r["low"]),
                 "close": _f(r["close"])} for r in _rows(rates)]

    def _m1(self) -> int:
        return int(getattr(self._mt5, "TIMEFRAME_M1", 1) or 1)

    def session_open_price(self, session: str) -> float:
        if self._sessions is None:
            return 0.0
        return self._sessions.open_price(session)

    # ── account ───────────────────────────────────────────────────────────────

    def _account(self) -> Any:
        return self._cached("account", self._fetch_account)

    def _fetch_account(self) -> Any:
        try:
            return self._mt5.account_info()
        except Exception as exc:  # noqa: BLE001
            logger.warning("account_info failed: %s", exc)
            return None

    def get_balance(self) -> Optional[float]:
        """
        None means unreadable — NOT zero.

        v1 returned 0.0 on failure, which the dead-man gate could not
        distinguish from a genuinely empty account. The gates now fail closed on
        None, so the ambiguity has to be gone from the source.
        """
        info = self._account()
        return _f(getattr(info, "balance", None), 0.0) if info is not None else None

    def get_free_margin(self) -> Optional[float]:
        info = self._account()
        return _f(getattr(info, "margin_free", None), 0.0) if info is not None else None

    def get_equity(self) -> Optional[float]:
        info = self._account()
        return _f(getattr(info, "equity", None), 0.0) if info is not None else None

    def get_margin_for(self, direction: str, volume: float, price: float) -> float:
        """
        Margin MT5 says this specific order needs. Not cached — it depends on
        the trade. 0.0 means unavailable and the viability gate falls back to a
        conservative estimate.
        """
        try:
            order_type = 0 if str(direction).upper() in _BUY_WORDS else 1
            margin = self._mt5.order_calc_margin(
                order_type, self.symbol, float(volume), float(price)
            )
            return _f(margin, 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_margin_for failed: %s", exc)
            return 0.0

    # ── positions and deals ───────────────────────────────────────────────────

    def get_open_positions(self) -> list:
        return self._cached("positions", self._fetch_positions)

    def _fetch_positions(self) -> list:
        try:
            positions = self._mt5.positions_get(symbol=self.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("positions_get failed: %s", exc)
            return []
        if not positions:
            return []
        return [{
            "ticket": int(_f(getattr(p, "ticket", 0))),
            "side": "BUY" if int(_f(getattr(p, "type", 0))) == 0 else "SELL",
            "volume": _f(getattr(p, "volume", 0)),
            "open_price": _f(getattr(p, "price_open", 0)),
            "sl": _f(getattr(p, "sl", 0)),
            "tp": _f(getattr(p, "tp", 0)),
            "profit": _f(getattr(p, "profit", 0)),
        } for p in positions]

    def get_closed_deal(self, ticket: int) -> Optional[dict]:
        """
        Realised result of a closed position, in account currency, from the
        broker's own deal history.

        Sums NET across every deal on the position — profit, swap, commission
        and fee — so partial closes and entry-side commissions are all captured.
        Close price is volume-weighted across the closing deals; close time is
        the latest of them.

        Returns None when no closing deal exists yet. Callers MUST treat None as
        "not closed, retry" and never as a zero-PnL close: booking a zero would
        write a false outcome into the journal and the edge ledger, and the
        ledger's whole value is that its numbers are real.
        """
        try:
            deals = self._mt5.history_deals_get(position=int(ticket))
        except Exception as exc:  # noqa: BLE001
            logger.warning("history_deals_get failed for %s: %s", ticket, exc)
            return None

        if not deals:
            return None

        net = 0.0
        closing = []
        for d in deals:
            for field in ("profit", "swap", "commission", "fee"):
                net += _f(getattr(d, field, 0.0))
            if int(_f(getattr(d, "entry", -1), -1)) == 1:      # DEAL_ENTRY_OUT
                closing.append(d)

        if not closing:
            return None                                # entry deals only

        total_volume = sum(_f(d.volume) for d in closing) or 1.0
        close_price = sum(_f(d.price) * _f(d.volume) for d in closing) / total_volume

        return {
            "ticket": int(ticket),
            "pnl": round(net, 2),
            "close_price": round(close_price, 2),
            "close_time": self.to_utc(max(int(_f(d.time)) for d in closing)),
            "win": net > 0,
        }

    # ── state for Proof of State ──────────────────────────────────────────────

    def state(self, session: str = "") -> dict:
        """
        The conditions this decision was made under, shaped for
        `record.proof.state_hash`. Uses the cache, so it costs nothing extra
        and describes the same instant every gate saw.
        """
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


class RpycMT5:
    """
    The remote `MetaTrader5` module, reached over rpyc.

    We do not use `mt5linux`. It was a thin wrapper over exactly this, and it has
    since drifted to a Docker-managed design that would spin up a second MT5
    beside the Wine terminal you already have logged in. Connecting directly is
    fewer moving parts and one less dependency that can change under us.

    The connection is held on this object because rpyc netrefs die with their
    connection — letting it be garbage collected turns every later call into an
    EOFError with no obvious cause.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._mt5 = conn.modules.MetaTrader5

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mt5, name)

    def send_request(self, request: dict) -> Any:
        """
        Run `order_send` entirely inside the remote interpreter.

        Reads work over netrefs because MT5 only hands values back. Writes do
        not. `order_send` takes a dict and inspects it with the CPython C API,
        and an rpyc proxy fails that inspection — the terminal answers
        `(-2, 'Unnamed arguments not allowed')` and returns None, which reads
        in the logs exactly like a dead bridge.

        `rpyc.classic.deliver` is not enough either: it copies the object to the
        remote side but hands back a reference, so the call still arrives
        holding a proxy.

        So the dict is constructed remotely from its literal repr and the call
        is made there. Every value is a str, int or float, so the repr
        round-trips exactly.
        """
        self._ensure_remote()
        self._conn.execute(f"_zc_req = {request!r}")
        return self._conn.eval("_zc_mt5.order_send(_zc_req)")

    def _ensure_remote(self) -> None:
        if getattr(self, "_remote_ready", False):
            return
        self._conn.execute("import MetaTrader5 as _zc_mt5")
        self._remote_ready = True

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


def connect(
    *,
    symbol: Optional[str] = None,
    volume: Optional[float] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Any:
    """
    Open the rpyc bridge to MT5 under Wine and initialise the terminal.

    Credentials are read from the environment only: MT5_LOGIN, MT5_PASSWORD,
    MT5_SERVER. Nothing here reads a tracked config file.
    """
    try:
        import rpyc                                 # noqa: PLC0415
    except ImportError as exc:
        raise MT5Unavailable("rpyc is not installed: pip install rpyc") from exc

    host = host or os.environ.get("MT5_BRIDGE_HOST", "localhost")
    port = int(port or os.environ.get("MT5_BRIDGE_PORT", 18812))

    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    if not (login and password and server):
        raise MT5Unavailable(
            "MT5_LOGIN, MT5_PASSWORD and MT5_SERVER must be set in the "
            "environment. They are deliberately not read from config."
        )

    try:
        conn = rpyc.classic.connect(host, port)
    except Exception as exc:  # noqa: BLE001
        raise MT5Unavailable(
            f"cannot reach the MT5 bridge at {host}:{port} ({exc}). "
            f"Is ops/start_mt5_bridge.sh running?"
        ) from exc

    mt5 = RpycMT5(conn)

    if not mt5.initialize(login=int(login), password=password, server=server):
        raise MT5Unavailable(
            f"MT5 initialize() failed: {mt5.last_error()}. Check that "
            f"MT5_SERVER matches the terminal's server name exactly."
        )

    info = mt5.account_info()
    logger.info("MT5 connected — account=%s server=%s balance=%.2f %s",
                getattr(info, "login", "?"), getattr(info, "server", "?"),
                _f(getattr(info, "balance", 0)), getattr(info, "currency", ""))
    return mt5
