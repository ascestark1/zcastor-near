"""
DGML dossier emitter.

One dossier per consequential decision. It is the artifact published at the
`uri` of an NVNM record, and its checksum is that record's `checksum` field.

VOCABULARY WARNING — READ BEFORE ANCHORING
------------------------------------------
The block and element names below are reconstructed from the v2 architecture
work, NOT read from the existing Afritensor dossier corpus. That corpus lives in
the dossier repo and was written in July, and I have not seen it. Before a single
dossier is anchored, diff this structure against a published one and reconcile —
two vocabularies for the same claim is worse than either alone, and a
counterparty comparing an old dossier to a new one should not find the same fact
under two different names.

Design decisions
----------------
`schema` and `schema_version` are the first elements. A record whose format
cannot be identified is not auditable in five years.

The dossier contains NO account balance, no credentials, and no raw tick stream.
Those are inputs to the state hash, which proves the conditions held without
disclosing them. This matches NVNM's own line — the underlying data stays in your
systems, only the proof goes out — while the decision itself is deliberately
published, because public verifiability is the point of this use case.

Provenance starts empty. The anchoring transaction does not exist when the
dossier is written, and a dossier cannot contain the hash of the transaction that
anchors it. The chain's own record versioning closes that loop: anchor with
status `committed`, then `updateRecordStatus` to `resolved` once the outcome
lands. Nothing is rewritten.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .canonical import checksum
from .proof import ProofSet

SCHEMA = "afritensor/dgml-trade-dossier"
SCHEMA_VERSION = "2.0.0-draft"


def build_dossier(
    *,
    signal_id: str,
    emitted_at: str,
    agent: str,
    proofs: ProofSet,
    decision: Mapping[str, Any],
    thesis: Mapping[str, Any],
    execution: Optional[Mapping[str, Any]] = None,
    outcome: Optional[Mapping[str, Any]] = None,
    policy_version: str = "",
    dashboard_version: str = "",
    code_version: str = "unknown",
    notes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Assemble a dossier. Pure — no I/O, no clock, no chain.

    `thesis` is the falsifiable claim made BEFORE the outcome was known:
    direction, target, stop, horizon. `outcome` is filled in later, and its
    absence is meaningful rather than missing — an unresolved dossier says so.
    """
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,

        "identity": {
            "signal_id": signal_id,
            "emitted_at": emitted_at,
            "agent": agent,
        },

        "proof": {
            "origin": proofs.origin,
            "process": proofs.process,
            "state": proofs.state,
        },

        "versions": {
            "policy": policy_version,
            "dashboard": dashboard_version,
            "code": code_version,
        },

        # The claim, made before the outcome existed.
        "thesis": dict(thesis),

        # Every gate verdict, in order — including the ones that objected but
        # did not bind. This is the refusal record.
        "decision": dict(decision),

        "execution": dict(execution) if execution else None,
        "outcome": dict(outcome) if outcome else None,

        # Filled by the anchor layer after the record lands.
        "provenance": {
            "chain": "nvnm",
            "registry": "",
            "record_id": "",
            "transaction": "",
            "status": "unanchored",
        },

        "notes": list(notes or []),
    }


def thesis_from_trace(trace, derived: Mapping[str, Any]) -> dict[str, Any]:
    """
    The falsifiable claim, extracted from a DecisionTrace and its derived values.

    Deliberately narrow: direction, entry, target, stop, R:R, horizon. If it
    cannot be checked against a later price, it does not belong here.
    """
    return {
        "direction": derived.get("direction", ""),
        "timeframe": trace.signal_id and derived.get("timeframe", "") or "",
        "entry_price": derived.get("execution_price"),
        "target": derived.get("tp"),
        "stop": derived.get("sl"),
        "target_pts": _rounded(derived.get("tp_pts")),
        "stop_pts": _rounded(derived.get("sl_pts")),
        "risk_reward": _ratio(derived.get("tp_pts"), derived.get("sl_pts")),
        "action": trace.action.value,
        "route_kind": trace.route_kind or None,
    }


def decision_from_trace(trace) -> dict[str, Any]:
    d = trace.to_dict()
    binding = trace.binding()
    return {
        "action": d["action"],
        "reason": d["reason"],
        "bound_at": binding.name if binding else None,
        "also_objected": [r.name for r in trace.shadow_objections()],
        "gates": d["gates"],
    }


def dossier_checksum(dossier: Mapping[str, Any]) -> str:
    """
    Checksum EXCLUDING the provenance block.

    Provenance is written after anchoring, so including it would mean the
    checksum changes the moment the record lands — and the anchored value would
    no longer match the published file. The stable identity of a dossier is
    everything except where it ended up.
    """
    stable = {k: v for k, v in dossier.items() if k != "provenance"}
    return checksum(stable)


def _rounded(value):
    return round(float(value)) if value is not None else None


def _ratio(tp, sl):
    if not tp or not sl:
        return None
    return round(float(tp) / float(sl), 2)
