"""
Record layer tests.

Most of these are about determinism. A checksum that is stable in this process
but not across processes, machines or languages would pass a naive test and fail
the only thing that matters — a stranger recomputing it.
"""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.gates.base import GateContext, Verdict, allow, suppress, route  # noqa: E402
from zcastor.gates.base import Gate  # noqa: E402
from zcastor.gates.pipeline import GatePipeline  # noqa: E402
from zcastor.record import canonical, dgml, proof  # noqa: E402
from zcastor.record.decision_log import DecisionLog, merge_totals  # noqa: E402


def gate(name, decision, **flags):
    return type(f"G_{name}", (Gate,),
                {"name": name, "evaluate": lambda self, c: decision, **flags})()


def trace_for(gates):
    ctx = GateContext(signal={}, origin_hash="sha256:o", policy={})
    return GatePipeline(gates).run(ctx, "sig_1", "sha256:p")


# ── canonical form ────────────────────────────────────────────────────────────

def test_key_order_does_not_change_the_checksum():
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical.checksum(a) == canonical.checksum(b)


def test_checksum_is_stable_across_processes():
    """The real requirement: a different interpreter must agree."""
    payload = '{"price":62850.5,"symbol":"BTCUSD","tags":["a","b"]}'
    code = (
        "import json,sys; sys.path.insert(0,%r);"
        "from zcastor.record import canonical;"
        "print(canonical.checksum(json.loads(%r)))" % (str(ROOT), payload)
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True)
    import json
    assert out.stdout.strip() == canonical.checksum(json.loads(payload))


def test_published_bytes_are_the_hashed_bytes():
    """
    The audit loop, end to end: what we write is what we hash, and `sha256sum`
    on the downloaded file reproduces the anchored value with no knowledge of
    our serialiser.
    """
    value = {"signal_id": "sig_1", "price": 62850.5, "city": "Zürich"}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "dossier.json"
        digest = canonical.write_canonical(path, value)
        raw = path.read_bytes()
        assert digest == "sha256:" + hashlib.sha256(raw).hexdigest()
        assert canonical.verify(path, digest)


def test_unicode_is_published_unescaped():
    data = canonical.canonical_bytes({"city": "Zürich"})
    assert "Zürich".encode() in data
    assert b"\\u00fc" not in data


def test_nan_is_rejected_not_serialised():
    try:
        canonical.canonical_bytes({"pnl": float("nan")})
    except canonical.CanonicalError as e:
        assert "non-finite" in str(e)
    else:
        raise AssertionError("NaN must not reach a published file")


def test_infinity_is_rejected():
    try:
        canonical.canonical_bytes({"rr": float("inf")})
    except canonical.CanonicalError:
        pass
    else:
        raise AssertionError("expected CanonicalError")


def test_non_string_key_is_rejected():
    try:
        canonical.canonical_bytes({1: "a"})
    except canonical.CanonicalError:
        pass
    else:
        raise AssertionError("expected CanonicalError")


def test_bare_strips_the_prefix_for_the_chain():
    d = canonical.checksum({"a": 1})
    assert canonical.bare(d) == d[7:]
    assert len(canonical.bare(d)) == 64


def test_tampering_is_detected():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "dossier.json"
        digest = canonical.write_canonical(path, {"outcome": "loss"})
        path.write_bytes(path.read_bytes().replace(b"loss", b"win!"))
        assert not canonical.verify(path, digest)


# ── proofs ────────────────────────────────────────────────────────────────────

def test_origin_hashes_the_payload_as_received():
    payload = {"direction": "UP", "confidence": "high", "entry_price": 62850.0}
    assert proof.origin_hash(payload) == canonical.checksum(payload)


def test_same_origin_different_policy_gives_a_different_process_hash():
    """Two identical signals may legitimately decide differently."""
    p1 = proof.process_hash({"min_risk_reward": 1.0}, code_version="abc123")
    p2 = proof.process_hash({"min_risk_reward": 1.5}, code_version="abc123")
    assert p1 != p2


def test_code_version_changes_the_process_hash():
    a = proof.process_hash({"x": 1}, code_version="abc123")
    b = proof.process_hash({"x": 1}, code_version="def456")
    assert a != b


def test_unknown_code_version_is_recorded_as_unknown():
    a = proof.process_hash({"x": 1}, code_version=None)
    b = proof.process_hash({"x": 1}, code_version="unknown")
    assert a == b, "absence must be explicit, not a plausible placeholder"


def test_state_hash_distinguishes_market_conditions():
    base = dict(tick_time=1.0, ask=62860.0, bid=62840.0, spread_pts=20)
    wide = dict(tick_time=1.0, ask=62900.0, bid=62800.0, spread_pts=100)
    assert proof.state_hash(**base) != proof.state_hash(**wide)


