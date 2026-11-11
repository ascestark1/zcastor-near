"""
Origin capture: keep what the gates were given, so a decision can be replayed.

Proof of Origin hashes each payload but keeps only the hash. That proves which
payload was decided on, but nothing can be re-decided from a hash. This module
keeps the inputs, one JSON line per signal in `data/origins/YYYY-MM-DD.jsonl`:

- **payload**: the signal exactly as received. Its checksum is the origin hash
  already in the decision log, so a capture proves it is the payload decided on.
- **state**: the in-memory state the stateful gates read (zone memory, signal
  coherence, entry cooldown, feed staleness, hourly levels), taken just before
  the pipeline ran. Levels are taken just after, because a query may refresh
  them mid-run and the post-run copy is what the gate actually read.
- **tape**: every call the gates made to the venue, positions, the edge ledger
  and the risk adapter, with arguments and results, in order.
- **clock**: the wall-clock time the gates saw. Replay freezes time there.
- **outcome**: the pipeline's verdict, per-gate results and derived values
  (stop, target, prices), before the engine acted on it.

The policy file is stored once per hash in `data/origins/policies/`.

Capture never changes a decision and never takes the engine down. If any part of
it fails, the failure is logged and the signal is decided exactly as before.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("zcastor.record.capture")

CAPTURE_VERSION = 1


# ── JSON encoding that survives a round trip ─────────────────────────────────
# Gates unpack tuples and compare values, so a tuple must come back a tuple.

def encode(value: Any) -> Any:
    if isinstance(value, tuple):
        return {"__tuple__": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, deque):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return value.value                     # str-valued enums
    return {"__repr__": repr(value)}


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"__tuple__"}:
            return tuple(decode(v) for v in value["__tuple__"])
        if set(value) == {"__repr__"}:
            return value["__repr__"]
        return {k: decode(v) for k, v in value.items()}
    return value


def call_key(method: str, args: tuple, kwargs: dict) -> str:
    return json.dumps([method, encode(list(args)), encode(dict(kwargs))],
                      sort_keys=True, separators=(",", ":"))


# ── Recording ────────────────────────────────────────────────────────────────

class Recorder:
    """
    Stands in for an adapter and writes down every call made through it.

    Attribute reads are recorded too (`market.volume`). Exceptions are recorded
    and re-raised unchanged, so the gate sees exactly what it would have seen.
    """

    def __init__(self, name: str, target: Any, tape: list) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_tape", tape)
        object.__setattr__(self, "_on", True)

    def stop(self) -> None:
        object.__setattr__(self, "_on", False)

    def __getattr__(self, attr: str) -> Any:
        target = object.__getattribute__(self, "_target")
        value = getattr(target, attr)
        if not object.__getattribute__(self, "_on"):
            return value
        name = object.__getattribute__(self, "_name")
        tape = object.__getattribute__(self, "_tape")

        if not callable(value):
            tape.append({"obj": name, "attr": attr, "value": encode(value)})
            return value

        def recorded(*args: Any, **kwargs: Any) -> Any:
            entry = {"obj": name, "method": attr,
                     "args": encode(list(args)), "kwargs": encode(kwargs)}
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                entry["raises"] = {"type": type(exc).__name__, "msg": str(exc)}
                if object.__getattribute__(self, "_on"):
                    tape.append(entry)
                raise
            entry["result"] = encode(result)
            if object.__getattribute__(self, "_on"):
                tape.append(entry)
            return result

        return recorded


# ── Stateful component snapshots ─────────────────────────────────────────────
# These read private attributes on purpose: the components are ours, and the
# alternative is a public "dump your internals" method on each, used only here.

def snap_zones(zones: Any) -> dict:
    with zones._lock:
        return {"entries": {k: [dict(e) for e in v]
                            for k, v in zones._entries.items()}}


def restore_zones(zones: Any, snap: dict) -> None:
    with zones._lock:
        zones._entries = {k: [dict(e) for e in v]
                          for k, v in snap.get("entries", {}).items()}


def snap_coherence(coh: Any) -> dict:
    with coh._lock:
        return {"recent": {tf: list(q) for tf, q in coh._recent.items()}}


def restore_coherence(coh: Any, snap: dict) -> None:
    with coh._lock:
        coh._recent = {tf: deque(v, maxlen=coh.window)
                       for tf, v in snap.get("recent", {}).items()}


def snap_cooldown(gate: Any) -> dict:
    return {"last_fill": gate._last_fill, "last_price": gate._last_price,
            "last_direction": gate._last_direction}


def restore_cooldown(gate: Any, snap: dict) -> None:
    gate._last_fill = float(snap.get("last_fill") or 0.0)
    gate._last_price = float(snap.get("last_price") or 0.0)
    gate._last_direction = str(snap.get("last_direction") or "")


def snap_feed(gate: Any) -> dict:
    return {"last_tick_time": gate._last_tick_time,
            "first_seen_at": gate._first_seen_at}


def restore_feed(gate: Any, snap: dict) -> None:
    gate._last_tick_time = snap.get("last_tick_time")
    gate._first_seen_at = snap.get("first_seen_at")


def snap_levels(levels: Any) -> Optional[dict]:
    if levels is None:
        return None
    with levels._lock:
        return {"highs": list(levels._highs), "lows": list(levels._lows),
                "refreshed_at": levels._refreshed_at}


def restore_levels(levels: Any, snap: dict, now: float) -> None:
    with levels._lock:
        levels._highs = list(snap.get("highs", []))
        levels._lows = list(snap.get("lows", []))
        # Fresh as of the frozen clock, so a replayed query never refreshes.
        levels._refreshed_at = now


# ── Outcome summary (shared with replay) ─────────────────────────────────────

def summarise(trace: Any, derived: dict) -> dict:
    binding = trace.binding()
    return {
        "action": trace.action.value,
        "reason": trace.reason,
        "route_kind": trace.route_kind or "",
        "binding": binding.name if binding else "",
        "results": [[r.name, r.status.value, r.verdict.value, r.reason,
                     encode(r.detail)] for r in trace.results],
        "derived": encode(dict(derived)),
    }


# ── The capture writer ───────────────────────────────────────────────────────

class _Open:
    """One signal's capture, between begin() and finish()."""

    def __init__(self, base: dict, recorders: dict[str, Recorder],
                 tape: list) -> None:
        self.base = base
        self.recorders = recorders
        self.tape = tape

    def wrap(self, name: str, target: Any) -> Any:
        return self.recorders.get(name, target)


