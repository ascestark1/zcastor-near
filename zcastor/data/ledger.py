"""
Edge ledger — which segments have earned the right to trade.

A segment is (timeframe, session, route). Every resolved trade contributes its
R-multiple. A segment stays in shadow until it clears three tests:

  n ≥ promote_n          enough evidence to say anything at all
  lower CI > 0           the pessimistic end of the expectancy estimate is
                         still positive — a good mean with wide variance is
                         not an edge
  recent mean ≥ 0        the last `demote_window` trades have not gone
                         negative, so a segment that used to work but stopped
                         gets demoted rather than coasting on old results

Shadow means recorded but not binding. The gate is advisory unless enforcement
is switched on at wiring time, so the ledger accumulates evidence for weeks
before it is ever allowed to refuse anything.

POLICY SCOPE
------------
Every record carries the policy hash in force when the trade was taken, and a
verdict only counts records from the CURRENT policy.

This matters more than it looks. R-multiples normalise for position size, but
not for geometry: moving a 15m stop from 115 points to 500 changes the spread
from a third of the risk to a tenth, and the same signal produces a completely
different hit-rate distribution. Pooling those gives a number describing neither
configuration, and it would arrive looking perfectly ordinary.

The cost is real — changing policy resets every segment to n=0, so a parameter
tweak is expensive and should be. The alternative is a ledger whose numbers
quietly stop meaning anything the first time someone adjusts a stop.

The honest part: with n < 40 nothing here says anything. That is the point. A
system that starts refusing trades on eight samples is not measuring, it is
overfitting to noise and calling it discipline.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("zcastor.data.ledger")

Segment = tuple[str, str, str]


class EdgeLedger:
    def __init__(
        self,
        policy: Mapping[str, Any],
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        policy_hash: str = "",
    ) -> None:
        cfg = dict(policy.get("ledger") or {})
        self.window_days = int(cfg.get("window_days", 30))
        self.promote_n = int(cfg.get("promote_n", 40))
        self.demote_window = int(cfg.get("demote_window", 20))
        self.z = float(cfg.get("z", 1.64))

        self.path = Path(path)
        self._clock = clock
        # Verdicts are scoped to this. Empty means "count everything", which is
        # only right for tests and one-off analysis.
        self.policy_hash = str(policy_hash or "")
        self._segments: dict[Segment, deque] = defaultdict(deque)
        self._lock = threading.RLock()
        self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        loaded = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                seg = (str(row["tf"]), str(row["session"]),
                       str(row.get("route", "market")))
                self._segments[seg].append(
                    (float(row["ts"]), float(row["r"]),
                     str(row.get("policy", ""))))
                loaded += 1
            except (KeyError, TypeError, ValueError):
                continue
        logger.info("edge ledger loaded %d records across %d segments",
                    loaded, len(self._segments))

    def record(self, *, timeframe: str, session: str, route: str,
               r_multiple: float, signal_id: str = "",
               policy: str = "") -> None:
        """
        Append one resolved outcome.

        R-multiple, not dollars: a segment's edge must be comparable across
        account sizes and stop distances, and dollars are neither.
        """
        seg = (str(timeframe), str(session), str(route or "market"))
        row = {"ts": self._clock(), "tf": seg[0], "session": seg[1],
               "route": seg[2], "r": float(r_multiple), "signal_id": signal_id,
               # Which reasoning produced this outcome. Without it, records
               # from different geometries pool and the verdict is meaningless.
               "policy": policy or self.policy_hash}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

        with self._lock:
            self._segments[seg].append((row["ts"], row["r"], row["policy"]))

    # ── verdicts ──────────────────────────────────────────────────────────────

    def _values(self, seg: Segment, any_policy: bool = False) -> list[float]:
        """
        Outcomes in scope for a verdict.

        Filtered to the current policy unless explicitly asked otherwise, so a
        geometry change cannot silently inherit evidence gathered under the old
        one. Records outside the window are dropped as before.
        """
        cutoff = self._clock() - self.window_days * 86400
        with self._lock:
            entries = self._segments.get(seg)
            if not entries:
                return []
            while entries and entries[0][0] < cutoff:
                entries.popleft()
            return [r for _, r, pol in entries
                    if any_policy or not self.policy_hash or pol == self.policy_hash]

    def verdict(self, timeframe: str, session: str,
                route: str = "market") -> dict[str, Any]:
        seg = (str(timeframe), str(session), str(route or "market"))
        values = self._values(seg)
        n = len(values)

        if n == 0:
            # Say WHY there is nothing rather than implying the segment is new:
            # evidence under a superseded policy is out of scope, not absent.
            prior = len(self._values(seg, any_policy=True))
            why = (f"no data under current policy ({prior} under earlier)"
                   if prior else "no data")
            return {"action": "shadow", "n": 0, "exp": 0.0, "lo": 0.0,
                    "recent": 0.0, "why": why}

        mean = sum(values) / n
        if n > 1:
            variance = sum((v - mean) ** 2 for v in values) / (n - 1)
            lower = mean - self.z * math.sqrt(variance / n)
        else:
            # One sample says nothing about variance. Treat the bound as
            # negative so a single lucky trade can never graduate a segment.
            lower = -abs(mean)

        recent = sum(values[-self.demote_window:]) / min(n, self.demote_window)

        if n < self.promote_n:
            action, why = "shadow", f"n<{self.promote_n}"
        elif lower <= 0:
            action, why = "shadow", "lower CI <= 0"
        elif recent < 0:
            action, why = "shadow", f"last {self.demote_window} mean<0 (edge decayed)"
        else:
            action, why = "execute", "earned"

        return {"action": action, "n": n, "exp": round(mean, 2),
                "lo": round(lower, 2), "recent": round(recent, 2), "why": why}

    def snapshot(self) -> list[dict[str, Any]]:
        """Every segment, most evidence first. For reporting and the dossier."""
        with self._lock:
            segments = list(self._segments.keys())
        rows = []
        for tf, session, route in segments:
            v = self.verdict(tf, session, route)
            v.update(tf=tf, session=session, route=route)
            rows.append(v)
        rows.sort(key=lambda r: (-r["n"], r["tf"]))
        return rows

    def stats(self) -> dict[str, Any]:
        rows = self.snapshot()
        with self._lock:
            total = sum(len(v) for v in self._segments.values())
        in_scope = sum(r["n"] for r in rows)
        return {
            "policy": self.policy_hash[:19] or "unscoped",
            "segments": len(rows),
            "records": in_scope,
            # Records held under superseded policies. Visible, not discarded.
            "out_of_scope": total - in_scope,
            "executing": sum(1 for r in rows if r["action"] == "execute"),
            "shadow": sum(1 for r in rows if r["action"] == "shadow"),
        }
