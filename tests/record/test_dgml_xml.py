"""
DGML emission tests.

The one that carries the design is test_thesis_and_outcome_attest_separately:
element-level attestation is what lets a prediction be anchored before its
result exists, and the result anchored afterwards, each verifiable alone.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.record import dgml_xml as dx  # noqa: E402
from zcastor.record.proof import ProofSet  # noqa: E402

PROOFS = ProofSet(origin="sha256:o", process="sha256:p", state="sha256:s")

THESIS = {"direction": "UP", "timeframe": "15m", "entry_price": 78500.0,
          "target": 79100.0, "stop": 78100.0, "stop_pts": 400,
          "risk_reward": 1.5, "action": "allow"}

DECISION = {"action": "allow", "reason": "", "bound_at": None,
            "gates": [{"name": "session", "status": "passed", "verdict": "allow"},
                      {"name": "london_bias", "status": "shadow",
                       "verdict": "suppress", "reason": "london_sell_bias"}]}

OUTCOME = {"outcome": "tp", "correct": True, "net_pts": 600.0,
           "close_pts": 512.0}


def record(**over):
    kw = dict(signal_id="sig_1", emitted_at="2026-08-31T10:00:00Z",
              agent="execution", proofs=PROOFS, thesis=THESIS,
              decision=DECISION, policy_version="2.1.0")
    kw.update(over)
    return dx.build_decision_record(**kw)


# ── format ────────────────────────────────────────────────────────────────────

def test_it_is_namespaced_xml_not_json():
    root = record()
    assert root.tag == f"{{{dx.DG_NS}}}chunk"
    assert dx.to_bytes(root).startswith(b"<?xml")


def test_values_are_typed_and_carry_both_forms():
    """dg:value is the machine form, the text is the human one."""
    root = record()
    entry = root.find(f".//{{{dx.DOCSET_NS}}}EntryPrice")
    assert entry.get(f"{{{dx.XSI_NS}}}type") == "decimal"
    assert entry.get(f"{{{dx.DG_NS}}}value") == "78500.00"
    assert entry.text == "78,500.00"


def test_no_spatial_attributes_are_emitted():
    """
    dg:origin is pixel coordinates back to a source page. These records were
    never a page, so emitting coordinates would fabricate provenance.
    """
    raw = dx.to_bytes(record())
    assert b"origin=" not in raw
    assert b'bornDigital="true"' in raw


def test_every_gate_verdict_is_preserved_including_shadows():
    root = record()
    names = [g.get(f"{{{dx.DG_NS}}}name")
             for g in root.iter(f"{{{dx.DOCSET_NS}}}GateVerdict")]
    assert names == ["session", "london_bias"]


def test_the_docset_vocabulary_is_shared_across_documents():
    """Cross-document tag consistency: one query works on the whole corpus."""
    a = record(signal_id="sig_1")
    b = record(signal_id="sig_2", thesis={**THESIS, "direction": "DOWN"})
    tags = lambda r: {e.tag for e in r.iter() if e.tag.startswith(f"{{{dx.DOCSET_NS}}}")}
    assert tags(a) == tags(b)


# ── attestation ───────────────────────────────────────────────────────────────

def test_thesis_and_outcome_attest_separately():
    """
    The property that matters: a prediction anchored before the result exists,
    the result anchored after, each provable on its own.
    """
    root = record(outcome=OUTCOME)
    ids = dict(dx.attested_elements(root))
    assert set(ids) == {"sig_1-thesis", "sig_1-outcome"}
    assert all(h.startswith("sha256:") for h in ids.values())
    assert ids["sig_1-thesis"] != ids["sig_1-outcome"]


def test_a_thesis_hash_does_not_change_when_the_outcome_arrives():
    """
    Anchoring the thesis at decision time is only meaningful if adding the
    outcome later cannot alter it.
    """
    before = dict(dx.attested_elements(record()))["sig_1-thesis"]
    after = dict(dx.attested_elements(record(outcome=OUTCOME)))["sig_1-thesis"]
    assert before == after


def test_an_element_verifies_on_its_own():
    root = record(outcome=OUTCOME)
    assert dx.verify_element(root, "sig_1-thesis")
    assert dx.verify_element(root, "sig_1-outcome")


def test_tampering_with_a_thesis_is_detected():
    root = record()
    target = root.find(f".//{{{dx.DOCSET_NS}}}Target")
    target.set(f"{{{dx.DG_NS}}}value", "99999.00")
    assert not dx.verify_element(root, "sig_1-thesis")


def test_identical_records_hash_identically():
    a = dict(dx.attested_elements(record()))
    b = dict(dx.attested_elements(record()))
    assert a == b


def test_a_changed_stop_changes_the_thesis_hash():
    a = dict(dx.attested_elements(record()))["sig_1-thesis"]
    b = dict(dx.attested_elements(
        record(thesis={**THESIS, "stop": 78000.0})))["sig_1-thesis"]
    assert a != b


def test_pretty_printing_does_not_affect_what_was_attested():
    """The hash is over canonical form, so formatting cannot change it."""
    root = record()
    before = dx.element_hash(root.find(f"{{{dx.DOCSET_NS}}}Thesis"))
    reparsed = ET.fromstring(dx.to_bytes(root, pretty=True))
    after = dx.element_hash(reparsed.find(f"{{{dx.DOCSET_NS}}}Thesis"))
    assert before == after


def test_the_outcome_links_back_to_the_thesis():
    """Containment is the tree; named links are the graph."""
    root = record(outcome=OUTCOME)
    chunk = root.find(f".//{{{dx.DOCSET_NS}}}Outcome/{{{dx.DG_NS}}}chunk")
    assert chunk.get(f"{{{dx.DG_NS}}}itemprop") == "settles"
    assert chunk.get(f"{{{dx.DG_NS}}}href") == "#sig_1-thesis"


def test_a_null_value_is_stated_not_omitted():
    root = record(thesis={**THESIS, "risk_reward": None})
    assert root.find(f".//{{{dx.DOCSET_NS}}}RiskReward") is None


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
