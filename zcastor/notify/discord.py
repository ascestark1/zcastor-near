"""
Discord notifications.

Deliberately quiet. The system refuses most of what it sees, so posting every
decision would produce hundreds of messages a day and you would stop reading
them by the second afternoon. A notifier nobody reads is worse than none,
because it creates the impression of oversight without providing it.

So: fills, closes, and things that need a human. Refusals go to the decision log
and the daily record, which is where they belong.

Never blocks and never raises. A webhook outage must not delay an order or take
down the process.

Posts are QUEUED AND RETRIED rather than dropped. The router restarts at 03:00
here, which is midnight UTC — exactly when the session rolls over and the daily
summary posts. Twelve consecutive summaries were lost to that two-minute DNS
outage before this was added. A notifier that silently drops the one message a
day that matters is worse than none, because the silence looks like nothing
happened.

One worker thread drains the queue and paces itself: Discord allows thirty
posts a minute per webhook, so a burst of decisions cannot trip the limit.

The URL comes from DISCORD_WEBHOOK_URL in the environment. v1 kept it in
config.json, which was committed, which is why that webhook had to be
regenerated.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("zcastor.notify")

TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 6          # roughly five minutes of backoff, past a router restart
MIN_INTERVAL = 2.2        # seconds between posts; Discord allows 30/min
QUEUE_LIMIT = 500


class Discord:
    def __init__(self, webhook_url: Optional[str] = None, *,
                 enabled: bool = True, venue: str = "",
                 events: Optional[set] = None) -> None:
        # None means "look at the environment". An empty string means the
        # caller explicitly wants this off, and must not be quietly overridden
        # by whatever happens to be exported in the shell — which is exactly
        # what `webhook_url or os.environ.get(...)` did.
        if webhook_url is None:
            self.url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        else:
            self.url = webhook_url
        self.enabled = bool(enabled and self.url)
        # Stamped on every decision. Four days on the wrong venue went
        # unnoticed because it was stated once at startup and never again.
        self.venue = venue
        # Which kinds of message go out. Default is fills and their results:
        # every decision was ~40 posts a day, which buried the ones that
        # carry money. Anything not listed is dropped before it is queued.
        self.events = set(events) if events is not None else {"fill", "close"}
        if enabled and not self.url:
            logger.info("no DISCORD_WEBHOOK_URL set — notifications disabled")

        self._queue: "queue.Queue" = queue.Queue(QUEUE_LIMIT)
        self._last_post = 0.0
        self._dropped = 0
        if self.enabled:
            threading.Thread(target=self._drain, daemon=True,
                             name="discord").start()

    # ── the worker ────────────────────────────────────────────────────────────

    def _drain(self) -> None:
        while True:
            content, attempts = self._queue.get()
            # Pace against the rate limit rather than discovering it.
            wait = MIN_INTERVAL - (time.time() - self._last_post)
            if wait > 0:
                time.sleep(wait)

            if self._post(content):
                self._last_post = time.time()
                continue

            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                self._dropped += 1
                logger.warning("gave up on a discord post after %d attempts",
                               attempts)
                continue
            # A router restart takes about two minutes; this rides out five.
            time.sleep(min(2 ** attempts, 64))
            try:
                self._queue.put_nowait((content, attempts))
            except queue.Full:
                self._dropped += 1

    # ── transport ─────────────────────────────────────────────────────────────

    def _post(self, content: str) -> bool:
        """One attempt. True on success, False when a retry is worthwhile."""
        if not self.enabled:
            return True
        payload = json.dumps({"content": content[:1900]}).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload,
            headers={
                "Content-Type": "application/json",
                # Discord's edge rejects the default Python-urllib agent with
                # a 403 before the request ever reaches the webhook. The URL
                # can be perfectly valid and still fail without this.
                "User-Agent": "Zcastor/2.0 (+https://afritensor.com)",
            })
        try:
            urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS).close()
            return True
        except urllib.error.HTTPError as exc:
            # Read the body: Discord explains itself, and the status alone
            # cannot distinguish a deleted webhook from a blocked client.
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            logger.warning("discord post failed: HTTP %s %s %s",
                           exc.code, exc.reason, detail)
            # A 4xx other than rate-limiting means the request itself is wrong.
            # Retrying a deleted webhook forever helps nobody.
            return exc.code not in (400, 401, 403, 404)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.debug("discord post failed, will retry: %s", exc)
            return False

    def send(self, content: str) -> None:
        """Queue a post. Never blocks the caller, never raises."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait((content, 0))
        except queue.Full:
            # Shedding load is the right failure: the trade loop must not stall
            # because a chat service is unreachable.
            self._dropped += 1
            logger.warning("discord queue full — dropping a post")

    def wants(self, kind: str) -> bool:
        return kind in self.events

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "queued": self._queue.qsize(),
                "dropped": self._dropped, "events": sorted(self.events)}

    # ── events ────────────────────────────────────────────────────────────────

    def filled(self, *, signal_id: str, side: str, price: float, sl: float,
               tp: Optional[float], route: str = "market",
               timeframe: str = "", session: str = "") -> None:
        if not self.wants("fill"):
            return
        target = f"{tp:,.2f}" if tp else "none"
        self.send(
            f"**FILL** `{side}` {timeframe} {session}\n"
            f"entry `{price:,.2f}`  stop `{sl:,.2f}`  target `{target}`\n"
            f"route `{route}`  id `{signal_id}`"
        )

    def closed(self, trade: dict) -> None:
        if not self.wants("close"):
            return
        pnl = float(trade.get("pnl", 0.0))
        mark = "🟢" if pnl > 0 else "🔴"
        self.send(
            f"{mark} **CLOSED** `{trade.get('direction', '')}` "
            f"{trade.get('timeframe', '')} {trade.get('session', '')}\n"
            f"pnl `{pnl:+.2f}`  entry `{float(trade.get('entry_price', 0)):,.2f}` "
            f"exit `{float(trade.get('close_price', 0)):,.2f}`\n"
            f"route `{trade.get('route', '')}`  id `{trade.get('signal_id', '')}`"
        )

    def decision(self, trace: Any, signal: dict) -> None:
        """
        Every decision, including every refusal.

        Roughly forty a day at current signal rates, which is readable. The
        refusals are the point: a system that mostly declines looks dormant
        from the outside, and without a reason attached you cannot tell
        discipline from a dead process.

        One line each. The gate that bound, and the gates that would also have
        objected — that second part is what no performance report shows.
        """
        if not self.wants("decision"):
            return
        action = getattr(trace.action, "value", str(trace.action))
        mark = {"allow": "🟩", "route": "🟦", "suppress": "⬜",
                "reject": "🟥"}.get(action, "▫️")

        binding = trace.binding()
        shadows = [r.name for r in trace.shadow_objections()]
        where = f" at `{binding.name}`" if binding and action != "allow" else ""
        also = f"  ·  also: {', '.join(shadows)}" if shadows else ""
        reason = f" `{trace.reason}`" if trace.reason else ""

        venue = f"`{self.venue.upper()}` " if self.venue else ""
        self.send(
            f"{mark} {venue}`{action.upper()}`{reason}{where}\n"
            f"    {signal.get('direction', '?')} "
            f"{signal.get('timeframe', '?')} "
            f"{signal.get('session', '?')} "
            f"conf={signal.get('confidence', '?')}{also}"
        )

    def armed(self, *, signal_id: str, direction: str, zone: float,
              stop: float, route_kind: str) -> None:
        if not self.wants("armed"):
            return
        self.send(
            f"**ARMED** `{direction}` at `{zone:,.2f}`  "
            f"stop `{stop:,.2f}`  ({route_kind})  id `{signal_id}`"
        )

    def halted(self, reason: str, detail: Any = "") -> None:
        """Something a human needs to see. Dead-man halt, feed failure, parked anchor."""
        if not self.wants("halt"):
            return
        self.send(f"⚠️ **ATTENTION** `{reason}`\n{detail}")

    def session_summary(self, *, day: str, totals: dict, reasons: list,
                        journal: dict) -> None:
        if not self.wants("session"):
            return
        top = "\n".join(f"  `{r['reason']}` × {r['count']}" for r in reasons[:5])
        self.send(
            f"**SESSION {day}**\n"
            f"decisions `{sum(totals.values())}`  "
            f"traded `{totals.get('allow', 0)}`  "
            f"armed `{totals.get('route', 0)}`  "
            f"refused `{totals.get('suppress', 0)}`\n"
            f"closed `{journal.get('closed', 0)}`  "
            f"net `{journal.get('net_pnl', 0):+.2f}`  "
            f"win rate `{journal.get('win_rate', 0):.0%}`\n"
            f"top refusals:\n{top or '  none'}"
        )
