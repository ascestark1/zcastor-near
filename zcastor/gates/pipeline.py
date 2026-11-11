"""
The gate pipeline.

Ordering is data, not control flow. The stack is a list; changing the order is
editing a list, not untangling nested `if not suppressed` blocks. Every rule
about when a gate runs lives here, once.

Semantics
---------
REJECT    stops the run immediately. The input was unusable; there is nothing
          further to evaluate and nothing to record.
SUPPRESS  binds the outcome, but the run continues so the record can say what
          else objected. Expensive gates are skipped once bound.
ROUTE     sets routing. Gates with runs_when_routed=False are skipped from that
          point on. A later SUPPRESS still binds and overrides the route — a
          routed signal that fails a gate which legitimately applies to it does
          not get armed.
ALLOW     the default outcome if nothing binds.

Advisory gates never bind, whatever they return.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from .base import (
    Decision,
    DecisionTrace,
    Gate,
    GateContext,
    GateResult,
    Status,
    Verdict,
)

logger = logging.getLogger("zcastor.gates")


class GatePipeline:
    def __init__(self, gates: Sequence[Gate]) -> None:
        self._validate(gates)
        self.gates: tuple[Gate, ...] = tuple(gates)

    @staticmethod
    def _validate(gates: Iterable[Gate]) -> None:
        seen: set[str] = set()
        for g in gates:
            if not isinstance(g, Gate):
                raise TypeError(f"{g!r} is not a Gate")
            if g.name in seen:
                raise ValueError(f"duplicate gate name {g.name!r}")
            if g.name == "unnamed":
                raise ValueError(f"{type(g).__name__} must set a name")
            seen.add(g.name)

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: GateContext, signal_id: str, policy_hash: str) -> DecisionTrace:
        trace = DecisionTrace(
            signal_id=signal_id,
            origin_hash=ctx.origin_hash,
            policy_hash=policy_hash,
        )

        routed = False
        bound: Optional[Decision] = None

        for gate in self.gates:
            if routed and not gate.runs_when_routed:
                trace.results.append(GateResult(
                    gate.name, Status.SKIPPED, Verdict.ALLOW,
                    reason="skipped: routed to watcher",
                ))
                continue

            if bound is not None and gate.expensive:
                trace.results.append(GateResult(
                    gate.name, Status.SKIPPED, Verdict.ALLOW,
                    reason=f"skipped: already suppressed ({bound.reason})",
                ))
                continue

            decision = self._evaluate(gate, ctx)

            # Advisory gates are recorded and cannot change anything.
            if gate.advisory:
                trace.results.append(GateResult(
                    gate.name, Status.ADVISORY, decision.verdict,
                    decision.reason, decision.detail,
                ))
                continue

            if decision.verdict is Verdict.REJECT:
                trace.results.append(GateResult(
                    gate.name, Status.BINDING, Verdict.REJECT,
                    decision.reason, decision.detail,
                ))
                trace.action = Verdict.REJECT
                trace.reason = decision.reason
                logger.info("REJECT at %s — %s", gate.name, decision.reason)
                return trace

            if decision.verdict is Verdict.SUPPRESS:
                status = Status.BINDING if bound is None else Status.SHADOW
                if bound is None:
                    bound = decision
                    routed = False          # a bind cancels any routing
                    trace.action = Verdict.SUPPRESS
                    trace.reason = decision.reason
                    trace.route_kind = ""
                trace.results.append(GateResult(
                    gate.name, status, Verdict.SUPPRESS,
                    decision.reason, decision.detail,
                ))
                continue

            if decision.verdict is Verdict.ROUTE:
                if bound is not None:
                    # Already suppressed; routing no longer applies.
                    trace.results.append(GateResult(
                        gate.name, Status.SHADOW, Verdict.ROUTE,
                        decision.reason, decision.detail,
                    ))
                    continue
                routed = True
                trace.action = Verdict.ROUTE
                trace.reason = decision.reason
                trace.route_kind = decision.route_kind
                trace.results.append(GateResult(
                    gate.name, Status.BINDING, Verdict.ROUTE,
                    decision.reason, decision.detail,
                ))
                continue

            trace.results.append(GateResult(
                gate.name, Status.PASSED, Verdict.ALLOW,
                detail=decision.detail,
            ))

        self._log(trace)
        return trace

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _evaluate(gate: Gate, ctx: GateContext) -> Decision:
        """
        A gate that raises must not take the process down, and must not be read
        as consent. Fail closed: an erroring gate suppresses.
        """
        try:
            decision = gate.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            logger.exception("gate %s raised", gate.name)
            return Decision(
                Verdict.SUPPRESS,
                reason=f"gate_error:{gate.name}",
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
        if not isinstance(decision, Decision):
            logger.error("gate %s returned %r, not a Decision", gate.name, decision)
            return Decision(Verdict.SUPPRESS, reason=f"gate_contract:{gate.name}")
        return decision

    @staticmethod
    def _log(trace: DecisionTrace) -> None:
        binding = trace.binding()
        shadows = [r.name for r in trace.shadow_objections()]
        logger.info(
            "%s — action=%s reason=%s%s%s",
            trace.signal_id,
            trace.action.value,
            trace.reason or "-",
            f" at={binding.name}" if binding else "",
            f" also_objected={','.join(shadows)}" if shadows else "",
        )