def test_state_hash_does_not_leak_the_balance():
    """The hash proves conditions held; the dossier never carries the number."""
    h = proof.state_hash(tick_time=1.0, ask=1.0, bid=1.0, spread_pts=0,
                         balance=1234.56)
    assert "1234" not in h


def test_build_produces_all_three_proofs():
    ps = proof.build(
        payload={"direction": "UP"},
        policy={"min_risk_reward": 1.0},
        state=dict(tick_time=1.0, ask=2.0, bid=1.0, spread_pts=1),
        code_version="abc",
    )
    assert len({ps.origin, ps.process, ps.state}) == 3
    assert set(ps.to_dict()) == {"origin", "process", "state"}


# ── dossier ───────────────────────────────────────────────────────────────────

def sample_dossier(**over):
    ps = proof.ProofSet(origin="sha256:o", process="sha256:p", state="sha256:s")
    t = trace_for([gate("zone", route("exhaustion"))])
    kw = dict(
        signal_id="sig_1",
        emitted_at="2026-08-27T09:00:00Z",
        agent="execution",
        proofs=ps,
        decision=dgml.decision_from_trace(t),
        thesis={"direction": "UP", "target": 63200.0, "stop": 62660.0},
        policy_version="2.0.0-dev",
    )
    kw.update(over)
    return dgml.build_dossier(**kw)


def test_dossier_checksum_ignores_provenance():
    """
    Provenance is written after anchoring. If it were inside the checksum, the
    anchored value would stop matching the published file the moment the
    transaction landed.
    """
    d = sample_dossier()
    before = dgml.dossier_checksum(d)
    d["provenance"] = {"chain": "nvnm", "transaction": "0xabc", "status": "committed"}
    assert dgml.dossier_checksum(d) == before


def test_dossier_content_change_does_change_the_checksum():
    a = dgml.dossier_checksum(sample_dossier())
    b = dgml.dossier_checksum(sample_dossier(thesis={"direction": "DOWN"}))
    assert a != b


def test_unresolved_dossier_says_so_explicitly():
    d = sample_dossier()
    assert d["outcome"] is None
    assert d["execution"] is None
    assert d["provenance"]["status"] == "unanchored"


def test_dossier_carries_no_account_state():
    raw = canonical.canonical_bytes(sample_dossier())
    for leak in (b"balance", b"free_margin", b"password", b"login"):
        assert leak not in raw


def test_decision_block_records_shadow_objections():
    t = trace_for([
        gate("session", suppress("session_blocked")),
        gate("range", suppress("range_below_sl")),
    ])
    block = dgml.decision_from_trace(t)
    assert block["reason"] == "session_blocked"
    assert block["bound_at"] == "session"
    assert block["also_objected"] == ["range"]
    assert len(block["gates"]) == 2


# ── decision log ──────────────────────────────────────────────────────────────

def log_with_entries():
    log = DecisionLog("2026-08-27")
    cases = [
        ("sig_1", [gate("a", allow())]),
        ("sig_2", [gate("session", suppress("session_blocked"))]),
        ("sig_3", [gate("session", suppress("session_blocked"))]),
        ("sig_4", [gate("london", suppress("london_sell_bias"))]),
        ("sig_5", [gate("zone", route("exhaustion"))]),
    ]
    for sid, gates in cases:
        log.add(signal_id=sid, at="2026-08-27T09:00:00Z", trace=trace_for(gates),
                origin="sha256:o", process="sha256:p", state="sha256:s")
    return log


def test_log_counts_actions():
    assert log_with_entries().summary() == {"allow": 1, "route": 1, "suppress": 3}


def test_log_ranks_suppression_reasons():
    assert list(log_with_entries().reasons()) == ["session_blocked", "london_sell_bias"]


def test_rejects_are_not_logged_as_refusals():
    """A malformed payload is not a signal the system declined."""
    from zcastor.gates.base import reject
    log = DecisionLog("2026-08-27")
    log.add(signal_id="sig_x", at="t", trace=trace_for([gate("d", reject("bad"))]),
            origin="o", process="p", state="s")
    assert log.entries == []


def test_log_is_deterministic():
    a = log_with_entries().checksum(policy_version="2.0.0-dev")
    b = log_with_entries().checksum(policy_version="2.0.0-dev")
    assert a == b


def test_merge_totals_across_days():
    logs = [log_with_entries().build(), log_with_entries().build()]
    assert merge_totals(logs) == {"allow": 2, "route": 2, "suppress": 6}


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
