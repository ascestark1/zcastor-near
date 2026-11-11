"""
Anchor backend interface.

Two operations, mirroring the NVNM anchoring module:

  add_record        — anchor a checksummed artifact in a registry
  update_status     — move an existing record's status, e.g. committed → resolved

Backends are interchangeable and the outbox does not know which it has.
`NullBackend` runs the whole pipeline offline; `NvnmBackend` talks to the chain.
When the MCP write path is added it becomes a third backend, not a rewrite —
the outbox stays the invariant and only the drain strategy changes.

A backend raises `AnchorUnavailable` when the failure is transient — RPC down,
chain halted, nonce contention. The outbox keeps the entry and retries. Any other
exception is treated as permanent for that entry: it is parked, not retried
forever, and it is never silently dropped.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class AnchorUnavailable(Exception):
    """Transient failure. Keep the entry and try again later."""


class AnchorRejected(Exception):
    """Permanent failure for this entry. Park it for a human to look at."""


@dataclass(frozen=True, slots=True)
class AnchorResult:
    transaction: str
    record_id: int = 0           # chain-assigned integer, not our checksum
    index: int = 0               # version index within the record


# Status vocabulary. Free-form on chain, but Inveniam's own registries use
# these, and a record whose status means nothing to a reader is worth less than
# one that follows the convention.
STATUS_ACTIVE = "Active"           # anchored, current
STATUS_SUPERSEDED = "Superseded"   # a later record replaced this one
STATUS_REVOKED = "Revoked"         # withdrawn


@dataclass(slots=True)
class PendingRecord:
    """One unit of work in the outbox."""
    id: str
    op: str                      # "add_record" | "update_status"
    # Human name, used for dedupe keys and logs. NOT sent to the chain:
    # registry names are neither unique nor queryable on-chain, so resolving
    # by name would let anyone shadow a registry by creating one with the
    # same name. The numeric id is authoritative.
    registry: str
    registry_id: int = 0
    agent: str = "execution"
    uri: str = ""
    checksum: str = ""           # bare hex, no "sha256:" prefix
    checksum_algo: str = "sha256"
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_ACTIVE
    record_id: int = 0           # chain-assigned; update_status only
    index: int = 0               # version index; update_status only
    created_at: str = ""
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""

    def dedupe_key(self) -> str:
        """
        Identity for idempotency.

        A dossier anchored once must not be anchored again after a restart, and
        two different status updates to the same record must not collapse into
        one — hence status is part of the key for updates.
        """
        if self.op == "update_status":
            return f"update_status|{self.registry}|{self.record_id}|{self.status}"
        return f"add_record|{self.registry}|{self.checksum}"

    def to_dict(self) -> dict[str, Any]:
        """
        Serialise every field, derived from the dataclass itself.

        This was a hand-written literal, and it silently stopped listing
        `registry_id` and `index` when those fields were added. Entries
        persisted, reloaded without them, and failed with "no numeric
        registry_id" — data loss in the one file whose whole purpose is not
        losing anything. Deriving the field list makes that drift impossible.
        """
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class AnchorBackend(ABC):
    name = "backend"

    @abstractmethod
    def add_record(self, entry: PendingRecord) -> AnchorResult:
        ...

    @abstractmethod
    def update_status(self, entry: PendingRecord) -> AnchorResult:
        ...

    def submit(self, entry: PendingRecord) -> AnchorResult:
        if entry.op == "add_record":
            return self.add_record(entry)
        if entry.op == "update_status":
            return self.update_status(entry)
        raise AnchorRejected(f"unknown op {entry.op!r}")


class NullBackend(AnchorBackend):
    """
    Accepts everything, touches nothing. Lets the full pipeline — gates, record
    layer, outbox — run end to end with no chain, no keys and no gas, which is
    how every step before go-live gets tested.
    """
    name = "null"

    def __init__(self) -> None:
        self.submitted: list[PendingRecord] = []

    def add_record(self, entry: PendingRecord) -> AnchorResult:
        self.submitted.append(entry)
        # Stand in for a chain-assigned id so callers exercise the real shape.
        return AnchorResult(transaction=f"null:{entry.id}",
                            record_id=len(self.submitted), index=1)

    def update_status(self, entry: PendingRecord) -> AnchorResult:
        self.submitted.append(entry)
        return AnchorResult(transaction=f"null:{entry.id}", record_id=entry.record_id)
