"""
What landed where.

The outbox knows what still needs sending; nothing knew what had already been
sent. That gap blocks the status lifecycle: to move a dossier from `committed` to
`resolved` you need the record ID the chain gave back, and by the time the
outcome arrives — hours later, possibly after a restart — that value is gone.

So: an append-only index of anchored records, keyed by signal ID.

This is also what makes the ordering guarantee demonstrable rather than merely
designed. A record anchored `committed` at decision time and moved to `resolved`
afterwards proves the prediction preceded the outcome, and the chain versions the
change instead of rewriting it. Without the index, every dossier would sit at
`committed` forever and the distinction would be invisible.

Append-only for the same reason as the outbox and the journal: a rewritten file
is one bad shutdown away from losing the lot.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("zcastor.anchor.index")


class AnchorIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._by_signal: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load()

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
            signal_id = row.get("signal_id")
            if not signal_id:
                continue
            # Later lines supersede earlier ones — that is how a status change
            # updates an entry without the file being rewritten.
            existing = self._by_signal.get(signal_id, {})
            existing.update(row)
            self._by_signal[signal_id] = existing
        logger.info("anchor index loaded %d records", len(self._by_signal))

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    # ── writes ────────────────────────────────────────────────────────────────

    def record_anchored(self, *, signal_id: str, registry: str, record_id: int,
                        checksum: str, transaction: str, status: str,
                        registry_id: int = 0, index: int = 0, uri: str = "",
                        kind: str = "dossier", environment: str = "") -> None:
        # Decision logs have no signal id; key them by checksum so they are
        # still addressable for status updates.
        signal_id = signal_id or f"checksum:{checksum.replace('sha256:', '')[:16]}"
        row = {"signal_id": signal_id, "registry": registry,
               "registry_id": int(registry_id),
               "record_id": record_id, "index": index, "checksum": checksum,
               "transaction": transaction, "status": status,
               "uri": uri, "kind": kind,
               # Which chain this landed on. A testnet record and a mainnet
               # record are different claims, and an index that cannot tell
               # them apart will eventually present one as the other.
               "environment": environment}
        with self._lock:
            existing = self._by_signal.get(signal_id, {})
            existing.update(row)
            self._by_signal[signal_id] = existing
        self._append(row)

    def record_status(self, signal_id: str, status: str,
                      transaction: str = "") -> bool:
        """
        Move a record's status. Returns False when the record is unknown.

        Called only once the chain has confirmed the write — the index is a map
        of what the chain holds, not of what we intend.
        """
        with self._lock:
            entry = self._by_signal.get(signal_id)
            if entry is None:
                return False
            entry["status"] = status
            if transaction:
                entry["status_transaction"] = transaction
        self._append({"signal_id": signal_id, "status": status,
                      "status_transaction": transaction})
        return True

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, signal_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            entry = self._by_signal.get(signal_id)
            return dict(entry) if entry else None

    def get_by_checksum(self, checksum: str) -> Optional[dict[str, Any]]:
        """
        Look up by content hash rather than signal id.

        Decision logs are session-scoped and have no signal id, so the checksum
        is the only handle on them.
        """
        bare = checksum.replace("sha256:", "")
        with self._lock:
            for entry in self._by_signal.values():
                if entry.get("checksum", "").replace("sha256:", "") == bare:
                    return dict(entry)
        return None

    def active(self) -> list[dict[str, Any]]:
        """Anchored and not yet superseded — the outstanding lifecycle work."""
        with self._lock:
            return [dict(e) for e in self._by_signal.values()
                    if e.get("status") == "Active"]

    def stats(self) -> dict[str, int]:
        with self._lock:
            entries = list(self._by_signal.values())
        counts: dict[str, int] = {}
        for e in entries:
            status = str(e.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {"total": len(entries), **dict(sorted(counts.items()))}
