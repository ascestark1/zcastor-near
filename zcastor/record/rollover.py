"""
Session rollover.

At UTC midnight the day's decision log is published and anchored, and a fresh
one starts. Until this ran, `/decisions` was memory only — restart the process
and every refusal was gone. Under the thesis that is the worst possible thing to
lose, because refusals are most of what the system does.

Why the whole day in one record rather than one per decision: a system built to
decline would write thousands of records to say no. The log is complete either
way — every verdict, with its reason and the policy in force — but the audit is
one lookup per session instead of one per rejection.

Two properties this has to hold:

**Crash safety.** The log is published to disk first, then enqueued. If the
process dies between the two, the artifact exists and the outbox dedupe key
means re-anchoring it later is a no-op rather than a duplicate.

**No silent gaps.** A day with zero decisions still publishes. An empty log is
a claim — "nothing was signalled" — and a missing file is ambiguous between that
and a system that was down.

**Each version is a superset of the last.** A restart mid-day resumes the day
from what was already published, so the next close contains every decision
made that day, not only the ones since the restart. Until 2 Oct 2026 it did
not: a restart began an empty log, and the next close published the partial
day and marked the earlier log Superseded. The chain said "replaced" and the
replacement did not contain what it replaced.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .decision_log import DecisionLog

logger = logging.getLogger("zcastor.record.rollover")


def utc_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


class SessionRollover:
    """Owns the current decision log and swaps it at the day boundary."""

    def __init__(
        self,
        *,
        publisher: Any,
        outbox: Any = None,
        anchor_index: Any = None,
        policy_version: str = "",
        process_hash: str = "",
        registry: str = "afritensor-decisions",
        registry_id: int = 0,
        venue: str = "",
        agent: str = "execution",
        clock: Any = None,
    ) -> None:
        self.publisher = publisher
        self.outbox = outbox
        self.anchor_index = anchor_index
        self.policy_version = policy_version
        self.process_hash = process_hash
        self.registry = registry
        self.registry_id = int(registry_id)
        self.agent = agent
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self.day = utc_day(self._clock())
        self.venue = venue
        self.log = DecisionLog(self.day, agent=agent, venue=venue)
        # Checksums of earlier logs for this day whose decisions were folded
        # into this one, and so are replaced by the next publication.
        self._merged: list[str] = []
        self._resume()

    def _resume(self) -> None:
        """Fold today's already-published logs into the in-memory one."""
        published = self.publisher.manifest().get("decisions", {}).get(self.day, [])
        seen: set[str] = set()
        resumed: list[dict[str, Any]] = []
        for item in published:
            path = self.publisher.root / str(item.get("path", ""))
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("earlier log %s unreadable — not resumed", path)
                continue
            if (doc.get("venue") or "") != (self.venue or ""):
                # Another venue's day is its own record, not part of this one.
                continue
            for decision in doc.get("decisions", []):
                key = json.dumps(decision, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    resumed.append(decision)
            self._merged.append(str(item.get("checksum", "")))

        if not self._merged:
            return
        resumed.sort(key=lambda d: str(d.get("at", "")))
        self.log.entries = resumed + self.log.entries
        logger.info("resumed %s: %d decisions from %d earlier log(s)",
                    self.day, len(resumed), len(self._merged))

    def _previous_checksum(self) -> str:
        """
        The checksum of the last session published before this one.

        Read from the publisher's manifest rather than held in memory, so a
        restart mid-week does not break the chain.
        """
        manifest = self.publisher.manifest().get("decisions", {})
        earlier = sorted(day for day in manifest if day < self.day)
        if not earlier:
            return ""
        entries = manifest[earlier[-1]]
        return entries[-1]["checksum"] if entries else ""

    def _supersede(self, previous_checksum: Optional[str]) -> None:
        """Mark the record this publication replaces as Superseded."""
        if not (previous_checksum and self.anchor_index and self.outbox):
            return
        entry = self.anchor_index.get_by_checksum(previous_checksum)
        if entry is None or entry.get("status") != "Active":
            return
        self.outbox.enqueue(
            op="update_status",
            registry=entry["registry"],
            registry_id=int(entry.get("registry_id") or 0),
            agent=self.agent,
            record_id=int(entry.get("record_id") or 0),
            status="Superseded",
            metadata={"kind": "status", "session_date": self.day,
                      "reason": "republished",
                      # The index key of the record being superseded. Decision
                      # logs have no signal id, so they are keyed by checksum —
                      # without this the status hook cannot find what landed.
                      "signal_id": entry.get("signal_id", "")},
        )
        # Queued, not done. The index moves when the chain confirms it —
        # see engine.on_anchored.
        logger.info("queued a supersede for the previous %s record", self.day)

    # ── the boundary ──────────────────────────────────────────────────────────

    def current_day(self) -> str:
        return utc_day(self._clock())

    def due(self) -> bool:
        return self.current_day() != self.day

    def tick(self) -> Optional[dict]:
        """Call on a timer. Rolls over only when the UTC date has changed."""
        if not self.due():
            return None
        return self.close(start_new=True)

    def close(self, *, start_new: bool = True) -> dict:
        """
        Publish and anchor the current log, then optionally begin the next.

        Also the shutdown path: closing cleanly means a restart mid-day does not
        strand the decisions made before it.
        """
        built = self.log.build(policy_version=self.policy_version,
                               process=self.process_hash,
                               previous=self._previous_checksum())
        published = self.publisher.publish_decision_log(built)

        # Republishing a session produces a NEW immutable file, so the earlier
        # record must be marked Superseded. Leaving two Active records for one
        # day would make it impossible for a reader to tell which is current.
        # Every log folded in at resume is replaced too, since this one now
        # contains its decisions — but never the record just published, which
        # is what an unchanged day republishes.
        replaced = [*self._merged, published.get("supersedes")]
        for previous in dict.fromkeys(c for c in replaced if c):
            if previous != published["checksum"]:
                self._supersede(previous)
        self._merged = []

        entry = None
        if self.outbox is not None:
            entry = self.outbox.enqueue(
                op="add_record",
                registry=self.registry,
                registry_id=self.registry_id,
                agent=self.agent,
                uri=published["uri"],
                checksum=published["checksum"],
                status="Active",
                created_at=self.day,
                metadata={
                    "kind": "decision_log",
                    "session_date": self.day,
                    "supersedes": published.get("supersedes") or "",
                    "totals": built["totals"],
                    "policy": self.process_hash,
                },
            )

        logger.info("session %s closed — %s decisions, checksum %s",
                    self.day, len(built["decisions"]), published["checksum"][:19])

        result = {"day": self.day, "decisions": len(built["decisions"]),
                  "totals": built["totals"], "checksum": published["checksum"],
                  "uri": published["uri"],
                  # None when the bytes are unchanged, so a caller can tell a
                  # genuine republication from a no-op.
                  "supersedes": published.get("supersedes"),
                  "queued": entry is not None}

        # A forced close mid-day (POST /rollover) is still the same day: keep
        # accumulating, or the next close would drop what was just published.
        if start_new and self.current_day() != self.day:
            self.day = self.current_day()
            self.log = DecisionLog(self.day, agent=self.agent, venue=self.venue)

        return result
