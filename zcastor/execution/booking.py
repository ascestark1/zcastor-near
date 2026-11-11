"""
Booking closes.

One path, one place. Whether a trade ends at its stop, its target, a manual
close, or a zone flip, it is booked here — by the poller noticing the ticket has
gone from the broker's open list.

The rules this enforces, all of them learned from v1 losing data:

**PnL comes from the broker's deal history, never from a price difference.**
`get_closed_deal` sums profit, swap, commission and fee across every deal on the
position. A computed `(close - entry) × volume` ignores costs and disagrees with
the account statement, which makes the edge ledger measure something that did not
happen.

**None means retry, never zero.** A position with only entry deals is not closed.
Booking 0.0 writes a false outcome into the journal and the ledger at once.

**Booking is idempotent.** The journal refuses a second close for a ticket. The
poller can and will see the same ticket twice across a restart.

**The edge ledger gets R-multiples, not dollars.** A segment's edge has to be
comparable across account sizes and stop distances.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("zcastor.execution.booking")


class Booker:
    def __init__(
        self,
        *,
        market: Any,
        journal: Any,
        ledger: Any = None,
        on_closed: Optional[Callable[[dict], None]] = None,
        policy_hash: str = "",
    ) -> None:
        self._market = market
        self._journal = journal
        self._ledger = ledger
        self._on_closed = on_closed
        # Stamped on every ledger entry so a geometry change cannot inherit
        # evidence gathered under the previous one.
        self._policy_hash = policy_hash

    def book(self, ticket: int) -> Optional[dict]:
        """
        Book one closed ticket. Returns the trade record, or None if the
        position is not actually closed yet.
        """
        trade = self._journal.get(ticket)
        if trade is None:
            logger.warning("close for unjournalled ticket %s — ignoring", ticket)
            return None
        if trade.get("state") == "closed":
            return None

        deal = self._market.get_closed_deal(ticket)
        if deal is None:
            # Not closed yet, or history unavailable. Retry next cycle.
            return None

        booked = self._journal.close_trade(
            ticket=ticket,
            pnl=deal["pnl"],
            close_price=deal["close_price"],
            close_time=deal["close_time"],
        )
        if not booked:
            return None

        record = self._journal.get(ticket)
        self._record_edge(record)

        if self._on_closed:
            try:
                self._on_closed(record)
            except Exception:  # noqa: BLE001
                logger.exception("on_closed hook failed for %s", ticket)

        logger.info("booked ticket=%s pnl=%.2f (%s)", ticket, deal["pnl"],
                    "win" if deal["win"] else "loss")
        return record

    def _record_edge(self, trade: dict) -> None:
        """
        R-multiple = realised PnL divided by the dollar risk that was taken.

        Risk is reconstructed from the stop distance recorded at entry, not from
        the current stop — a trade whose stop was moved still risked what it
        risked when the decision was made.
        """
        if self._ledger is None:
            return
        try:
            entry = float(trade.get("entry_price") or 0)
            stop = float(trade.get("sl") or 0)
            volume = float(trade.get("volume") or 0)
            risk = abs(entry - stop) * volume
            if risk <= 0:
                logger.debug("no reconstructable risk for %s — not recorded",
                             trade.get("ticket"))
                return
            self._ledger.record(
                timeframe=str(trade.get("timeframe", "")),
                session=str(trade.get("session", "")),
                route=str(trade.get("route", "market")),
                r_multiple=float(trade.get("pnl", 0.0)) / risk,
                signal_id=str(trade.get("signal_id", "")),
                policy=self._policy_hash,
            )
        except Exception:  # noqa: BLE001
            logger.exception("edge ledger record failed")


class ClosePoller:
    """
    Detects closes by diffing the broker's open tickets against the journal's.

    Polling rather than events because MT5 gives us no close callback, and
    because a poller is restart-safe by construction: whatever closed while the
    process was down is simply missing from the open list on the next tick.
    """

    def __init__(self, *, market: Any, journal: Any, booker: Any) -> None:
        self._market = market
        self._journal = journal
        self._booker = booker

    def poll(self) -> list[dict]:
        expected = set(self._journal.open_tickets())
        if not expected:
            return []

        try:
            live = {int(p["ticket"]) for p in self._market.get_open_positions()}
        except Exception:  # noqa: BLE001
            logger.exception("positions unavailable — skipping this cycle")
            return []

        booked = []
        for ticket in sorted(expected - live):
            record = self._booker.book(ticket)
            if record:
                booked.append(record)
        return booked
