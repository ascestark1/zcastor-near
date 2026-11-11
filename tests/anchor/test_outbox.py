"""
Outbox tests.

The scenario that motivated this module is `test_survives_a_multi_day_chain_halt`:
MANTRA halted on 20 Aug, NVNM inherits settlement from it, and a naive anchor
would have dropped that day's records with a logged warning.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.base import (  # noqa: E402
    AnchorBackend, AnchorRejected, AnchorResult, AnchorUnavailable, NullBackend,
    PendingRecord,
)
from zcastor.anchor.outbox import Outbox  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class HaltedChain(AnchorBackend):
    name = "halted"

    def __init__(self):
        self.calls = 0

    def add_record(self, entry):
        self.calls += 1
        raise AnchorUnavailable("chain halted: cosmos-evm")

    update_status = add_record


class RejectingChain(AnchorBackend):
    name = "rejecting"

    def add_record(self, entry):
        raise AnchorRejected("caller lacks editor role on registry")

    update_status = add_record


class CountingBackend(NullBackend):
    pass


def outbox(tmp, clock=None, **kw):
    return Outbox(Path(tmp) / "outbox.jsonl", clock=clock or Clock(), **kw)


def enqueue_dossier(box, checksum="abc123", registry="afritensor-dossiers"):
    return box.enqueue(op="add_record", registry=registry,
                       uri=f"https://example.test/{checksum}.json",
                       checksum=checksum, metadata={"signal_id": "sig_1"})


# ── basics ────────────────────────────────────────────────────────────────────

def test_enqueue_then_drain_sends():
    with tempfile.TemporaryDirectory() as d:
        box, backend = outbox(d), NullBackend()
        enqueue_dossier(box)
        assert box.drain(backend)["sent"] == 1
        assert box.stats() == {"pending": 0, "parked": 0, "anchored": 1}


def test_checksum_prefix_is_stripped_for_the_chain():
    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        entry = box.enqueue(op="add_record", registry="r", checksum="sha256:deadbeef")
        assert entry.checksum == "deadbeef"


def test_duplicate_enqueue_is_refused():
    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        assert enqueue_dossier(box) is not None
        assert enqueue_dossier(box) is None, "same dossier must not queue twice"
        assert len(box.pending) == 1


def test_status_updates_are_not_deduped_against_each_other():
    """committed → resolved are two distinct units of work on one record."""
    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        a = box.enqueue(op="update_status", registry="r", record_id="rec1",
                        status="committed")
        b = box.enqueue(op="update_status", registry="r", record_id="rec1",
                        status="resolved")
        assert a is not None and b is not None


# ── the halt ──────────────────────────────────────────────────────────────────

def test_survives_a_multi_day_chain_halt():
    """20 Aug: MANTRA halted, NVNM went with it. Nothing may be lost."""
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        box = outbox(d, clock=clock)
        halted = HaltedChain()

        for i in range(5):
            box.enqueue(op="add_record", registry="r", checksum=f"c{i}")

        # Three days of failed drains.
        for _ in range(72):
            box.drain(halted)
            clock.advance(3600)

        assert len(box.pending) == 5, "nothing may be dropped during a halt"
        assert box.stats()["parked"] == 0

        # Chain returns.
        assert box.drain(NullBackend())["sent"] == 5
        assert box.stats()["pending"] == 0


def test_backoff_prevents_hammering_a_dead_chain():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        box, halted = outbox(d, clock=clock), HaltedChain()
        enqueue_dossier(box)

        box.drain(halted)                 # attempt 1
        box.drain(halted)                 # inside backoff, skipped
        assert halted.calls == 1

        clock.advance(10)                 # first backoff is 5s
        box.drain(halted)
        assert halted.calls == 2


def test_backoff_is_capped():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        box, halted = outbox(d, clock=clock), HaltedChain()
        enqueue_dossier(box)
        for _ in range(20):
            clock.advance(2000)
            box.drain(halted)
        entry = box.pending[0]
        assert entry.next_attempt_at - clock.t <= 900.0


# ── permanent failures ────────────────────────────────────────────────────────

def test_rejected_entries_are_parked_not_retried():
    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        enqueue_dossier(box)
        box.drain(RejectingChain())
        assert box.stats() == {"pending": 0, "parked": 1, "anchored": 0}


def test_parked_entries_can_be_requeued_deliberately():
    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        enqueue_dossier(box)
        box.drain(RejectingChain())
        assert box.requeue_parked() == 1
        assert box.drain(NullBackend())["sent"] == 1


def test_max_attempts_parks_rather_than_looping_forever():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        box = outbox(d, clock=clock, max_attempts=3)
        enqueue_dossier(box)
        halted = HaltedChain()
        for _ in range(5):
            clock.advance(2000)
            box.drain(halted)
        assert box.stats()["parked"] == 1


def test_a_broken_backend_never_takes_down_the_caller():
    class Exploding(AnchorBackend):
        name = "boom"
        def add_record(self, entry):
            raise MemoryError("something unexpected")
        update_status = add_record

    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        enqueue_dossier(box)
        stats = box.drain(Exploding())      # must not raise
        assert stats["failed"] == 1


# ── durability ────────────────────────────────────────────────────────────────

def test_pending_work_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "outbox.jsonl"
        box = Outbox(path, clock=Clock())
        for i in range(3):
            box.enqueue(op="add_record", registry="r", checksum=f"c{i}")
        del box

        reopened = Outbox(path, clock=Clock())
        assert len(reopened.pending) == 3
        assert reopened.drain(NullBackend())["sent"] == 3


def test_restart_does_not_double_anchor():
    """A public ledger showing two records for one decision is a real problem."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "outbox.jsonl"
        box = Outbox(path, clock=Clock())
        enqueue_dossier(box, checksum="abc123")
        box.drain(NullBackend())
        del box

        reopened = Outbox(path, clock=Clock())
        assert enqueue_dossier(reopened, checksum="abc123") is None
        assert reopened.stats()["pending"] == 0


