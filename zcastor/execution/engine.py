"""
The execution engine.

What v1's 773-line `process_signal` became. It is short because it decides
nothing: the gates decide, the record layer serialises, the outbox anchors. This
orchestrates.

    capture origin → run gates → act on the verdict → record → enqueue

Three things live here that deliberately do not live in a gate:

**Zone-flip closing.** When a zone is spent, every open position in that
direction is structurally wrong regardless of timeframe, so they are closed. v1
did this inside the zone gate, which made the gate untestable without a broker
and let a "decision" place orders. It is an action, so it is here.

**The watcher fallback.** Gates always state their judgement — a routed signal
says "this belongs at the zone" — without consulting deployment config. If the
watcher is not running, that is converted to a suppression here, with reason
`watcher_disabled`. The record then says both things truthfully: the gate routed,
the deployment could not honour it.

**Recording, always.** Every recordable decision produces a decision-log entry
and, if it traded, a dossier. This happens whether the order succeeded, failed,
or was never attempted. v1 lost 19 signals to measurement on 10 June because
outcome registration sat inside the `commit_ok` branch — an audit outage blinded
the learning layer. Nothing here is conditional on the chain.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..anchor.base import STATUS_ACTIVE, STATUS_SUPERSEDED
from ..gates.base import DecisionTrace, GateContext, Verdict
from ..market import snapshot as snap
from ..record import dgml, proof

logger = logging.getLogger("zcastor.execution.engine")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Engine:
    def __init__(
        self,
        *,
        pipeline: Any,
        market: Any,
        orders: Any,
        risk: Any,
        targets: Any,
        ledger: Any,
        journal: Any,
        positions: Any,
        policy: dict,
        policy_hash: str,
        decision_log: Any = None,
        outbox: Any = None,
        watcher: Any = None,
        publisher: Any = None,
        environment: str = "",
        resolver: Any = None,
        anchor_index: Any = None,
        code_version: str = "unknown",
        registries: Optional[dict[str, str]] = None,
        registry_ids: Optional[dict[str, int]] = None,
        dossier_uri_template: str = "",
    ) -> None:
        self.pipeline = pipeline
        self.market = market
        self.orders = orders
        self.risk = risk
        self.targets = targets
        self.ledger = ledger
        self.journal = journal
        self.positions = positions
        self.policy = policy
        self.policy_hash = policy_hash
        self.decision_log = decision_log
        self.outbox = outbox
        self.watcher = watcher
        self.publisher = publisher
        self.environment = environment
        self.resolver = resolver
        self.anchor_index = anchor_index
        self.code_version = code_version
        self.registries = registries or {}
        # Numeric ids assigned by the chain at registry creation. Empty until
        # setup_registries.py has run; the outbox then parks records with a
        # clear reason instead of sending an unidentifiable registry.
        self.registry_ids = {k: int(v) for k, v in (registry_ids or {}).items()}
        # Set by the app after construction; optional everywhere else.
        self.on_fill_notify = None
        # Set by the app. Called for every recordable decision, refusals
        # included — see notify/discord.py for why the refusals matter.
        self.on_decision_notify = None
        self.dossier_uri_template = dossier_uri_template

    # ── entry point ───────────────────────────────────────────────────────────

    def process(self, payload: dict) -> DecisionTrace:
        received_at = _now()
        origin = snap.capture(
            payload,
            received_at=received_at,
            expected_dashboard_version=self.policy.get("dashboard_version"),
        )
        signal_id = f"sig_{uuid.uuid4().hex[:12]}"

        self.market.begin()

        ctx = GateContext(
            signal=dict(payload),
            origin_hash=origin.origin_hash,
            policy=self.policy,
            market=self.market,
            risk=self.risk,
            positions=self.positions,
            ledger=self.ledger,
            targets=self.targets,
        )

        trace = self.pipeline.run(ctx, signal_id, self.policy_hash)

        if trace.action is Verdict.REJECT:
            # Unusable input. Nothing to be accountable for, so no record.
            logger.info("%s rejected: %s", signal_id, trace.reason)
            return trace

        # A spent zone invalidates the direction whatever happens next, so this
        # runs before the routing decision is acted on. Putting it inside the
        # ALLOW path meant it never fired at all: an exhausted zone produces
        # ROUTE, so the positions it was meant to close stayed open.
        self._close_exhausted_direction(ctx)

        execution = None
        if trace.action is Verdict.ROUTE:
            execution = self._route(ctx, trace)
        elif trace.action is Verdict.ALLOW:
            execution = self._execute(ctx, trace)

        self._notify_decision(ctx, trace)
        self._register_for_measurement(ctx, trace)
        self._record(ctx, trace, origin, execution, received_at)
        return trace

    # ── acting ────────────────────────────────────────────────────────────────

    def _route(self, ctx: GateContext, trace: DecisionTrace) -> Optional[dict]:
        if self.watcher is None:
            trace.action = Verdict.SUPPRESS
            trace.reason = "watcher_disabled"
            logger.info("%s routed but no watcher is running", trace.signal_id)
            return None
        try:
            armed = self.watcher.arm(
                signal_id=trace.signal_id,
                signal=ctx.signal,
                route_kind=trace.route_kind,
                derived=dict(ctx.derived),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("watcher arm failed")
            trace.action = Verdict.SUPPRESS
            trace.reason = "watcher_error"
            return {"error": str(exc)}
        logger.info("%s armed at zone (%s)", trace.signal_id, trace.route_kind)
        return {"armed": True, "route_kind": trace.route_kind, "detail": armed}

    def _execute(self, ctx: GateContext, trace: DecisionTrace) -> Optional[dict]:
        side = "BUY" if ctx.need("is_buy") else "SELL"
        result = self.orders.market_order(
            side=side,
            price=ctx.need("execution_price"),
            sl=ctx.need("sl"),
            tp=ctx.get("tp"),
        )

        if "error" in result:
            # The decision stands and is recorded; only the fill failed.
            trace.action = Verdict.SUPPRESS
            trace.reason = "order_failed"
            logger.error("%s order failed: %s", trace.signal_id, result["error"])
            return {"error": result["error"]}

        ticket = result["ticket"]
        self.journal.open_trade(
            ticket=ticket,
            signal_id=trace.signal_id,
            direction=ctx.need("direction"),
            timeframe=str(ctx.signal.get("timeframe", "")),
            session=str(ctx.signal.get("session", "")),
            route="market",
            entry_price=result["price"],
            sl=result["sl"],
            tp=result.get("tp") or 0.0,
            volume=self.orders.volume,
        )

        # Zone memory records the entry only after a fill. An intention is not
        # an entry, and recording one would exhaust a zone we never traded.
        self.risk.record_entry(
            ctx.need("mid"), ctx.need("direction").lower(),
            str(ctx.signal.get("timeframe", "1h")),
        )

        if self.on_fill_notify:
            try:
                self.on_fill_notify(
                    signal_id=trace.signal_id, side=side, price=result["price"],
                    sl=result["sl"], tp=result.get("tp"), route="market",
                    timeframe=str(ctx.signal.get("timeframe", "")),
                    session=str(ctx.signal.get("session", "")))
            except Exception:  # noqa: BLE001
                logger.exception("fill notification failed")

        return {"ticket": ticket, "side": side, "price": result["price"],
                "sl": result["sl"], "tp": result.get("tp")}

    def _close_exhausted_direction(self, ctx: GateContext) -> list[int]:
        """
        When a zone is spent, close every open position in that direction.

        Not timeframe-scoped on purpose: a spent zone invalidates the direction,
        and a 5m position sitting inside an exhausted 1h move is wrong for the
        same reason the new signal would have been.
        """
        if ctx.get("zone_status") != "blocked":
            return []

        direction = ctx.need("direction")
        want = "BUY" if direction == "UP" else "SELL"
        closed = []
        for position in self.market.get_open_positions():
            if position.get("side") != want:
                continue
            result = self.orders.close_position(position["ticket"], want)
            if "error" not in result:
                closed.append(position["ticket"])
        if closed:
            logger.info("zone exhausted — closed %s positions in %s: %s",
                        len(closed), direction, closed)
        return closed

    def _notify_decision(self, ctx: GateContext, trace: DecisionTrace) -> None:
        """Never let a chat outage affect a decision that has already been made."""
        if not self.on_decision_notify:
            return
        try:
            self.on_decision_notify(trace, dict(ctx.signal))
        except Exception:  # noqa: BLE001
            logger.exception("decision notification failed")

    def _register_for_measurement(self, ctx: GateContext,
                                  trace: DecisionTrace) -> None:
        """
        Measure every recordable decision, refusals included.

        Unconditional and outside the recording path: on 10 June nineteen
        signals were lost to measurement because registration sat inside the
        branch that ran when anchoring succeeded. An audit outage must never
        blind the learning layer.
        """
        if self.resolver is None:
            return
        self.resolver.register(
            signal_id=trace.signal_id,
            direction=ctx.get("direction", str(ctx.signal.get("direction", ""))),
            ref_price=float(ctx.get("mid") or ctx.get("execution_price") or 0),
            timeframe=str(ctx.signal.get("timeframe", "")),
            session=str(ctx.signal.get("session", "")),
            action=trace.action.value,
            sl_pts=float(ctx.get("sl_pts") or 0),
            tp_pts=float(ctx.get("tp_pts") or 0),
            spread_pts=ctx.get("spread_pts"),
        )

    # ── watcher fills ─────────────────────────────────────────────────────────

    def on_watcher_fill(self, entry: Any, result: dict) -> None:
        """
        A zone entry confirmed and filled. Journal it, update zone memory, and
        record it as its own dossier.

        Route is "watcher", not "market" — the edge ledger segments on it, and
        conflating the two would hide exactly the comparison the whole routing
        design exists to measure.
        """
        self.journal.open_trade(
            ticket=result["ticket"],
            signal_id=entry.signal_id,
            direction=entry.direction.upper(),
            timeframe=entry.timeframe,
            session=entry.session,
            route=f"watcher:{entry.level_source or 'unknown'}",
            entry_price=result["price"],
            sl=result["sl"],
            tp=result.get("tp") or 0.0,
            volume=getattr(self.orders, "volume", 0.01),
        )
        self.risk.record_entry(entry.zone_price, entry.direction.lower(),
                               entry.timeframe)
        self._record_watcher_fill(entry, result)

    def _record_watcher_fill(self, entry: Any, result: dict) -> None:
        try:
            at = _now()
            proofs = proof.ProofSet(
                origin=str(entry.derived.get("origin_hash", "")) or
                proof.origin_hash(entry.signal),
                process=proof.process_hash(self.policy, self.code_version),
                state=proof.state_hash(**self.market.state(session=entry.session)),
            )
            dossier = dgml.build_dossier(
                signal_id=entry.signal_id,
                emitted_at=at,
                agent="watcher",
                proofs=proofs,
                decision={"action": "route", "reason": f"routed_{entry.route_kind}",
                          "bound_at": "zone", "also_objected": [], "gates": []},
                thesis={
                    "direction": entry.direction.upper(),
                    "timeframe": entry.timeframe,
                    "zone_price": entry.zone_price,
                    "entry_price": result["price"],
                    "stop": result["sl"],
                    "target": result.get("tp"),
                    "stop_pts": round(entry.stop_pts),
                    "route_kind": entry.route_kind,
                    "action": "watcher_fill",
                },
                execution=result,
                policy_version=str(self.policy.get("policy_version", "")),
                code_version=self.code_version,
                notes=[f"structural stop beyond the level; "
                       f"{'swept and reclaimed' if entry.swept else 'level held'}"],
            )
            if self.publisher is not None:
                published = self.publisher.publish_dossier(dossier, day=at[:10])
                self._enqueue_dossier_for(entry.signal_id, published["checksum"],
                                          published["uri"], "route")
        except Exception:  # noqa: BLE001
            logger.exception("RECORD FAILED for watcher fill %s", entry.signal_id)

    # ── outcome lifecycle ─────────────────────────────────────────────────────

    def on_anchored(self, entry: Any, result: Any) -> None:
        """Remember what landed, so its status can be moved later."""
        if self.anchor_index is None:
            return

        # A status update that has now landed: move the index to match the
        # chain. This is the only place a status is allowed to change.
        if entry.op == "update_status":
            signal_id = str(entry.metadata.get("signal_id", ""))
            if signal_id and self.anchor_index.record_status(
                    signal_id, entry.status, result.transaction):
                logger.info("%s is now %s on chain", signal_id, entry.status)
            return
        self.anchor_index.record_anchored(
            signal_id=str(entry.metadata.get("signal_id", "")),
            registry=entry.registry,
            registry_id=entry.registry_id,
            record_id=result.record_id,
            index=result.index,
            checksum=entry.checksum,
            transaction=result.transaction,
            status=entry.status,
            uri=entry.uri,
            kind=str(entry.metadata.get("kind", "dossier")),
            environment=self.environment,
        )

    def on_outcome_resolved(self, outcome: dict) -> None:
        """
        Move the dossier from committed to resolved.

        This is what makes the ordering guarantee visible rather than merely
        designed: the record was anchored as a prediction, and the transition
        is a separate, later, chain-versioned event. Nothing is rewritten.

        Signals that never anchored — refusals, which live in the daily log
        rather than their own record — simply have no entry, and that is not
        an error.
        """
        if self.anchor_index is None or self.outbox is None:
            return
        signal_id = str(outcome.get("signal_id", ""))
        entry = self.anchor_index.get(signal_id)
        if entry is None or entry.get("status") != STATUS_ACTIVE:
            return

        self.outbox.enqueue(
            op="update_status",
            registry=entry["registry"],
            registry_id=int(entry.get("registry_id") or 0),
            agent="oracle",
            record_id=int(entry.get("record_id") or 0),
            index=int(entry.get("index") or 0),
            # Their vocabulary, not ours. The prediction record is superseded
            # by the resolved one; it is not deleted and not revoked.
            status=STATUS_SUPERSEDED,
            created_at=_now(),
            metadata={"signal_id": signal_id, "kind": "status",
                      "outcome": outcome.get("outcome", ""),
                      "correct": bool(outcome.get("correct"))},
        )
        # NOT marked here. The index is a map of what the chain holds, and a
        # queued update is an intention. Marking it now produced 54 records
        # reading Superseded locally while every status update sat parked and
        # the chain still said Active — which a disclosure bundle would have
        # reported to a counterparty as fact. on_anchored does it once the
        # write lands.
        logger.info("%s resolved (%s) — status update queued",
                    signal_id, outcome.get("outcome"))

    # ── recording ─────────────────────────────────────────────────────────────

    def _record(self, ctx: GateContext, trace: DecisionTrace, origin: Any,
                execution: Optional[dict], received_at: str) -> None:
        """
        Never raises. A failure in the accountability layer must not become a
        failure in the trading layer — but it is logged loudly, because a
        silently missing record is the one outcome this whole design exists to
        prevent.
        """
        try:
            state = proof.state_hash(
                **self.market.state(session=str(ctx.signal.get("session", ""))))
            process = proof.process_hash(self.policy, self.code_version)
            proofs = proof.ProofSet(origin=origin.origin_hash,
                                    process=process, state=state)

            dossier_checksum = None
            if execution and "ticket" in execution:
                dossier = dgml.build_dossier(
                    signal_id=trace.signal_id,
                    emitted_at=received_at,
                    agent="execution",
                    proofs=proofs,
                    decision=dgml.decision_from_trace(trace),
                    thesis=dgml.thesis_from_trace(trace, ctx.derived),
                    execution=execution,
                    policy_version=str(self.policy.get("policy_version", "")),
                    dashboard_version=origin.dashboard_version,
                    code_version=self.code_version,
                )

                # Write the artifact BEFORE anchoring its hash. A record whose
                # uri resolves to nothing is worse than no record — it looks
                # verifiable and is not.
                if self.publisher is not None:
                    published = self.publisher.publish_dossier(
                        dossier, day=received_at[:10])
                    dossier_checksum = published["checksum"]
                    uri = published["uri"]
                else:
                    dossier_checksum = dgml.dossier_checksum(dossier)
                    uri = ""

                self._enqueue_dossier(trace, dossier_checksum, uri)

            if self.decision_log is not None:
                self.decision_log.add(
                    signal_id=trace.signal_id, at=received_at, trace=trace,
                    origin=proofs.origin, process=proofs.process, state=proofs.state,
                    dossier_checksum=dossier_checksum,
                )
        except Exception:  # noqa: BLE001
            logger.exception("RECORD FAILED for %s — decision is not accounted for",
                             trace.signal_id)

    def _enqueue_dossier(self, trace: DecisionTrace, checksum: str,
                         uri: str = "") -> None:
        self._enqueue_dossier_for(trace.signal_id, checksum, uri,
                                  trace.action.value)

    def _enqueue_dossier_for(self, signal_id: str, checksum: str, uri: str,
                             action: str, agent: str = "execution") -> None:
        if self.outbox is None:
            return
        self.outbox.enqueue(
            op="add_record",
            registry=self.registries.get("dossiers", "afritensor-dossiers"),
            # Kept for status updates, which DO take a numeric id. addRecord
            # itself identifies the registry by name.
            registry_id=self.registry_ids.get("dossiers", 0),
            agent=agent, uri=uri, checksum=checksum, status=STATUS_ACTIVE,
            created_at=_now(),
            metadata={"signal_id": signal_id, "action": action,
                      "policy": self.policy_hash,
                      "env": self.environment or "local"},
        )
