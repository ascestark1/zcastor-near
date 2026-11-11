"""
The outcome resolver.

Registers every recordable signal — including the ones that were refused — and
measures what price did over that timeframe's horizon.

Refusals are the point. Measuring only what you traded tells you nothing about
whether declining was right; the refusals are the counterfactual, and without
them "the system says no a lot" is an assertion rather than a finding. A gate
that has been suppressing profitable signals for three months should be
discoverable, and it only is if the signals it killed were measured anyway.

Two things carried from v1, both from real damage:

**Registration is unconditional.** On 10 June the bridge was down between 21:18
and 01:03 and nineteen signals were lost to measurement, because registration
sat inside the branch that ran when anchoring succeeded. An audit outage must
never blind the learning layer, so nothing here depends on the chain.

**Unresolvable is not wrong.** When bars cannot be fetched the record stays
pending and is retried. Scoring it as incorrect would fill the model-coherence
number with the resolver's own failures.

Separate from booking. `Booker` records execution truth from the broker's deal
history; this records signal truth from price. A trade can be stopped out on a
signal whose direction was right, and those are different lessons.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .classify import classify, horizon_minutes

logger = logging.getLogger("zcastor.resolve")


class OutcomeResolver:
    def __init__(
        self,
        *,
        policy: Mapping[str, Any],
        market: Any,
        pending_path: str | Path,
        outcomes_path: str | Path,
        on_resolved: Optional[Callable[[dict], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        cfg = dict(policy.get("resolver") or {})
        self.policy = policy
        self.market = market
        self.pending_path = Path(pending_path)
        self.outcomes_path = Path(outcomes_path)
        self.on_resolved = on_resolved
        self._clock = clock

        self.default_spread = float(cfg.get("default_spread_pts", 50.0))
        self.default_rr = float(cfg.get("default_rr", 4.0 / 3.0))
        self.give_up_seconds = float(cfg.get("give_up_hours", 72)) * 3600

        self._lock = threading.RLock()
        self._pending: list[dict] = self._load(self.pending_path, [])

    # ── persistence ───────────────────────────────────────────────────────────

    @staticmethod
    def _load(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("could not read %s (%s) — starting empty", path, exc)
            return default

    def _save_pending(self) -> None:
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(json.dumps(self._pending, indent=2))

    def _append_outcome(self, record: dict) -> None:
        outcomes = self._load(self.outcomes_path, [])
        if not isinstance(outcomes, list):
            outcomes = []
        outcomes.append(record)
        self.outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        self.outcomes_path.write_text(json.dumps(outcomes, indent=2))

    # ── registration ──────────────────────────────────────────────────────────

    def register(self, *, signal_id: str, direction: str, ref_price: float,
                 timeframe: str, session: str = "", action: str = "",
                 sl_pts: float = 0.0, tp_pts: float = 0.0,
                 spread_pts: Optional[float] = None) -> None:
        """
        Register a signal for measurement. Called for every recordable decision,
        traded or refused. Never raises — measurement failing must not stop a
        trade from being placed.
        """
        try:
            if ref_price <= 0:
                return
            now = self._clock()
            horizon = horizon_minutes(self.policy, timeframe)
            record = {
                "signal_id": signal_id,
                "direction": str(direction).upper(),
                "ref_price": float(ref_price),
                "timeframe": str(timeframe),
                "session": str(session),
                "action": str(action),
                "sl_pts": float(sl_pts or 0.0),
                "tp_pts": float(tp_pts or 0.0),
                "spread_pts": float(self.default_spread if spread_pts is None
                                    else spread_pts),
                "registered_ts": now,
                "due_ts": now + horizon * 60,
                "horizon_min": horizon,
                "attempts": 0,
            }
            with self._lock:
                if any(r["signal_id"] == signal_id for r in self._pending):
                    return
                self._pending.append(record)
                self._save_pending()
        except Exception:  # noqa: BLE001
            logger.exception("could not register %s for measurement", signal_id)

    # ── resolution ────────────────────────────────────────────────────────────

    def tick(self) -> dict[str, int]:
        """One cycle. Never raises."""
        now = self._clock()
        with self._lock:
            due = [r for r in self._pending if r["due_ts"] <= now]

        resolved = abandoned = stalled = 0

        for record in due:
            if now - record["registered_ts"] > self.give_up_seconds:
                self._drop(record, "gave_up")
                abandoned += 1
                continue

            verdict = self._resolve(record)
            if verdict is None:
                record["attempts"] += 1
                stalled += 1
                continue

            self._complete(record, verdict)
            resolved += 1

        if resolved or abandoned:
            with self._lock:
                self._save_pending()

        with self._lock:
            remaining = len(self._pending)
        return {"resolved": resolved, "abandoned": abandoned,
                "stalled": stalled, "pending": remaining}

    def _resolve(self, record: dict) -> Optional[dict]:
        start = record["registered_ts"]
        end = start + record["horizon_min"] * 60
        try:
            rates = self.market.get_rates_range(start, end)
        except Exception as exc:  # noqa: BLE001
            logger.debug("rates unavailable for %s: %s", record["signal_id"], exc)
            return None

        if not rates:
            return None

        return classify(
            ref_price=record["ref_price"],
            direction=record["direction"],
            rates=rates,
            sl_pts=record["sl_pts"],
            tp_pts=record["tp_pts"],
            spread_pts=record["spread_pts"],
            volume=float(getattr(self.market, "volume", 0.01)),
            default_rr=self.default_rr,
        )

    def _complete(self, record: dict, verdict: dict) -> None:
        outcome = {
            "signal_id": record["signal_id"],
            "direction": record["direction"],
            "timeframe": record["timeframe"],
            "session": record["session"],
            "action": record["action"],
            "ref_price": record["ref_price"],
            "resolved_ts": self._clock(),
            **verdict,
        }
        self._append_outcome(outcome)
        self._drop(record, "resolved")

        logger.info("resolved %s — %s, direction %s (%+.0fpts)",
                    record["signal_id"], verdict["outcome"],
                    "correct" if verdict["correct"] else "wrong",
                    verdict["close_pts"])

        if self.on_resolved:
            try:
                self.on_resolved(outcome)
            except Exception:  # noqa: BLE001
                logger.exception("on_resolved hook failed for %s",
                                 record["signal_id"])

    def _drop(self, record: dict, reason: str) -> None:
        with self._lock:
            self._pending = [r for r in self._pending
                             if r["signal_id"] != record["signal_id"]]
        if reason != "resolved":
            logger.info("dropped %s from measurement: %s",
                        record["signal_id"], reason)

    # ── reporting ─────────────────────────────────────────────────────────────

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def stats(self) -> dict[str, Any]:
        outcomes = self._load(self.outcomes_path, [])
        if not isinstance(outcomes, list) or not outcomes:
            return {"pending": self.pending_count(), "resolved": 0}

        correct = sum(1 for o in outcomes if o.get("correct"))
        won = sum(1 for o in outcomes if o.get("won"))
        refused = [o for o in outcomes if o.get("action") == "suppress"]
        refused_right = sum(1 for o in refused if not o.get("won"))

        return {
            "pending": self.pending_count(),
            "resolved": len(outcomes),
            "direction_accuracy": round(correct / len(outcomes), 3),
            "net_win_rate": round(won / len(outcomes), 3),
            # Of the signals we refused, how many would have lost anyway?
            # This is the number that says whether the gates are earning
            # their keep, and it cannot be computed without measuring refusals.
            "refusals_measured": len(refused),
            "refusals_vindicated": (round(refused_right / len(refused), 3)
                                    if refused else None),
        }