def test_a_torn_final_line_does_not_lose_the_queue():
    """A crash mid-append can corrupt the last line and nothing earlier."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "outbox.jsonl"
        box = Outbox(path, clock=Clock())
        for i in range(3):
            box.enqueue(op="add_record", registry="r", checksum=f"c{i}")
        with path.open("a") as fh:
            fh.write('{"_": "enqueue", "entry": {"id": "trunc')

        reopened = Outbox(path, clock=Clock())
        assert len(reopened.pending) == 3


def test_compaction_preserves_state():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "outbox.jsonl"
        box = Outbox(path, clock=Clock())
        for i in range(5):
            box.enqueue(op="add_record", registry="r", checksum=f"c{i}")
        box.drain(NullBackend())
        for i in range(5, 8):
            box.enqueue(op="add_record", registry="r", checksum=f"c{i}")

        before = box.stats()
        box.compact()
        reopened = Outbox(path, clock=Clock())
        assert reopened.stats() == before
        # Dedupe is per (registry, checksum), so this must match the registry
        # the entries were actually queued into.
        assert reopened.enqueue(op="add_record", registry="r", checksum="c0") is None


def test_drain_respects_its_limit():
    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        for i in range(10):
            box.enqueue(op="add_record", registry="r", checksum=f"c{i}")
        assert box.drain(NullBackend(), limit=4)["sent"] == 4
        assert len(box.pending) == 6


# ── duplicate protection ──────────────────────────────────────────────────────

def test_requeueing_an_anchored_entry_does_not_send_it_twice():
    """
    Two on-chain records for one document is the exact ambiguity the index
    exists to prevent, and requeue_parked bypassed the enqueue-time check.
    """
    with tempfile.TemporaryDirectory() as d:
        backend = NullBackend()
        box = outbox(d)
        enqueue_dossier(box, checksum="dup1")
        box.drain(backend)
        assert len(backend.submitted) == 1

        # Force the entry back into the parked set and requeue it.
        entry = PendingRecord(id="x", op="add_record",
                              registry="afritensor-dossiers", checksum="dup1")
        box._parked["x"] = entry
        box.requeue_parked()
        box.drain(backend)

        assert len(backend.submitted) == 1, "must not anchor the same work twice"


def test_a_parked_entry_blocks_a_duplicate_enqueue():
    """
    Parked work is outstanding, not finished. Without this a failing status
    update is re-enqueued every cycle and the outbox grows without bound.
    """
    with tempfile.TemporaryDirectory() as d:
        box = outbox(d)
        box.enqueue(op="update_status", registry="afritensor-dossiers",
                    registry_id=4298, record_id=7, status="Superseded")
        box.drain(RejectingChain())
        assert box.stats()["parked"] == 1

        again = box.enqueue(op="update_status", registry="afritensor-dossiers",
                            registry_id=4298, record_id=7, status="Superseded")
        assert again is None
        assert box.stats()["parked"] == 1


def test_duplicate_suppression_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "outbox.jsonl"
        box = Outbox(path, clock=Clock())
        enqueue_dossier(box, checksum="dup2")
        box.drain(NullBackend())

        reopened = Outbox(path, clock=Clock())
        backend = NullBackend()
        reopened.drain(backend)
        assert backend.submitted == []


# ── serialisation ─────────────────────────────────────────────────────────────

def test_every_field_survives_a_round_trip():
    """
    A hand-written to_dict silently stopped listing registry_id and index when
    those fields were added, so entries persisted without them and could never
    be sent. Deriving the field list from the dataclass makes that impossible.
    """
    from zcastor.anchor.base import PendingRecord
    original = PendingRecord(
        id="e1", op="add_record", registry="afritensor-decisions",
        registry_id=4297, agent="oracle", uri="https://example.test/x.json",
        checksum="ab" * 32, checksum_algo="sha256", metadata={"k": "v"},
        status="Active", record_id=9, index=2, created_at="2026-08-30",
        attempts=3, next_attempt_at=123.0, last_error="boom",
    )
    restored = PendingRecord.from_dict(original.to_dict())
    for field in original.__slots__:
        assert getattr(restored, field) == getattr(original, field), field


def test_registry_id_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "outbox.jsonl"
        box = Outbox(path, clock=Clock())
        box.enqueue(op="add_record", registry="afritensor-decisions",
                    registry_id=4297, checksum="cd" * 32)
        assert Outbox(path, clock=Clock()).pending[0].registry_id == 4297


def test_a_repaired_entry_keeps_its_repair():
    """Requeue must persist the corrected field, not just fix it in memory."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "outbox.jsonl"
        box = Outbox(path, clock=Clock())
        box.enqueue(op="add_record", registry="afritensor-decisions",
                    checksum="ef" * 32)
        box.drain(RejectingChain())
        assert box.parked[0].registry_id == 0

        box.parked[0].registry_id = 4297
        box.requeue_parked()

        assert Outbox(path, clock=Clock()).pending[0].registry_id == 4297


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
