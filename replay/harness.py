"""
Re-decide captured signals offline, with the real gate code.

Each capture (zcastor/record/capture.py) holds the payload, the state of every
stateful gate, a tape of every venue call, and the clock. Replay rebuilds the
gate stack from gates/assembly.py under a chosen policy, restores the state,
freezes time at the captured clock, plays the tape back in place of the venue,
and runs the pipeline. Nothing touches a network, a broker or a chain.

What is re-computed rather than played back: the gates themselves, the target
optimizer, zone memory and signal coherence. Those are the pieces that read
point distances from the policy, so a policy change must be able to change
them. What is played back: the venue (prices, balance, margin, positions,
session opens), the edge ledger, model coherence and today's PnL. Those are
facts about the world at that moment, not judgements.

A call the replay makes that is not on the tape is a "tape miss". It means the
policy under test asked the venue something the original run never asked (a
different volume for margin, say). The gate then fails closed, the verdict
differs, and the diff says why.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from zcastor.gates.assembly import build_gates
from zcastor.gates.base import GateContext
from zcastor.gates.cooldown import EntryCooldownGate
from zcastor.gates.feed import FeedGate
from zcastor.gates.london import LondonBiasGate
from zcastor.gates.pipeline import GatePipeline
from zcastor.market.levels import HourlyLevels
from zcastor.record import capture as cap
from zcastor.risk.coherence import SignalCoherence
from zcastor.risk.zones import ZoneTracker
from zcastor.targets.optimizer import TargetOptimizer

# Risk-adapter methods that replay computes from restored state.
RECOMPUTED_RISK = {"check_zone", "zone_phase", "check_signal_coherence"}


class TapeMiss(Exception):
    """The replay asked the venue something the original run did not."""


class TapePlayer:
    def __init__(self, name: str, tape: Iterable[dict]) -> None:
        self._name = name
        self._calls: dict[str, deque] = defaultdict(deque)
        self._attrs: dict[str, list] = defaultdict(list)
        self.misses: list[str] = []
        for e in tape:
            if e.get("obj") != name:
                continue
            if "attr" in e:
                self._attrs[e["attr"]].append(cap.decode(e["value"]))
            else:
                key = json.dumps([e["method"], e["args"], e["kwargs"]],
                                 sort_keys=True, separators=(",", ":"))
                self._calls[key].append(e)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr in self._attrs:
            return self._attrs[attr][0]

        def played(*args: Any, **kwargs: Any) -> Any:
            key = json.dumps([attr, cap.encode(list(args)), cap.encode(kwargs)],
                             sort_keys=True, separators=(",", ":"))
            queue = self._calls.get(key)
            if not queue:
                miss = f"{self._name}.{attr}{tuple(args)}{kwargs or ''}"
                self.misses.append(miss)
                raise TapeMiss(f"not on tape: {miss}")
            # Repeat the last answer if a call recurs more often than recorded;
            # within one decision the venue answers from its per-signal cache.
            entry = queue.popleft() if len(queue) > 1 else queue[0]
            if "raises" in entry:
                exc_type = type(entry["raises"]["type"], (Exception,), {})
                raise exc_type(entry["raises"]["msg"])
            return cap.decode(entry["result"])

        return played


class ReplayRisk:
    """The risk adapter: zone memory and coherence live, the rest from tape."""

    def __init__(self, zones: ZoneTracker, signal: SignalCoherence,
                 tape: TapePlayer) -> None:
        self.zones = zones
        self._signal = signal
        self._tape = tape

    def check_zone(self, price: float, direction: str, timeframe: str) -> tuple:
        return self.zones.check(price, direction, timeframe)

    def zone_phase(self, price: float, direction: str, timeframe: str) -> str:
        return self.zones.phase(price, direction, timeframe)

    def check_signal_coherence(self, direction: str, timeframe: str) -> tuple:
        return self._signal.check(direction, timeframe)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        return getattr(self._tape, attr)


@dataclass
class Replayed:
    record: dict
    outcome: dict
    misses: list[str] = field(default_factory=list)


def replay_one(record: dict, policy: dict) -> Replayed:
    """Re-decide one captured signal under `policy`."""
    now = float(record["clock"])
    clock = lambda: now  # noqa: E731
    state = record.get("state") or {}
    tape = record.get("tape") or []

    zones = ZoneTracker(policy, clock=clock)
    if state.get("zones"):
        cap.restore_zones(zones, state["zones"])
    signal = SignalCoherence(policy, clock=clock)
    if state.get("signal_coherence"):
        cap.restore_coherence(signal, state["signal_coherence"])
    cooldown = EntryCooldownGate(clock=clock)
    if state.get("cooldown"):
        cap.restore_cooldown(cooldown, state["cooldown"])
    feed = FeedGate(clock=clock)
    if state.get("feed"):
        cap.restore_feed(feed, state["feed"])

    levels = None
    if state.get("levels") is not None:
        cfg = dict(policy.get("watcher", {}).get("hourly_levels", {}))
        if cfg.get("enabled", True):
            levels = HourlyLevels(
                _no_rates, lookback_hours=int(cfg.get("lookback_hours", 168)),
                k=int(cfg.get("swing_k", 3)),
                refresh_seconds=float(cfg.get("refresh_seconds", 900)),
                clock=clock)
            cap.restore_levels(levels, state["levels"], now)

    london = LondonBiasGate(now=lambda: datetime.fromtimestamp(now, timezone.utc))

    players = {n: TapePlayer(n, tape) for n in ("market", "risk",
                                                 "positions", "ledger")}
    ctx = GateContext(
        signal=dict(cap.decode(record["payload"])),
        origin_hash=record["origin_hash"],
        policy=policy,
        market=players["market"],
        risk=ReplayRisk(zones, signal, players["risk"]),
        positions=players["positions"],
        ledger=players["ledger"],
        targets=TargetOptimizer(policy),
    )
    pipeline = GatePipeline(build_gates(policy, levels=levels,
                                        cooldown=cooldown, feed=feed,
                                        london=london))
    trace = pipeline.run(ctx, record["signal_id"], record.get("policy_hash", ""))
    misses = [m for p in players.values() for m in p.misses]
    # Through the same encoder the capture used, so equal means equal.
    outcome = json.loads(json.dumps(cap.summarise(trace, ctx.derived),
                                    sort_keys=True))
    return Replayed(record, outcome, misses)


def _no_rates(_from: float, _to: float) -> list:
    raise TapeMiss("hourly levels tried to refresh during replay")


# ── comparing ────────────────────────────────────────────────────────────────

def diff(a: dict, b: dict) -> list[str]:
    """Every difference between two outcomes, as readable lines."""
    out: list[str] = []
    for k in ("action", "reason", "route_kind", "binding"):
        if a.get(k) != b.get(k):
            out.append(f"{k}: {a.get(k)!r} -> {b.get(k)!r}")

    ra = {r[0]: r for r in a.get("results", [])}
    rb = {r[0]: r for r in b.get("results", [])}
    for name in list(ra) + [n for n in rb if n not in ra]:
        x, y = ra.get(name), rb.get(name)
        if x is None or y is None:
            out.append(f"gate {name}: {'absent' if x is None else 'ran'} -> "
                       f"{'absent' if y is None else 'ran'}")
            continue
        for i, label in ((1, "status"), (2, "verdict"), (3, "reason")):
            if x[i] != y[i]:
                out.append(f"gate {name} {label}: {x[i]!r} -> {y[i]!r}")
        if x[4] != y[4]:
            for k in sorted(set(x[4]) | set(y[4])):
                if x[4].get(k) != y[4].get(k):
                    out.append(f"gate {name} detail.{k}: "
                               f"{x[4].get(k)!r} -> {y[4].get(k)!r}")

    da, db = a.get("derived", {}), b.get("derived", {})
    for k in sorted(set(da) | set(db)):
        if da.get(k) != db.get(k):
            out.append(f"derived.{k}: {da.get(k)!r} -> {db.get(k)!r}")
    return out


# ── loading ──────────────────────────────────────────────────────────────────

def load_records(root: Path, *, since: str = "", until: str = "",
                 symbol: str = "") -> Iterator[dict]:
    for path in sorted(Path(root).glob("*.jsonl")):
        day = path.stem
        if since and day < since:
            continue
        if until and day > until:
            continue
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # A torn last line from a crash is expected; skip it.
                    continue
                if symbol and rec.get("symbol") != symbol:
                    continue
                yield rec


def load_policy_for(root: Path, policy_hash: str,
                    cache: Optional[dict] = None) -> Optional[dict]:
    cache = {} if cache is None else cache
    if policy_hash not in cache:
        p = Path(root) / "policies" / f"{policy_hash.replace('sha256:', '')}.json"
        cache[policy_hash] = json.loads(p.read_text()) if p.exists() else None
    return cache[policy_hash]
