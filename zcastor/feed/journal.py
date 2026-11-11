"""
The signal feed — fills and closes, signed, for followers to copy.

Discord tells people what happened. This tells programs, and has to meet a
higher bar, because a program will act on it with someone else's money:

- **Authentic.** Every event is signed with the operator key, the same key
  that anchors Zcastor's records on NVNM. A follower pins that address once
  and rejects anything else. Whoever relays the feed — the feed server, the
  tunnel, anything in between — can delay or withhold an event but cannot
  forge or alter one.
- **Exact.** The signature covers the payload string as transmitted, the same
  rule as the record layer: what is signed is what is sent, so a follower
  never re-serialises anything before checking it.
- **Ordered.** Each event carries a sequence number inside the signed payload.
  A follower resumes from the last one it handled and can see a gap.
- **Tied to the record.** Each event names the policy hash in force, which is
  anchored in the policy registry, and the signal id, which appears in that
  day's anchored decision log. A follower can check afterwards that what it
  was sent is what the record says happened.

Fills and closes only. Refusals stay in the decision log: a follower acts on
trades, and posting hundreds of non-trades into an execution feed is noise
with a cost.

Never raises and never blocks for long — a feed failure must not touch an
order. The journal is append-only; the feed server (serve.py) reads it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..record.canonical import canonical_bytes

logger = logging.getLogger("zcastor.feed")

SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SignalFeed:
    def __init__(self, path: str | Path, *, private_key: str = "",
                 venue: str = "", symbol: str = "", policy: str = "",
                 enabled: bool = True,
                 clock: Optional[Callable[[], str]] = None) -> None:
        self.path = Path(path)
        self.venue = venue
        self.symbol = symbol
        self.policy = policy
        self._clock = clock or _now_iso
        self._lock = threading.Lock()
        self._account = None
        self.signer = ""
        self.enabled = False
        self.errors = 0

        if not enabled:
            return
        if not private_key:
            logger.warning("signal feed is on but no signing key is set "
                           "(ZCASTOR_FEED_KEY or NVNM_PRIVATE_KEY) — feed off")
            return
        try:
            from eth_account import Account  # noqa: PLC0415
            self._account = Account.from_key(private_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("signal feed key unusable (%s) — feed off",
                           type(exc).__name__)
            return
        self.signer = self._account.address
        self.enabled = True
        self._seq = self._last_seq()
        logger.info("signal feed on — signer %s, %s, seq %d",
                    self.signer, self.path.name, self._seq)

    # ── journal ───────────────────────────────────────────────────────────────

    def _last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                last = max(last, int(json.loads(line)["seq"]))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return last

    def sign(self, payload: str) -> str:
        from eth_account.messages import encode_defunct  # noqa: PLC0415
        signed = self._account.sign_message(encode_defunct(text=payload))
        return "0x" + bytes(signed.signature).hex()

    def _emit(self, kind: str, **fields: Any) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            with self._lock:
                seq = self._seq + 1
                event = {"v": SCHEMA_VERSION, "kind": kind, "seq": seq,
                         "at": self._clock(), "venue": self.venue,
                         "symbol": self.symbol, "policy": self.policy,
                         **fields}
                payload = canonical_bytes(event).decode("utf-8").rstrip("\n")
                envelope = {"seq": seq, "payload": payload,
                            "sig": self.sign(payload), "signer": self.signer}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(envelope, sort_keys=True) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                self._seq = seq
            logger.info("feed %s #%d %s", kind, seq, fields.get("signal_id", ""))
            return envelope
        except Exception:  # noqa: BLE001
            self.errors += 1
            logger.exception("signal feed write failed")
            return None

    # ── events ────────────────────────────────────────────────────────────────

    def fill(self, *, signal_id: str, side: str, price: float, sl: float,
             tp: Optional[float] = None, route: str = "market",
             timeframe: str = "", session: str = "") -> Optional[dict]:
        """Same arguments as Discord.filled, so the two are called alike."""
        return self._emit("fill", signal_id=signal_id, side=str(side).upper(),
                          entry=float(price), sl=float(sl),
                          tp=float(tp) if tp else None, route=route,
                          timeframe=timeframe, session=session)

    def closed(self, trade: dict) -> Optional[dict]:
        """
        Zcastor's position is closed. Exit price only: P&L would reveal the
        size of the operator's account, which the record layer never does.
        """
        return self._emit("close", signal_id=str(trade.get("signal_id", "")),
                          exit=float(trade.get("close_price") or 0.0))

    def online(self) -> Optional[dict]:
        """
        Posted at engine startup. Followers trade nothing on it; it gives a
        new follower a signed event to check its setup against (--check)
        before the first real fill, and marks restarts in the journal.
        """
        return self._emit("online")

    def stats(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "signer": self.signer,
                "seq": getattr(self, "_seq", 0), "errors": self.errors}
