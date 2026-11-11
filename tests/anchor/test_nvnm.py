"""
NVNM backend tests.

Everything except the RPC call itself. The transport is faked, so argument
shapes, metadata encoding, validation and error classification are all covered —
which is most of what can go wrong, and all of what we can check before the
first real transaction.

What these CANNOT tell us: whether the ABI matches the precompile, whether the
argument order is right, or whether the fee denom works. Those need the chain.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.base import (  # noqa: E402
    AnchorRejected, AnchorUnavailable, PendingRecord,
)
from zcastor.anchor.nvnm import (  # noqa: E402
    AGENT_ROLES, REGISTRIES, NvnmBackend, Transport,
)
from zcastor.anchor.outbox import Outbox  # noqa: E402

CHECKSUM = "a" * 64


class FakeTransport(Transport):
    address = "0x5501000000000000000000000000000000005331"

    def __init__(self, *, status=1, raises=None):
        self.calls = []
        self._status = status
        self._raises = raises

    def call(self, function, args):
        self.calls.append((function, args))
        if self._raises:
            raise self._raises
        return {"tx": "0xabc123", "status": self._status, "block": 4242}


def record(**kw):
    base = dict(id="e1", op="add_record", registry="afritensor-dossiers",
                registry_id=42,
                uri="https://example.test/trades/2026-08-27/sig_1.json",
                checksum=CHECKSUM, metadata={"signal_id": "sig_1"},
                status="Active")
    base.update(kw)
    return PendingRecord(**base)


# ── add_record ────────────────────────────────────────────────────────────────

def test_add_record_sends_a_record_struct():
    """
    addRecord takes ONE Record tuple, and identifies the registry by NAME —
    the opposite of every other function, which take a numeric id.
    """
    t = FakeTransport()
    result = NvnmBackend(t).add_record(record())
    fn, args = t.calls[0]
    assert fn == "addRecord"
    assert len(args) == 1, "a single struct, not flattened fields"

    uri, checksum, algo, meta, ts, status, rid, index, latest, reg_id = args[0]
    assert reg_id == 42, "the registry is a numeric id at the END of the tuple"
    assert uri.endswith("sig_1.json")
    assert checksum == CHECKSUM
    assert algo == "sha256"
    assert status == "Active"
    assert (ts, rid, index, latest) == ("", 0, 0, False), "chain sets these"
    assert result.transaction == "0xabc123"


def test_metadata_is_canonical_json():
    """Ordering-dependent metadata would make equivalent records differ on chain."""
    t = FakeTransport()
    NvnmBackend(t).add_record(record(metadata={"b": 2, "a": 1}))
    assert t.calls[0][1][0][3] == '{"a":1,"b":2}'


def test_empty_metadata_gets_a_label_because_the_chain_rejects_braces():
    """The precompile refuses an empty metadata string, and {} counts as empty."""
    t = FakeTransport()
    NvnmBackend(t).add_record(record(metadata={}))
    assert t.calls[0][1][0][3] != "{}"
    assert "label" in t.calls[0][1][0][3]


def test_a_prefixed_checksum_is_refused():
    """
    'sha256:abc…' would anchor a value that never matches the published file —
    worse than not anchoring, because it looks verifiable.
    """
    try:
        NvnmBackend(FakeTransport()).add_record(record(checksum="sha256:" + CHECKSUM))
    except AnchorRejected as e:
        assert "64 hex chars" in str(e)
    else:
        raise AssertionError("expected AnchorRejected")


def test_missing_checksum_is_refused():
    try:
        NvnmBackend(FakeTransport()).add_record(record(checksum=""))
    except AnchorRejected:
        pass
    else:
        raise AssertionError("expected AnchorRejected")


def test_a_record_without_a_numeric_registry_id_is_refused():
    """addRecord identifies the registry by ID; the name is only a label."""
    try:
        NvnmBackend(FakeTransport()).add_record(record(registry_id=0))
    except AnchorRejected as e:
        assert "registry_id" in str(e)
    else:
        raise AssertionError("expected AnchorRejected")


def test_a_reverted_transaction_is_permanent():
    """Mined and reverted means the chain is up and the request is ours."""
    try:
        NvnmBackend(FakeTransport(status=0)).add_record(record())
    except AnchorRejected as e:
        assert "reverted" in str(e)
    else:
        raise AssertionError("expected AnchorRejected")


# ── status updates ────────────────────────────────────────────────────────────

def test_update_status_addresses_a_record_by_id_and_index():
    """
    Decoded from calls that succeeded on chain: there is no checksum argument.
    A record is (registryId, recordId, index). The documented five-argument
    form carrying a checksum has never appeared on chain.
    """
    t = FakeTransport()
    NvnmBackend(t).update_status(record(op="update_status", record_id=9,
                                        index=1, status="Superseded"))
    assert t.calls[0] == ("updateRecordStatus", [42, 9, 1, "Superseded"])


def test_a_missing_index_defaults_to_the_first_version():
    """Records on chain use index 1 for their first version, so 0 addresses nothing."""
    t = FakeTransport()
    NvnmBackend(t).update_status(record(op="update_status", record_id=9,
                                        index=0, status="Superseded"))
    assert t.calls[0][1][2] == 1


def test_status_update_without_a_record_id_is_refused():
    try:
        NvnmBackend(FakeTransport()).update_status(record(op="update_status",
                                                          record_id=0))
    except AnchorRejected:
        pass
    else:
        raise AssertionError("expected AnchorRejected")


# ── administration ────────────────────────────────────────────────────────────

def test_add_registry_sends_three_strings():
    """
    Verified against the chain: addRegistry(string,string,string). The
    two-argument version we shipped first hashed to a selector the precompile
    does not implement, and four transactions reverted before we found it.
    """
    t = FakeTransport()
    NvnmBackend(t).add_registry("afritensor-dossiers", "trade dossiers")
    assert t.calls[0] == ("addRegistry",
                          ["afritensor-dossiers", "trade dossiers", ""])


def test_registry_metadata_is_passed_when_given():
    t = FakeTransport()
    NvnmBackend(t).add_registry("r", "d", '{"owner":"afritensor"}')
    assert t.calls[0][1][2] == '{"owner":"afritensor"}'


def test_our_abi_matches_the_documented_interface():
    """Guards against the ABI drifting away from what the precompile implements."""
    from zcastor.anchor.nvnm import ANCHORING_ABI, SELECTORS_VERIFIED
    expected = {
        "addRegistry": ["string", "string", "string"],
        "grantRole": ["uint64", "string", "address", "string"],
        "revokeRole": ["uint64", "string", "address", "string"],
        "updateRecordStatus": ["uint64", "uint64", "uint64", "string"],
    }
    for entry in ANCHORING_ABI:
        want = expected.get(entry["name"])
        if want:
            assert [i["type"] for i in entry["inputs"]] == want, entry["name"]
    assert SELECTORS_VERIFIED["addRegistry"] == "318b38b1"


def test_add_record_matches_the_deployed_struct():
    """
    Verified against calldata from calls that succeeded on chain. The
    documented struct leads with a `string registry` and has no registryId;
    that selector stopped being accepted in June 2026.
    """
    from zcastor.anchor.nvnm import ANCHORING_ABI, SELECTORS_VERIFIED
    entry = next(e for e in ANCHORING_ABI if e["name"] == "addRecord")
    assert len(entry["inputs"]) == 1
    fields = entry["inputs"][0]["components"]
    assert [f["name"] for f in fields] == [
        "uri", "checksum", "checksumAlgo", "metadata", "timestamp",
        "status", "recordId", "index", "isLatest", "registryId"]
    assert fields[-1]["type"] == "uint64"
    assert SELECTORS_VERIFIED["addRecord"] == "64d25295"


def test_grant_role_puts_the_checksum_second():
    """
    grantRole(uint64 registryId, string checksum, address account, string role).
    Omitting the checksum reverted six transactions; an empty one means
    registry-level scope.
    """
    t = FakeTransport()
    NvnmBackend(t).grant_role(42, "0xabc", "editor")
    assert t.calls[0] == ("grantRole", [42, "", "0xabc", "editor"])


def test_record_level_role_passes_a_checksum():
    t = FakeTransport()
    NvnmBackend(t).grant_role(42, "0xabc", "admin", checksum=CHECKSUM)
    assert t.calls[0][1][1] == CHECKSUM


def test_unknown_role_is_refused():
    try:
        NvnmBackend(FakeTransport()).grant_role(42, "0xabc", "superuser")
    except AnchorRejected:
        pass
    else:
        raise AssertionError("expected AnchorRejected")


def test_revoking_an_agent_is_one_call():
    t = FakeTransport()
    NvnmBackend(t).revoke_role(42, "0xabc")
    assert t.calls[0] == ("revokeRole", [42, "", "0xabc", "editor"])


# ── failure classification ────────────────────────────────────────────────────

def transient(exc):
    try:
        NvnmBackend(FakeTransport(raises=exc)).add_record(record())
    except AnchorUnavailable:
        return True
    except AnchorRejected:
        return False
    raise AssertionError("expected an exception")


def test_rpc_timeout_is_transient():
    assert transient(TimeoutError("read timed out"))


def test_connection_error_is_transient():
    assert transient(ConnectionError("connection refused"))


def test_an_unrecognised_failure_is_treated_as_transient():
    """Erring here costs a retry. Erring the other way loses a record."""
    assert transient(RuntimeError("something nobody has seen before"))


def test_a_missing_role_is_permanent():
    assert not transient(Exception("execution reverted: caller lacks editor role"))


def test_an_unknown_registry_is_permanent():
    assert not transient(Exception("registry not found: afritensor-typo"))


# ── outbox integration ────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_the_outbox_retries_a_halted_chain_and_parks_a_bad_role():
    with tempfile.TemporaryDirectory() as d:
        clock = Clock()
        box = Outbox(Path(d) / "outbox.jsonl", clock=clock)
        box.enqueue(op="add_record", registry="afritensor-dossiers",
                    registry_id=42, checksum=CHECKSUM,
                    uri="https://example.test/x.json")

        halted = NvnmBackend(FakeTransport(raises=ConnectionError("chain halted")))
        box.drain(halted)
        assert len(box.pending) == 1, "a halt must never lose a record"

        clock.advance(60)      # past the backoff window
        rejected = NvnmBackend(FakeTransport(
            raises=Exception("caller lacks editor role")))
        box.drain(rejected)
        assert box.stats()["parked"] == 1


def test_a_bad_checksum_parks_rather_than_looping():
    with tempfile.TemporaryDirectory() as d:
        box = Outbox(Path(d) / "outbox.jsonl")
        # Bypass the outbox's own stripping to simulate a malformed entry.
        entry = box.enqueue(op="add_record", registry="r", registry_id=42,
                            checksum=CHECKSUM)
        entry.checksum = "tooshort"
        box.drain(NvnmBackend(FakeTransport()))
        assert box.stats()["parked"] == 1


# ── definitions ───────────────────────────────────────────────────────────────

def test_four_registries_are_defined_with_descriptions():
    assert set(REGISTRIES) == {"afritensor-policy", "afritensor-decisions",
                               "afritensor-dossiers", "afritensor-archive"}
    assert all(v for v in REGISTRIES.values())


def test_every_agent_writes_only_to_defined_registries():
    for agent, registries in AGENT_ROLES.items():
        for r in registries:
            assert r in REGISTRIES, f"{agent} references unknown registry {r}"


def test_no_agent_can_write_to_the_policy_registry():
    """Policy is published by the operator, not by an agent that follows it."""
    for registries in AGENT_ROLES.values():
        assert "afritensor-policy" not in registries


def test_no_agent_can_write_to_the_archive():
    """History is not something a running agent gets to append to."""
    for registries in AGENT_ROLES.values():
        assert "afritensor-archive" not in registries


# ── receipt decoding ──────────────────────────────────────────────────────────

def receipt(data_hex, topic=None):
    from zcastor.anchor.nvnm import REGISTRY_ADDED_TOPIC
    return {"logs": [{"topics": [topic or REGISTRY_ADDED_TOPIC, "0x5501"],
                      "data": data_hex}]}


def test_registry_id_is_decoded_from_the_receipt():
    """The chain assigns it and returns it nowhere else."""
    from zcastor.anchor.nvnm import registry_id_from_receipt
    assert registry_id_from_receipt(
        receipt("0" * 60 + "10c8" + "0" * 128)) == 4296


def test_a_leading_0x_does_not_shift_the_decode():
    """
    Slicing from index 2 on data without a 0x prefix multiplied every id by
    256 and produced plausible numbers rather than an error.
    """
    from zcastor.anchor.nvnm import registry_id_from_receipt
    bare = "0" * 60 + "10c8" + "0" * 128
    assert registry_id_from_receipt(receipt(bare)) == 4296
    assert registry_id_from_receipt(receipt("0x" + bare)) == 4296


def test_unrelated_logs_are_ignored():
    from zcastor.anchor.nvnm import registry_id_from_receipt
    assert registry_id_from_receipt(receipt("0" * 64, topic="0xdeadbeef")) == 0


def test_a_receipt_with_no_logs_yields_zero():
    from zcastor.anchor.nvnm import registry_id_from_receipt
    assert registry_id_from_receipt({"logs": []}) == 0


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
