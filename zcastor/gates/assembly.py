"""
The gate stack, in order, built in one place.

The server and the replay harness both build the stack from here. If they built
it separately, a gate added to one and not the other would make every replay
quietly wrong, and nothing would say so.

Stateful gates (feed staleness, entry cooldown, London bias with its clock) are
passed in rather than made here, so their owner can snapshot and restore them.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .coherence import ModelCoherenceGate, SignalCoherenceGate
from .confidence import ConfidenceGate
from .cooldown import EntryCooldownGate
from .deadman import DeadmanGate
from .direction import DirectionGate
from .exhaustion import SessionExhaustionGate
from .feed import FeedGate
from .ledger import EdgeLedgerGate
from .london import LondonBiasGate
from .range import RangeAdequacyGate, RiskRewardGate
from .regime import RegimeContextGate
from .session import SessionGate
from .spread import SpreadHardGate, SpreadViabilityGate
from .stack import StackLimitGate
from .structural import StructuralLevelGate
from .targets import TargetStage
from .viability import ViabilityGate
from .zone import FlipConfirmGate, ZoneGate


def build_gates(
    policy: Mapping[str, Any],
    *,
    levels: Any,
    cooldown: EntryCooldownGate,
    feed: Optional[FeedGate] = None,
    london: Optional[LondonBiasGate] = None,
) -> list:
    return [
        DirectionGate(), ConfidenceGate(), feed or FeedGate(), SpreadHardGate(),
        DeadmanGate(), StackLimitGate(), cooldown,
        ZoneGate(), FlipConfirmGate(), SignalCoherenceGate(),
        ModelCoherenceGate(), SessionGate(), RegimeContextGate(),
        london or LondonBiasGate(),
        TargetStage(),
        SpreadViabilityGate(), StructuralLevelGate(levels, enforce=bool(
            policy.get("structural_level_enforce", False))),
        RangeAdequacyGate(), RiskRewardGate(),
        SessionExhaustionGate(),
        EdgeLedgerGate(enforce=bool(policy.get("ledger_enforce"))),
        ViabilityGate(),
    ]
