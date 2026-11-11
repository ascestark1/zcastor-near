"""
Daily decision log — the refusal record.

The system declines most of what it sees. Under the accountability thesis that is
the interesting behaviour, not the boring part: anyone can show you their fills.
Showing every signal you refused, with the reason and the policy in force, is the
claim that is actually hard to fake.

Why a daily digest rather than one record per refusal: volume. Anchoring is cheap
but not free, and a system built to say no would write thousands of records to
say it. One dossier per session, listing every decision in order, keeps the full
detail and makes the audit one lookup instead of a thousand. Revisit if a
counterparty ever asks for finer grain — the entries are per-decision either way,
only the anchoring is batched.

Executed trades still get their own dossier. This log is the complete decision
set including them, so the two views reconcile: every entry here with
`action: allow` has a matching trade dossier.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .canonical import checksum

SCHEMA = "afritensor/dgml-decision-log"
SCHEMA_VERSION = "2.0.0-draft"


class DecisionLog:
    """Accumulates a session's decisions. In memory; the caller persists it."""

    def __init__(self, session_date: str, agent: str = "execution",
                 venue: str = "") -> None:
        self.session_date = session_date
        self.agent = agent
        # Where these decisions executed. A session log that cannot say which
        # venue it describes cannot be checked against that venue's records.
        self.venue = venue
        self.entries: list[dict[str, Any]] = []

    def add(
        self,
        *,
        signal_id: str,
        at: str,
        trace,
        origin: str,
        process: str,
        state: str,
        dossier_checksum: Optional[str] = None,
    ) -> None:
        """
        Record one decision.

        REJECTs are not logged. A malformed payload is not a decision the system
        made, and padding the refusal record with dropped inputs would inflate
        the count of "signals declined" — which is precisely the number a reader
        would take as meaningful.
        """
        if not trace.recordable:
            return

        binding = trace.binding()
        self.entries.append({
            "signal_id": signal_id,
            "at": at,
            "action": trace.action.value,
            "reason": trace.reason,
            "bound_at": binding.name if binding else None,
            "also_objected": [r.name for r in trace.shadow_objections()],
            # Would have refused, but running advisory: the shadow record a
            # gate needs before it is trusted to bind.
            "advisory_objected": [r.name for r in trace.advisory_objections()],
            "route_kind": trace.route_kind or None,
            "proof": {"origin": origin, "process": process, "state": state},
            "dossier": dossier_checksum,
        })

    # ── views ─────────────────────────────────────────────────────────────────

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            counts[e["action"]] = counts.get(e["action"], 0) + 1
        return dict(sorted(counts.items()))

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            if e["action"] == "suppress" and e["reason"]:
                counts[e["reason"]] = counts.get(e["reason"], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def ranked_reasons(self) -> list[dict[str, Any]]:
        """
        Refusal reasons, most frequent first, as a LIST.

        It has to be a list. Canonical serialisation sorts object keys — it must,
        or the checksum would depend on insertion order — so a dict here would be
        rewritten alphabetically on publication and the ranking would silently
        vanish from the artifact. A JSON array keeps its order and stays
        canonical.
        """
        return [{"reason": reason, "count": count}
                for reason, count in self.reasons().items()]

    def build(self, *, policy_version: str = "", process: str = "",
              previous: str = "") -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "session_date": self.session_date,
            "agent": self.agent,
            "venue": self.venue,
            "policy_version": policy_version,
            "process": process,
            # The checksum of the previous session's log.
            #
            # Without this each record is individually verifiable but the
            # SERIES is not: an omitted day is invisible, so publishing only
            # the good sessions would pass every check. Chained, a reader walks
            # backwards from today and a gap stops them. "We publish
            # everything" becomes a property rather than a promise.
            "previous": previous,
            "totals": self.summary(),
            "suppression_reasons": self.ranked_reasons(),
            "decisions": list(self.entries),
        }

    def checksum(self, **kw: Any) -> str:
        return checksum(self.build(**kw))


def merge_totals(logs: list[Mapping[str, Any]]) -> dict[str, int]:
    """Roll several days' logs into one count. Used by reporting, not anchoring."""
    out: dict[str, int] = {}
    for log in logs:
        for action, n in (log.get("totals") or {}).items():
            out[action] = out.get(action, 0) + int(n)
    return dict(sorted(out.items()))