class OriginCapture:
    """
    `components` is a callable returning the live stateful objects by name:
    zones, signal_coherence, cooldown, feed, levels. Any may be None.
    """

    ADAPTERS = ("market", "risk", "positions", "ledger")

    def __init__(self, root: Path, *, policy: dict, policy_hash: str,
                 components: Callable[[], dict], symbol: str = "",
                 venue: str = "", clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.policy_hash = policy_hash
        self.symbol = symbol
        self.venue = venue
        self._components = components
        self._clock = clock
        self._lock = threading.Lock()
        self._store_policy(policy, policy_hash)

    def _store_policy(self, policy: dict, policy_hash: str) -> None:
        try:
            d = self.root / "policies"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{policy_hash.replace('sha256:', '')}.json"
            if not p.exists():
                p.write_text(json.dumps(policy, sort_keys=True, indent=1))
        except Exception:  # noqa: BLE001
            logger.exception("could not store policy copy for capture")

    def begin(self, *, payload: dict, signal_id: str, origin_hash: str,
              received_at: str, adapters: dict[str, Any]) -> Optional[_Open]:
        try:
            comps = self._components() or {}
            state = {
                "zones": snap_zones(comps["zones"]) if comps.get("zones") else None,
                "signal_coherence": (snap_coherence(comps["signal_coherence"])
                                     if comps.get("signal_coherence") else None),
                "cooldown": (snap_cooldown(comps["cooldown"])
                             if comps.get("cooldown") else None),
                "feed": snap_feed(comps["feed"]) if comps.get("feed") else None,
            }
            tape: list = []
            recorders = {n: Recorder(n, adapters[n], tape)
                         for n in self.ADAPTERS if adapters.get(n) is not None}
            base = {
                "v": CAPTURE_VERSION,
                "signal_id": signal_id,
                "origin_hash": origin_hash,
                "received_at": received_at,
                "clock": self._clock(),
                "symbol": self.symbol,
                "venue": self.venue,
                "policy_hash": self.policy_hash,
                "payload": encode(dict(payload)),
                "state": state,
            }
            return _Open(base, recorders, tape)
        except Exception:  # noqa: BLE001
            logger.exception("origin capture begin failed; deciding without it")
            return None

    def finish(self, cap: Optional[_Open], trace: Any, derived: dict) -> None:
        if cap is None:
            return
        try:
            for r in cap.recorders.values():
                r.stop()
            comps = self._components() or {}
            record = dict(cap.base)
            record["state"] = dict(record["state"],
                                   levels=snap_levels(comps.get("levels")))
            record["tape"] = cap.tape
            record["outcome"] = summarise(trace, derived)
            line = json.dumps(record, sort_keys=True, separators=(",", ":"),
                              allow_nan=False)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(self.root / f"{day}.jsonl", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("origin capture write failed for %s",
                             cap.base.get("signal_id"))
