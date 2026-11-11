"""
Signal snapshot — Proof of Origin at the door.

The payload is captured and hashed the moment it arrives, before any gate reads
a field. This is the piece v1 did not have: `process_signal` consumed the
payload key by key and never held it as an object, so there was nothing to make
a claim about. "This is what the agent was given" cannot be reconstructed later
from the fields that happened to get used.

The rule here is do-not-touch. No filtering, no coercion, no defaults filled in,
no unknown keys dropped. A dashboard build that adds a field must change the
origin hash — that is the point. Normalisation belongs to the gates, which write
their results into `derived` and leave the original alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..record.canonical import canonical_bytes, checksum

# Fields the dashboard is expected to send. Absence is recorded, never filled.
EXPECTED_FIELDS = (
    "timestamp", "timeframe", "session", "regime", "tf_regime",
    "direction", "confidence", "entry_price", "range_low", "range_high",
    "atr", "raw_conf", "zone_status", "zone_count", "zone_phase",
    "zone_flip_pending", "nearest_resistance_pts", "nearest_support_pts",
    "nearest_resistance_price", "nearest_support_price",
    "resistance_strength", "support_strength", "spread_pts",
    "block_direction", "block_confidence",
)


@dataclass(frozen=True, slots=True)
class SignalSnapshot:
    payload: dict[str, Any]
    origin_hash: str
    received_at: str
    dashboard_version: str = ""
    missing_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bytes(self) -> bytes:
        """Canonical origin bytes — publishable beside the dossier."""
        return canonical_bytes(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_hash": self.origin_hash,
            "received_at": self.received_at,
            "dashboard_version": self.dashboard_version,
            "missing_fields": list(self.missing_fields),
        }


def capture(
    payload: Mapping[str, Any],
    *,
    received_at: str,
    expected_dashboard_version: Optional[str] = None,
) -> SignalSnapshot:
    """
    Hash the payload as received.

    `missing_fields` is recorded rather than acted on. A field the dashboard
    stopped sending is a real change worth seeing in the record, but deciding
    what to do about it belongs to the gates.
    """
    frozen = dict(payload)
    declared = str(frozen.get("dashboard_version", "") or "")
    missing = tuple(f for f in EXPECTED_FIELDS if f not in frozen)

    return SignalSnapshot(
        payload=frozen,
        origin_hash=checksum(frozen),
        received_at=received_at,
        dashboard_version=declared,
        missing_fields=missing,
    )


def version_matches(snapshot: SignalSnapshot, expected: str) -> bool:
    """
    Does the payload come from the dashboard build the policy pins?

    A mismatch is not automatically a refusal — an older build may be perfectly
    tradeable — but it must be visible, because a signal's meaning depends on
    the build that produced it and a dossier that cannot say which one is not
    auditable.
    """
    if not expected:
        return True
    return snapshot.dashboard_version == expected
