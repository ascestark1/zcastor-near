"""
Trade journal.

One record per trade, opened at fill and closed when the broker's deal history
says so. Append-only JSONL with an in-memory index, same reasoning as the
outbox: a rewritten JSON file is one bad shutdown away from losing everything.

Two rules carried from v1, both learned from real damage:

**Closing is idempotent.** The close poller can see the same ticket twice —
across a restart, or when a partial close lands between polls. Booking a trade
twice double-counts the PnL, which corrupts the drawdown gate and the edge
ledger simultaneously.

**A missing deal is not a zero-PnL close.** `get_closed_deal` returns None when
the position is not fully closed. Writing that as 0.0 puts a false outcome into
the ledger, and the ledger's only value is that its numbers are real.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("zcastor.data.journal")


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._trades: dict[int, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticket = row.get("ticket")
            if ticket is None:
                continue
            # Later lines for a ticket supersede earlier ones — this is how a
            # close updates an open record without rewriting the file.
            existing = self._trades.get(int(ticket), {})
            existing.update(row)
            self._trades[int(ticket)] = existing

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    # ── writes ────────────────────────────────────────────────────────────────

    def open_trade(self, *, ticket: int, signal_id: str, direction: str,
                   timeframe: str, session: str, route: str,
                   entry_price: float, sl: float, tp: float,
                   volume: float, opened_at: Optional[str] = None) -> None:
        row = {
            "ticket": int(ticket), "signal_id": signal_id,
            "direction": direction, "timeframe": timeframe, "session": session,
            "route": route, "entry_price": float(entry_price),
            "sl": float(sl), "tp": float(tp), "volume": float(volume),
            "opened_at": opened_at or _now(), "state": "open",
        }
        with self._lock:
            if int(ticket) in self._trades:
                logger.warning("ticket %s already journalled", ticket)
                return
            self._trades[int(ticket)] = row
        self._append(row)

    def close_trade(self, *, ticket: int, pnl: float, close_price: float,
                    close_time: Optional[int] = None) -> bool:
        """
        Book a close. Returns False if already booked.

        The idempotency guard is the whole point: double-booking corrupts the
        drawdown gate and the edge ledger at the same time, and neither failure
        is visible until much later.
        """
        with self._lock:
            trade = self._trades.get(int(ticket))
            if trade is None:
                logger.warning("close for unknown ticket %s", ticket)
                return False
            if trade.get("state") == "closed":
                return False

            update = {
                "ticket": int(ticket), "state": "closed",
                "pnl": round(float(pnl), 2),
                "close_price": float(close_price),
                "close_time": int(close_time or 0),
                "closed_at": _now(),
                "win": float(pnl) > 0,
            }
            trade.update(update)

        self._append(update)
        return True

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, ticket: int) -> Optional[dict[str, Any]]:
        with self._lock:
            trade = self._trades.get(int(ticket))
            return dict(trade) if trade else None

    def open_tickets(self) -> list[int]:
        with self._lock:
            return [t for t, r in self._trades.items() if r.get("state") == "open"]

    def today_pnl(self, today: Optional[str] = None) -> float:
        """
        Realised PnL since UTC midnight. Feeds the dead-man gate's
        start-of-day reconstruction, so it counts closed trades only —
        an open position's floating PnL is not realised and must not
        halt trading on a wobble.
        """
        day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            return round(sum(
                float(r.get("pnl", 0.0))
                for r in self._trades.values()
                if r.get("state") == "closed"
                and str(r.get("closed_at", "")).startswith(day)
            ), 2)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            trades = list(self._trades.values())
        closed = [t for t in trades if t.get("state") == "closed"]
        wins = [t for t in closed if t.get("win")]
        return {
            "total": len(trades),
            "open": len(trades) - len(closed),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 3) if closed else 0.0,
            "net_pnl": round(sum(float(t.get("pnl", 0.0)) for t in closed), 2),
        }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
