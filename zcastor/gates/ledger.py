"""
Edge ledger.

Every (timeframe, session, route) segment starts in shadow and must earn the
right to trade: a minimum sample and a positive lower confidence bound on
expectancy. Until then the verdict is recorded but does not bind.

`enforce` is a constructor argument rather than a policy read at evaluate time,
because the pipeline needs to know whether this gate is advisory *before* it runs
it. Flipping enforcement is therefore a deliberate act at wiring time, not a
value that can change under a running process — which matters, because the moment
this gate starts binding is the moment measured evidence starts refusing trades,
and that transition should appear in the policy record.

The verdict is recorded either way. A shadow segment that would have been
refused is exactly the data that tells you whether enforcement is ready.
"""

from __future__ import annotations

from .base import Gate, GateContext, Decision, allow, suppress


class EdgeLedgerGate(Gate):
    name = "edge_ledger"

    def __init__(self, enforce: bool = False) -> None:
        self.enforce = bool(enforce)
        self.advisory = not self.enforce

    def evaluate(self, ctx: GateContext) -> Decision:
        timeframe = str(ctx.signal.get("timeframe", ""))
        session = str(ctx.signal.get("session", ""))
        # The route is known only after the routing gates have run, so this gate
        # must sit after them. v1 guessed it from a flag and could mislabel.
        route = ctx.get("route_kind") and "watcher" or "market"

        verdict = ctx.ledger.verdict(timeframe, session, route)
        action = str(verdict.get("action", "")).lower()

        detail = {
            "segment": f"{timeframe}/{session}/{route}",
            "action": action,
            "n": int(verdict.get("n", 0)),
            "expectancy": round(float(verdict.get("exp", 0.0)), 2),
            "lower_ci": round(float(verdict.get("lo", 0.0)), 2),
            "why": str(verdict.get("why", "")),
            "enforcing": self.enforce,
        }

        if self.enforce and action == "shadow":
            return suppress("ledger_shadow", **detail)

        return allow(**detail)
