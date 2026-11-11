"""
Durable outbox.

The invariant: **a record that has been handed to the outbox is never lost**, and
nothing in the trade loop ever waits on a chain.

MANTRA halted on 20 August and NVNM inherits settlement from it. A naive
fire-and-forget anchor would have dropped that day's records with a logged
warning and no way to recover them. Here they sit on disk until the chain comes
back.

Design
------
Append-only log. Enqueue appends one line; a successful send appends another.
Nothing is ever rewritten in place, so a crash mid-write can corrupt at most the
final line, and a truncated final line is discarded on load rather than aborting
the whole file. Rewriting a JSON file in place is the standard way to lose an
entire queue to one bad shutdown.

State is rebuilt by replaying the log. `compact()` rewrites it via a temp file
and an atomic rename once the completed entries outnumber the pending ones.

Idempotency is by dedupe key, so a restart cannot double-anchor a dossier —
which on a public ledger is not a cosmetic problem: two records for one decision
invite exactly the question the whole system exists to avoid.

Failure handling
----------------
`AnchorUnavailable` → exponential backoff, entry stays pending, forever if need
be. `AnchorRejected` or an unexpected exception → the entry is parked. Parked
entries are visible, counted, and never retried automatically, because silently
retrying a permanently-bad record hides the fault.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .base import (
    AnchorBackend,
    AnchorRejected,
    AnchorResult,
    AnchorUnavailable,
    PendingRecord,
)

logger = logging.getLogger("zcastor.anchor.outbox")

MAX_BACKOFF_SECONDS = 900.0
BASE_BACKOFF_SECONDS = 5.0


class Outbox:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 0,          # 0 = retry transient failures forever
        on_anchored: Optional[Callable[[PendingRecord, AnchorResult], None]] = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self.max_attempts = max_attempts
        # Called after a successful send, so the caller can remember what
        # landed. Failure here must not un-anchor the record.
        self.on_anchored = on_anchored
        self._pending: dict[str, PendingRecord] = {}
        self._done: set[str] = set()          # dedupe keys already anchored
        self._parked: dict[str, PendingRecord] = {}
        self._completed_lines = 0
        self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _append(self, obj: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Only the final line can be torn by a crash mid-append.
                logger.warning("discarding unparseable outbox line")
                continue
            self._replay(obj)

    def _replay(self, obj: dict[str, Any]) -> None:
        kind = obj.get("_")
        if kind == "enqueue":
            entry = PendingRecord.from_dict(obj["entry"])
            if entry.dedupe_key() not in self._done:
                self._pending[entry.id] = entry
        elif kind == "sent":
            entry_id = obj.get("id", "")
            entry = self._pending.pop(entry_id, None)
            if entry is not None:
                self._done.add(entry.dedupe_key())
            elif obj.get("dedupe"):
                self._done.add(obj["dedupe"])
            self._completed_lines += 1
        elif kind == "parked":
            entry_id = obj.get("id", "")
            entry = self._pending.pop(entry_id, None)
            if entry is not None:
                entry.last_error = obj.get("error", "")
                self._parked[entry_id] = entry
            self._completed_lines += 1

    # ── queue ─────────────────────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        op: str,
        registry: str,
        registry_id: int = 0,
        agent: str = "execution",
        uri: str = "",
        checksum: str = "",
        metadata: Optional[dict[str, Any]] = None,
        status: str = "Active",
        record_id: int = 0,
        index: int = 0,
        created_at: str = "",
    ) -> Optional[PendingRecord]:
        """
        Add work. Returns None if this exact unit is already queued or anchored.

        One synchronous file append — microseconds, and the only cost the trade
        loop ever pays for the accountability layer.
        """
        entry = PendingRecord(
            id=uuid.uuid4().hex,
            op=op,
            registry=registry,
            registry_id=int(registry_id),
            agent=agent,
            uri=uri,
            checksum=_bare(checksum),
            metadata=dict(metadata or {}),
            status=status,
            record_id=record_id,
            index=int(index),
            created_at=created_at,
        )

        key = entry.dedupe_key()
        if key in self._done:
            logger.debug("already anchored, skipping: %s", key)
            return None
        if any(e.dedupe_key() == key for e in self._pending.values()):
            logger.debug("already queued, skipping: %s", key)
            return None
        # Parked work is outstanding, not finished. Without this a failing
        # status update is re-enqueued on every cycle and the outbox grows
        # without bound — eleven copies of one update, in our case.
        if any(e.dedupe_key() == key for e in self._parked.values()):
            logger.debug("already parked, skipping: %s", key)
            return None

        self._append({"_": "enqueue", "entry": entry.to_dict()})
        self._pending[entry.id] = entry
        return entry

    # ── drain ─────────────────────────────────────────────────────────────────

    def drain(self, backend: AnchorBackend, *, limit: int = 50) -> dict[str, int]:
        """
        Try to send pending work. Never raises — a broken chain must not take
        down the process that is still trading.
        """
        now = self._clock()
        sent = failed = skipped = 0

        for entry in list(self._pending.values())[:limit]:
            if entry.next_attempt_at > now:
                skipped += 1
                continue

            # A requeue can reintroduce an entry whose work already landed —
            # the enqueue path checks this, but requeue_parked bypassed it and
            # produced two on-chain records for one document.
            if entry.dedupe_key() in self._done:
                logger.info("dropping already-anchored entry %s",
                            entry.dedupe_key())
                self._pending.pop(entry.id, None)
                self._append({"_": "sent", "id": entry.id,
                              "dedupe": entry.dedupe_key(),
                              "tx": "duplicate", "record_id": 0,
                              "at": self._clock()})
                skipped += 1
                continue
            try:
                result = backend.submit(entry)
            except AnchorUnavailable as exc:
                self._backoff(entry, str(exc))
                failed += 1
            except (AnchorRejected, Exception) as exc:  # noqa: BLE001
                self._park(entry, f"{type(exc).__name__}: {exc}")
                failed += 1
            else:
                self._complete(entry, result)
                sent += 1

        return {"sent": sent, "failed": failed, "skipped": skipped,
                "pending": len(self._pending), "parked": len(self._parked)}

    def _complete(self, entry: PendingRecord, result: AnchorResult) -> None:
        self._append({
            "_": "sent", "id": entry.id, "dedupe": entry.dedupe_key(),
            "tx": result.transaction, "record_id": result.record_id,
            "at": self._clock(),
        })
        self._pending.pop(entry.id, None)
        self._done.add(entry.dedupe_key())
        self._completed_lines += 1
        logger.info("anchored %s → %s", entry.dedupe_key(), result.transaction)

        if self.on_anchored:
            try:
                self.on_anchored(entry, result)
            except Exception:  # noqa: BLE001
                logger.exception("on_anchored hook failed for %s", entry.id)

    def _backoff(self, entry: PendingRecord, error: str) -> None:
        entry.attempts += 1
        entry.last_error = error
        delay = min(BASE_BACKOFF_SECONDS * (2 ** (entry.attempts - 1)),
                    MAX_BACKOFF_SECONDS)
        entry.next_attempt_at = self._clock() + delay
        if self.max_attempts and entry.attempts >= self.max_attempts:
            self._park(entry, f"gave up after {entry.attempts} attempts: {error}")
        else:
            logger.warning("anchor unavailable (attempt %d, retry in %.0fs): %s",
                           entry.attempts, delay, error)

    def _park(self, entry: PendingRecord, error: str) -> None:
        self._append({"_": "parked", "id": entry.id, "error": error,
                      "at": self._clock()})
        self._pending.pop(entry.id, None)
        entry.last_error = error
        self._parked[entry.id] = entry
        logger.error("anchor parked %s: %s", entry.dedupe_key(), error)

    # ── introspection ─────────────────────────────────────────────────────────

    @property
    def pending(self) -> list[PendingRecord]:
        return list(self._pending.values())

    @property
    def parked(self) -> list[PendingRecord]:
        return list(self._parked.values())

    def stats(self) -> dict[str, int]:
        return {"pending": len(self._pending), "parked": len(self._parked),
                "anchored": len(self._done)}

    def mark_done(self, entry: PendingRecord, reason: str = "already_on_chain") -> bool:
        """
        Retire a parked entry whose work the chain already holds.

        Not a delete. The entry is recorded as complete so the dedupe key is
        remembered and a later requeue cannot resurrect it — which is how five
        records ended up parked while the log showed those exact updates
        landing.
        """
        if entry.id not in self._parked:
            return False
        self._parked.pop(entry.id, None)
        self._done.add(entry.dedupe_key())
        self._append({"_": "sent", "id": entry.id,
                      "dedupe": entry.dedupe_key(), "tx": reason,
                      "record_id": entry.record_id, "at": self._clock()})
        logger.info("retired %s — %s", entry.dedupe_key(), reason)
        return True

    def requeue_parked(self) -> int:
        """
        Deliberate operator action after a fault is understood.

        Skips anything whose work already landed. Requeuing an anchored entry
        writes a second record for one document, which is precisely the
        ambiguity the index exists to prevent.
        """
        n = 0
        for entry in list(self._parked.values()):
            if entry.dedupe_key() in self._done:
                logger.info("not requeuing %s — already anchored",
                            entry.dedupe_key())
                self._parked.pop(entry.id, None)
                continue
            entry.attempts = 0
            entry.next_attempt_at = 0.0
            self._append({"_": "enqueue", "entry": entry.to_dict()})
            self._pending[entry.id] = entry
            self._parked.pop(entry.id, None)
            n += 1
        return n

    def compact(self) -> None:
        """Rewrite the log with only live state, via an atomic rename."""
        lines = [{"_": "enqueue", "entry": e.to_dict()} for e in self._pending.values()]
        lines += [{"_": "sent", "id": f"compacted:{k}", "dedupe": k, "at": 0}
                  for k in sorted(self._done)]
        lines += [{"_": "parked", "id": e.id, "error": e.last_error, "at": 0}
                  for e in self._parked.values()]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for obj in lines:
                    fh.write(json.dumps(obj, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._completed_lines = 0

    def maybe_compact(self, threshold: int = 500) -> bool:
        if self._completed_lines >= threshold:
            self.compact()
            return True
        return False


def _bare(digest: str) -> str:
    return digest[7:] if digest.startswith("sha256:") else digest
