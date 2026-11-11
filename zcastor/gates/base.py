"""
Gate primitives for Zcastor v2.

The single rule this module exists to enforce: a gate is a pure function from
context to verdict. It never mutates a shared suppression flag, and it never
decides whether it should run — the pipeline decides that from the gate's own
declared properties.

That is what makes the v1 route-guard bug inexpressible. In v1, "routed signals
skip the range gates" was a comment plus an `and not route_to_watcher` clause
repeated by hand at each call site; when the block was split into three
statements, two copies of the guard were lost silently. Here, a gate declares
`runs_when_routed = False` once, and the pipeline honours it for every gate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ── Verdicts ──────────────────────────────────────────────────────────────────

class Verdict(str, Enum):
    """
    ALLOW    — this gate has no objection.
    REJECT   — the input is unusable. Terminal. No record is produced, because
               there is no decision to be accountable for.
    SUPPRESS — a decision made under policy: do not send an order. The signal is
               still committed, revealed and resolved, and the refusal becomes a
               record. This is the verdict the accountability thesis is about.
    ROUTE    — hand to the entry watcher for a confirmed zone entry.
    """
    ALLOW = "allow"
    REJECT = "reject"
    SUPPRESS = "suppress"
    ROUTE = "route"


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    # ROUTE only: "fresh" | "exhaustion"
    route_kind: str = ""
    # Free-form gate output for the decision record. Must be JSON-serialisable.
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_allow(self) -> bool:
        return self.verdict is Verdict.ALLOW


def allow(**detail: Any) -> Decision:
    return Decision(Verdict.ALLOW, detail=dict(detail))


def reject(reason: str, **detail: Any) -> Decision:
    return Decision(Verdict.REJECT, reason=reason, detail=dict(detail))


def suppress(reason: str, **detail: Any) -> Decision:
    return Decision(Verdict.SUPPRESS, reason=reason, detail=dict(detail))


def route(kind: str, **detail: Any) -> Decision:
    if kind not in ("fresh", "exhaustion"):
        raise ValueError(f"route kind must be 'fresh' or 'exhaustion', got {kind!r}")
    return Decision(Verdict.ROUTE, reason=f"routed_{kind}", route_kind=kind,
                    detail=dict(detail))


# ── Context ───────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class GateContext:
    """
    Everything a gate may read, and the one place a stage may write.

    `signal` and `origin_hash` are frozen inputs — the exact payload the agent
    saw, and its checksum. `derived` holds values computed by stages during the
    run (execution_price, tp, sl, sl_pts...). A gate reads from `derived` via
    `need()`, which raises if the value is absent rather than silently defaulting
    — in v1 a missing `spread_ratio` produced an UnboundLocalError at runtime,
    and a missing S&R level was read as a float(None) crash.
    """
    signal: dict[str, Any]
    origin_hash: str
    policy: dict[str, Any]
    # Adapters. Typed as Any so this module stays dependency-free.
    market: Any = None
    risk: Any = None
    positions: Any = None
    ledger: Any = None
    targets: Any = None
    derived: dict[str, Any] = field(default_factory=dict)

    def need(self, key: str) -> Any:
        """Read a derived value that must exist by now. Fails loudly if not."""
        if key not in self.derived:
            raise KeyError(
                f"gate requires derived value {key!r}, which no earlier stage "
                f"produced. Check stage ordering in the pipeline."
            )
        return self.derived[key]

    def get(self, key: str, default: Any = None) -> Any:
        """Read an optional derived value."""
        return self.derived.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self.derived[key] = value

    def policy_value(self, key: str, default: Any = None) -> Any:
        return self.policy.get(key, default)


# ── Gates ─────────────────────────────────────────────────────────────────────

class Gate(ABC):
    """
    One decision, one class.

    Declared properties, honoured by the pipeline:

    runs_when_routed — False for gates whose question is meaningless once the
        signal is going to the watcher. The range and session-exhaustion gates
        judge a *market* entry against a *market* stop; a routed signal will be
        entered at a structural level with a structural stop, so applying them
        would refuse the trade for a stop it is never going to use.

    expensive — True for gates that hit the broker or the network. Once a
        binding suppression exists, expensive gates are skipped; cheap ones keep
        evaluating so the decision record can say what *else* would have
        objected.

    advisory — True for gates that observe but never bind. Their verdict is
        recorded; it cannot change the outcome.
    """

    name: str = "unnamed"
    runs_when_routed: bool = True
    expensive: bool = False
    advisory: bool = False

    @abstractmethod
    def evaluate(self, ctx: GateContext) -> Decision:
        ...

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Gate {self.name}>"


class Stage(Gate):
    """
    A pipeline step that computes rather than decides — TP/SL derivation, signal
    ID minting. Returns ALLOW normally, or REJECT when it cannot produce what
    later gates need. Modelled as a Gate so the pipeline has one kind of thing to
    run and the trace records computation failures like any other terminal event.
    """
    advisory: bool = False


# ── Trace ─────────────────────────────────────────────────────────────────────

class Status(str, Enum):
    BINDING = "binding"    # this verdict determined the outcome
    SHADOW = "shadow"      # would have objected, but something bound first
    PASSED = "passed"
    SKIPPED = "skipped"    # not run: routed past, or expensive after a bind
    ADVISORY = "advisory"


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    status: Status
    verdict: Verdict
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DecisionTrace:
    """
    The full record of one signal's journey through the gate stack.

    This object has two consumers and that is the whole point of the design:
    `execution/` reads `action` to decide what to do, and `record/` serialises it
    into a DGML dossier or the daily decision log. One traversal, one artifact.
    """
    signal_id: str
    origin_hash: str
    policy_hash: str
    results: list[GateResult] = field(default_factory=list)
    action: Verdict = Verdict.ALLOW
    reason: str = ""
    route_kind: str = ""

    # ── queries ──
    @property
    def routed(self) -> bool:
        return self.action is Verdict.ROUTE

    @property
    def executable(self) -> bool:
        return self.action is Verdict.ALLOW

    @property
    def recordable(self) -> bool:
        """REJECTs are dropped inputs, not decisions — they produce no record."""
        return self.action is not Verdict.REJECT

    def binding(self) -> Optional[GateResult]:
        for r in self.results:
            if r.status is Status.BINDING:
                return r
        return None

    def shadow_objections(self) -> list[GateResult]:
        return [r for r in self.results if r.status is Status.SHADOW]

    def advisory_objections(self) -> list[GateResult]:
        """
        Gates that objected in advisory mode and were overruled.

        Before this, an advisory gate's judgement was computed and discarded —
        it reached no record, so the edge ledger gate, advisory since day one,
        never logged one of its judgements, and a gate in shadow could never
        accumulate the evidence needed to decide whether to enforce it.
        """
        return [r for r in self.results
                if r.status is Status.ADVISORY and r.verdict is Verdict.SUPPRESS]

    def to_dict(self) -> dict[str, Any]:
        """Canonical-ready form. Key order is fixed; hashing happens upstream."""
        return {
            "signal_id": self.signal_id,
            "origin_hash": self.origin_hash,
            "policy_hash": self.policy_hash,
            "action": self.action.value,
            "reason": self.reason,
            "route_kind": self.route_kind,
            "gates": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "verdict": r.verdict.value,
                    "reason": r.reason,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }
