"""
Anchor index and status lifecycle tests.

The point of all this is test_the_record_proves_the_thesis_preceded_the_outcome:
a dossier anchors as `committed` when the decision is made, and moves to
`resolved` only after price has spoken. Two separate, ordered, chain-versioned
events — which is the claim that makes the whole record worth anything.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.base import (  # noqa: E402
    AnchorBackend, AnchorRejected, AnchorResult, NullBackend, PendingRecord,
)


class RejectingChain(AnchorBackend):
    name = "rejecting"

    def add_record(self, entry):
        raise AnchorRejected("reverted")

    def update_status(self, entry):
        raise AnchorRejected("reverted")
from zcastor.anchor.index import AnchorIndex  # noqa: E402
from zcastor.anchor.outbox import Outbox  # noqa: E402

CHECKSUM = "b" * 64


def index(tmp):
    return AnchorIndex(Path(tmp) / "anchor_index.jsonl")


def anchored(idx, signal_id="sig_1", status="Active"):
    idx.record_anchored(signal_id=signal_id, registry="afritensor-dossiers",
                        registry_id=42, record_id=7, checksum=CHECKSUM,
                        transaction="0xabc", status=status,
                        uri="https://example.test/x.json")


# ── index ─────────────────────────────────────────────────────────────────────

def test_anchored_records_are_retrievable_by_signal():
    with tempfile.TemporaryDirectory() as d:
        idx = index(d)
        anchored(idx)
        entry = idx.get("sig_1")
        assert entry["record_id"] == 7
        assert entry["registry_id"] == 42
        assert entry["status"] == "Active"


def test_the_index_survives_a_restart():
    """The outcome arrives hours later, often after a restart."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "anchor_index.jsonl"
        anchored(AnchorIndex(path))
        assert AnchorIndex(path).get("sig_1")["record_id"] == 7


def test_status_change_supersedes_without_rewriting():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "anchor_index.jsonl"
        idx = AnchorIndex(path)
        anchored(idx)
        assert idx.record_status("sig_1", "Superseded", "0xdef")

        reopened = AnchorIndex(path)
        entry = reopened.get("sig_1")
        assert entry["status"] == "Superseded"
        assert entry["record_id"] == 7, "earlier fields must survive"
        assert len(path.read_text().splitlines()) == 2, "append, never rewrite"


def test_status_change_for_an_unknown_signal_is_refused():
    with tempfile.TemporaryDirectory() as d:
        assert not index(d).record_status("sig_missing", "Superseded")


def test_committed_lists_outstanding_lifecycle_work():
    with tempfile.TemporaryDirectory() as d:
        idx = index(d)
        anchored(idx, "sig_1")
        anchored(idx, "sig_2")
        idx.record_status("sig_2", "Superseded")
        assert [e["signal_id"] for e in idx.active()] == ["sig_1"]
        assert idx.stats() == {"total": 2, "Active": 1, "Superseded": 1}


def test_a_torn_line_does_not_lose_the_index():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "anchor_index.jsonl"
        anchored(AnchorIndex(path))
        with path.open("a") as fh:
            fh.write('{"signal_id": "trunc')
        assert AnchorIndex(path).get("sig_1") is not None


# ── outbox hook ───────────────────────────────────────────────────────────────

def test_the_outbox_reports_what_it_anchored():
    with tempfile.TemporaryDirectory() as d:
        seen = []
        box = Outbox(Path(d) / "outbox.jsonl",
                     on_anchored=lambda e, r: seen.append((e, r)))
        box.enqueue(op="add_record", registry="afritensor-dossiers",
                    checksum=CHECKSUM, metadata={"signal_id": "sig_1"})
        box.drain(NullBackend())
        assert seen[0][0].metadata["signal_id"] == "sig_1"
        assert seen[0][1].transaction.startswith("null:")


def test_a_broken_hook_does_not_un_anchor_a_record():
    def boom(entry, result):
        raise RuntimeError("index write failed")

    with tempfile.TemporaryDirectory() as d:
        box = Outbox(Path(d) / "outbox.jsonl", on_anchored=boom)
        box.enqueue(op="add_record", registry="r", checksum=CHECKSUM)
        assert box.drain(NullBackend())["sent"] == 1
        assert box.stats()["anchored"] == 1


# ── the lifecycle, end to end ─────────────────────────────────────────────────

class FakeEngineBits:
    """The two engine handlers, isolated from the rest of the engine."""

    def __init__(self, outbox, idx):
        self.outbox, self.anchor_index = outbox, idx

    on_anchored = None      # bound below from the real engine


def build(tmp):
    from zcastor.execution.engine import Engine

    idx = AnchorIndex(Path(tmp) / "anchor_index.jsonl")
    box = Outbox(Path(tmp) / "outbox.jsonl")
    engine = Engine(
        pipeline=None, market=None, orders=None, risk=None, targets=None,
        ledger=None, journal=None, positions=None, policy={},
        policy_hash="sha256:p", outbox=box, anchor_index=idx,
    )
    box.on_anchored = engine.on_anchored
    return engine, box, idx


def test_the_record_proves_the_thesis_preceded_the_outcome():
    with tempfile.TemporaryDirectory() as d:
        engine, box, idx = build(d)

        # Decision time: anchored as a prediction.
        box.enqueue(op="add_record", registry="afritensor-dossiers",
                    registry_id=42, checksum=CHECKSUM,
                    uri="https://example.test/sig_1.json",
                    metadata={"signal_id": "sig_1"})
        box.drain(NullBackend())
        assert idx.get("sig_1")["status"] == "Active"

        # Hours later: price has spoken.
        engine.on_outcome_resolved({"signal_id": "sig_1", "outcome": "tp",
                                    "correct": True})

        pending = box.pending[0]
        assert pending.op == "update_status"
        assert pending.status == "Superseded"
        assert pending.registry_id == 42
        assert pending.record_id == 1, "the chain-assigned id, not our checksum"
        assert pending.agent == "oracle"

        # The index must still say Active: the update is queued, not landed.
        # Claiming otherwise is what let 54 records read Superseded locally
        # while every status update sat parked and the chain said Active.
        assert idx.get("sig_1")["status"] == "Active", \
            "the index must not report a status the chain does not hold"

        box.drain(NullBackend())
        assert box.stats()["pending"] == 0
        assert idx.get("sig_1")["status"] == "Superseded"


def test_a_parked_status_update_never_moves_the_index():
    """The failure that prompted this: queued is not landed."""
    with tempfile.TemporaryDirectory() as d:
        engine, box, idx = build(d)
        box.enqueue(op="add_record", registry="afritensor-dossiers",
                    registry_id=42, checksum=CHECKSUM,
                    metadata={"signal_id": "sig_1"})
        box.drain(NullBackend())

        engine.on_outcome_resolved({"signal_id": "sig_1", "outcome": "tp",
                                    "correct": True})
        box.drain(RejectingChain())

        assert box.stats()["parked"] == 1
        assert idx.get("sig_1")["status"] == "Active"


def test_an_unanchored_signal_resolves_quietly():
    """Refusals live in the daily log, not their own record. Not an error."""
    with tempfile.TemporaryDirectory() as d:
        engine, box, _ = build(d)
        engine.on_outcome_resolved({"signal_id": "sig_refused", "outcome": "sl"})
        assert box.pending == []


def test_resolving_twice_does_not_queue_two_updates():
    with tempfile.TemporaryDirectory() as d:
        engine, box, idx = build(d)
        box.enqueue(op="add_record", registry="afritensor-dossiers",
                    registry_id=42, checksum=CHECKSUM,
                    metadata={"signal_id": "sig_1"})
        box.drain(NullBackend())

        outcome = {"signal_id": "sig_1", "outcome": "tp", "correct": True}
        engine.on_outcome_resolved(outcome)
        engine.on_outcome_resolved(outcome)
        assert len(box.pending) == 1


def test_the_status_update_carries_the_outcome():
    with tempfile.TemporaryDirectory() as d:
        engine, box, _ = build(d)
        box.enqueue(op="add_record", registry="afritensor-dossiers",
                    registry_id=42, checksum=CHECKSUM,
                    metadata={"signal_id": "sig_1"})
        box.drain(NullBackend())
        engine.on_outcome_resolved({"signal_id": "sig_1", "outcome": "sl",
                                    "correct": False})
        meta = box.pending[0].metadata
        assert meta["outcome"] == "sl" and meta["correct"] is False


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
