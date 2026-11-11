"""
Proof of Origin, Process and State.

NVNM's model is that an agent sources data, reasons, and acts, and that each of
those three is separately attestable. Zcastor's mapping:

ORIGIN   — what the agent was told. The dashboard payload exactly as received,
           before any gate touches it. Captured at the door, not reconstructed
           afterwards from the fields that happened to be used.

PROCESS  — how the agent reasons. The policy file plus the code version. Two
           signals with the same origin can legitimately produce different
           decisions if the policy changed between them, so a decision record
           without a process hash is unauditable.

STATE    — what the world looked like. Tick, spread, balance, open positions,
           session open. The market conditions the decision was made under,
           which are not in the payload and are gone a second later.

v1 had all three implicitly and none of them explicitly, which is why Proof of
Origin did not exist: `process_signal` consumed the payload field by field and
never held it as an object.

A note on honesty: `code_version` is a git commit hash supplied by the caller. If
it is unavailable we record `"unknown"` rather than a plausible-looking
placeholder. An unknown provenance element should read as unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import canonical_bytes, checksum, checksum_bytes


@dataclass(frozen=True, slots=True)
class ProofSet:
    origin: str
    process: str
    state: str

    def to_dict(self) -> dict[str, str]:
        return {"origin": self.origin, "process": self.process, "state": self.state}


def origin_hash(payload: Mapping[str, Any]) -> str:
    """
    Hash the payload as received. Do not filter, reorder or coerce first —
    the claim is "this is what the agent was given", and any preprocessing
    makes it a claim about what we chose to keep.
    """
    return checksum(dict(payload))


def process_hash(policy: Mapping[str, Any], code_version: str | None = None) -> str:
    return checksum({
        "policy": dict(policy),
        "code_version": code_version or "unknown",
    })


def process_hash_from_file(policy_path, code_version: str | None = None) -> str:
    """
    Hash the policy file's bytes rather than a parsed copy of it.

    Preferred when the policy file is itself published: the auditor can then
    `sha256sum config/policy.json` directly, exactly as with a dossier.
    """
    return checksum({
        "policy_file": checksum_bytes(policy_path.read_bytes()),
        "code_version": code_version or "unknown",
    })


def state_hash(
    *,
    tick_time: float,
    ask: float,
    bid: float,
    spread_pts: float,
    balance: float | None = None,
    free_margin: float | None = None,
    open_positions: int | None = None,
    session: str = "",
    session_open: float | None = None,
) -> str:
    """
    Hash the market state the decision was made under.

    Keyword-only and explicit: a positional signature here would let two floats
    swap silently and produce a state hash that verifies but describes a
    different world.
    """
    return checksum({
        "tick_time": tick_time,
        "ask": ask,
        "bid": bid,
        "spread_pts": spread_pts,
        "balance": balance,
        "free_margin": free_margin,
        "open_positions": open_positions,
        "session": session,
        "session_open": session_open,
    })


def build(
    *,
    payload: Mapping[str, Any],
    policy: Mapping[str, Any],
    state: Mapping[str, Any],
    code_version: str | None = None,
) -> ProofSet:
    return ProofSet(
        origin=origin_hash(payload),
        process=process_hash(policy, code_version),
        state=state_hash(**state),
    )


def payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """
    The canonical origin bytes.

    Worth publishing alongside the dossier when a counterparty needs to verify
    the origin hash rather than trust it — the underlying market data is not
    sensitive. Account state, by contrast, stays local: the state hash proves
    the conditions without disclosing the balance.
    """
    return canonical_bytes(dict(payload))
