"""
DGML emission — semantic XML, not JSON.

DGML (Document Graph Markup Language) is an open standard from Docugami and
Inveniam: semantic XML where tags name what a thing IS in its domain, values are
typed, and any element subtree can be hashed and anchored independently.

Our earlier records were JSON that borrowed the name. This produces the format.

Four properties of the standard, and how we use them:

**Cross-document tag consistency.** Every decision we emit shares one docset
vocabulary, so `dec:StopDistance` means the same thing in the first record and
the ten-thousandth. A reader who can query one can query the corpus.

**Complete semantic preservation.** We emit every gate verdict, including the
ones that did not bind, rather than choosing fields now and regretting it later.
A question nobody has asked yet is answerable from the same file.

**Element-level attestation.** This is the one that changes our design. A
subtree can be hashed and anchored on its own, so the THESIS can be anchored at
decision time and the OUTCOME anchored separately once price has spoken —
proving the prediction preceded the result without needing two documents or a
status flag. Each element carries `dg:hash` over its own canonical form.

**No spatial layer, deliberately.** `dg:origin` encodes pixel coordinates back
to a source page. Our records are born digital: there is no page, no scan, no
region to point at. Emitting coordinates would be fabricating provenance for
something that never had it, so the attribute is omitted entirely. This is the
open question we are putting to the DGML maintainers — how born-digital records
should be represented in a format whose spatial layer assumes extraction.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from typing import Any, Mapping, Optional, Sequence
from xml.dom import minidom

DG_NS = "http://dgml.io/ns/dg#"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
DOCSET_NS = "http://www.afritensor.com/decisions#"
DOCSET_PREFIX = "dec"

SCHEMA_VERSION = "1.0.0-draft"

ET.register_namespace("dg", DG_NS)
ET.register_namespace("xsi", XSI_NS)
ET.register_namespace(DOCSET_PREFIX, DOCSET_NS)


def _dg(name: str) -> str:
    return f"{{{DG_NS}}}{name}"


def _xsi(name: str) -> str:
    return f"{{{XSI_NS}}}{name}"


def _tag(name: str) -> str:
    return f"{{{DOCSET_NS}}}{name}"


# ── canonical form and hashing ────────────────────────────────────────────────

def canonical(element: ET.Element) -> bytes:
    """
    C14N 2.0 canonical form of one element.

    Attribute order, namespace declarations and whitespace all vary between
    serialisers, so hashing raw XML would produce different digests for
    identical documents. Canonicalisation is what makes an element hash mean
    anything to a third party.

    `strip_text` matters more than it looks. We publish an indented file
    because DGML's fourth layer is that a person can open and read it — but
    indentation is whitespace inside elements, so hashing it would mean the
    pretty file and the compact one attest differently. Stripping surrounding
    whitespace makes the hash a property of the CONTENT, so a verifier gets the
    same answer from whichever form they were handed.
    """
    return ET.canonicalize(ET.tostring(element, encoding="unicode"),
                           strip_text=True).encode("utf-8")


def element_hash(element: ET.Element) -> str:
    """
    sha256 over an element's canonical form, EXCLUDING its own dg:hash.

    Self-exclusion is required: an element cannot contain a hash of itself. The
    attribute is stripped from a copy before digesting, so a verifier does the
    same and gets the same answer.
    """
    clone = _strip_hash(element)
    return "sha256:" + hashlib.sha256(canonical(clone)).hexdigest()


def _strip_hash(element: ET.Element) -> ET.Element:
    clone = ET.fromstring(ET.tostring(element, encoding="unicode"))
    for node in clone.iter():
        node.attrib.pop(_dg("hash"), None)
    return clone


def seal(element: ET.Element) -> str:
    """Attach dg:hash to an element and return it. Depth-first, so children are
    sealed before the parent that contains them."""
    for child in list(element):
        if child.get(_dg("attest")) == "true":
            seal(child)
    digest = element_hash(element)
    element.set(_dg("hash"), digest)
    return digest


# ── building blocks ───────────────────────────────────────────────────────────

def value(parent: ET.Element, name: str, raw: Any,
          kind: Optional[str] = None) -> ET.Element:
    """
    A typed value element.

    `dg:value` carries the machine form and the text carries the human form.
    They are allowed to differ — 8500.00 and "$8,500" — which is the point:
    one file serves an LLM, a browser and an auditor.
    """
    el = ET.SubElement(parent, _tag(name))
    if raw is None:
        el.set(_dg("null"), "true")
        return el

    if kind is None:
        kind = ("boolean" if isinstance(raw, bool)
                else "integer" if isinstance(raw, int)
                else "decimal" if isinstance(raw, float)
                else "string")

    el.set(_xsi("type"), kind)
    if kind == "decimal":
        el.set(_dg("value"), f"{float(raw):.2f}")
        el.text = f"{float(raw):,.2f}"
    elif kind == "boolean":
        el.set(_dg("value"), "true" if raw else "false")
        el.text = "yes" if raw else "no"
    else:
        el.set(_dg("value"), str(raw))
        el.text = str(raw)
    return el


def link(parent: ET.Element, itemprop: str, href: str,
         text: str = "") -> ET.Element:
    """
    A named relationship to another element or document.

    The XML tree carries containment; these carry the graph. An outcome points
    at the thesis it settles, a dossier points at the policy that produced it.
    """
    el = ET.SubElement(parent, _dg("chunk"))
    el.set(_dg("itemprop"), itemprop)
    el.set(_dg("href"), href)
    if text:
        el.text = text
    return el


def attestable(parent: ET.Element, name: str, **attrs: str) -> ET.Element:
    """A subtree intended to be hashed and anchored on its own."""
    el = ET.SubElement(parent, _tag(name))
    el.set(_dg("attest"), "true")
    for k, v in attrs.items():
        el.set(k, v)
    return el


# ── documents ─────────────────────────────────────────────────────────────────

def build_decision_record(
    *,
    signal_id: str,
    emitted_at: str,
    agent: str,
    proofs: Any,
    thesis: Mapping[str, Any],
    decision: Mapping[str, Any],
    execution: Optional[Mapping[str, Any]] = None,
    outcome: Optional[Mapping[str, Any]] = None,
    policy_version: str = "",
    dashboard_version: str = "",
    code_version: str = "",
) -> ET.Element:
    """
    One decision as a DGML document.

    Thesis and Outcome are separately attestable, which is what lets the
    prediction be anchored before the result exists and the result be anchored
    afterwards, each verifiable alone.
    """
    root = ET.Element(_dg("chunk"))
    root.set(_dg("schema"), "afritensor/decisions")
    root.set(_dg("schemaVersion"), SCHEMA_VERSION)
    root.set(_dg("id"), signal_id)
    # Stated rather than implied: this record was never a page.
    root.set(_dg("bornDigital"), "true")

    identity = ET.SubElement(root, _tag("Identity"))
    value(identity, "SignalId", signal_id)
    value(identity, "EmittedAt", emitted_at, "dateTime")
    value(identity, "Agent", agent)

    proof = ET.SubElement(root, _tag("Proof"))
    value(proof, "OriginHash", proofs.origin)
    value(proof, "ProcessHash", proofs.process)
    value(proof, "StateHash", proofs.state)

    versions = ET.SubElement(root, _tag("Versions"))
    value(versions, "PolicyVersion", policy_version)
    value(versions, "DashboardVersion", dashboard_version)
    value(versions, "CodeVersion", code_version)

    # ── the falsifiable claim, made before the outcome existed ──
    claim = attestable(root, "Thesis")
    claim.set(_dg("id"), f"{signal_id}-thesis")
    for name, key, kind in [
        ("Direction", "direction", "string"),
        ("Timeframe", "timeframe", "string"),
        ("EntryPrice", "entry_price", "decimal"),
        ("Target", "target", "decimal"),
        ("Stop", "stop", "decimal"),
        ("StopDistance", "stop_pts", "integer"),
        ("RiskReward", "risk_reward", "decimal"),
        ("Action", "action", "string"),
        ("RouteKind", "route_kind", "string"),
    ]:
        if thesis.get(key) is not None:
            value(claim, name, thesis[key], kind)

    # ── every gate verdict, including those that did not bind ──
    verdicts = ET.SubElement(root, _tag("Decision"))
    value(verdicts, "Action", decision.get("action", ""))
    value(verdicts, "Reason", decision.get("reason", ""))
    value(verdicts, "BoundAt", decision.get("bound_at") or "")
    gates = ET.SubElement(verdicts, _tag("GateVerdicts"))
    for gate in decision.get("gates", []):
        el = ET.SubElement(gates, _tag("GateVerdict"))
        el.set(_dg("name"), str(gate.get("name", "")))
        value(el, "Status", gate.get("status", ""))
        value(el, "Verdict", gate.get("verdict", ""))
        if gate.get("reason"):
            value(el, "Reason", gate["reason"])

    if execution:
        ex = ET.SubElement(root, _tag("Execution"))
        value(ex, "Ticket", execution.get("ticket"), "integer")
        value(ex, "Side", execution.get("side", ""))
        value(ex, "FillPrice", execution.get("price"), "decimal")

    # ── the result, attested separately and pointing back at the claim ──
    if outcome:
        result = attestable(root, "Outcome")
        result.set(_dg("id"), f"{signal_id}-outcome")
        link(result, "settles", f"#{signal_id}-thesis", "the thesis above")
        value(result, "Result", outcome.get("outcome", ""))
        value(result, "DirectionCorrect", bool(outcome.get("correct")), "boolean")
        value(result, "NetPoints", outcome.get("net_pts"), "decimal")
        value(result, "ClosePoints", outcome.get("close_pts"), "decimal")

    seal(root)
    return root


def to_bytes(root: ET.Element, pretty: bool = True) -> bytes:
    """
    Serialise for publication.

    Pretty-printed on purpose: DGML's fourth layer is that a person can open
    the file and read it. The hash is over the canonical form, not this, so
    formatting cannot change what was attested.
    """
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if not pretty:
        return raw
    parsed = minidom.parseString(raw)
    return parsed.toprettyxml(indent="  ", encoding="utf-8")


def attested_elements(root: ET.Element) -> list[tuple[str, str]]:
    """
    Every independently anchorable subtree, as (id, hash).

    These are what go on chain. A counterparty can be given one element and its
    anchor without ever seeing the rest of the document.
    """
    out = []
    for el in root.iter():
        if el.get(_dg("attest")) == "true":
            out.append((el.get(_dg("id"), ""), el.get(_dg("hash"), "")))
    return out


def verify_element(root: ET.Element, element_id: str) -> bool:
    """Recompute one element's hash — the check a recipient performs."""
    for el in root.iter():
        if el.get(_dg("id")) == element_id:
            return el.get(_dg("hash")) == element_hash(el)
    return False
