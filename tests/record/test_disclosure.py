"""
Visibility and disclosure tests.

The one that carries the design is test_the_recipient_can_verify_without_us:
a private record's document never went to the world, but the counterparty who
receives it can still check it against a hash anchored before the outcome
existed.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.anchor.index import AnchorIndex  # noqa: E402
from zcastor.record.canonical import checksum_bytes  # noqa: E402
from zcastor.record.disclosure import Disclosure, DisclosureLog  # noqa: E402
from zcastor.record.publisher import Publisher  # noqa: E402

sys.path.insert(0, str(ROOT / "tests" / "record"))
from test_publishing import dossier  # noqa: E402

URI = "https://raw.example.test/{path}"
AT = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


# ── visibility ────────────────────────────────────────────────────────────────

def test_public_mode_produces_a_fetchable_url():
    with tempfile.TemporaryDirectory() as d:
        pub = Publisher(Path(d) / "pub", uri_template=URI, visibility="public")
        out = pub.publish_dossier(dossier(), day="2026-08-29")
        assert out["uri"].startswith("https://")


def test_private_mode_names_the_custodian_instead():
    """
    An empty URI says nothing. `held://` states that a document exists, who
    holds it, and that it can be requested — a reader can tell withheld from
    never recorded.
    """
    with tempfile.TemporaryDirectory() as d:
        pub = Publisher(Path(d) / "priv", uri_template=URI,
                        visibility="private", holder="afritensor")
        out = pub.publish_dossier(dossier(), day="2026-08-29")
        assert out["uri"].startswith("held://afritensor/")
        assert "http" not in out["uri"]


def test_private_mode_ignores_the_public_template():
    with tempfile.TemporaryDirectory() as d:
        pub = Publisher(Path(d) / "priv", uri_template=URI, visibility="private")
        assert "raw.example.test" not in pub.uri_for("trades/x.json")


def test_the_checksum_is_identical_in_either_mode():
    """Visibility changes where a document lives, never what it hashes to."""
    with tempfile.TemporaryDirectory() as d:
        a = Publisher(Path(d) / "pub", uri_template=URI, visibility="public")
        b = Publisher(Path(d) / "priv", visibility="private")
        assert (a.publish_dossier(dossier(), day="2026-08-29")["checksum"]
                == b.publish_dossier(dossier(), day="2026-08-29")["checksum"])


def test_an_unknown_visibility_is_refused():
    try:
        Publisher("/tmp/x", visibility="semi-public")
    except ValueError as e:
        assert "visibility" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ── disclosure ────────────────────────────────────────────────────────────────

def setup(tmp):
    pub = Publisher(Path(tmp) / "priv", visibility="private")
    published = pub.publish_dossier(dossier(), day="2026-08-29")

    idx = AnchorIndex(Path(tmp) / "index.jsonl")
    idx.record_anchored(signal_id="sig_1", registry="afritensor-dossiers",
                        registry_id=4298, record_id=7,
                        checksum=published["checksum"], transaction="0xabc",
                        status="Active", uri=published["uri"],
                        environment="testnet")

    log = DisclosureLog(Path(tmp) / "disclosures.jsonl")
    disc = Disclosure(publisher=pub, anchor_index=idx, log=log,
                      environment="testnet", registry="afritensor-dossiers")
    return pub, idx, log, disc, published


def test_the_recipient_can_verify_without_us():
    with tempfile.TemporaryDirectory() as d:
        pub, idx, log, disc, published = setup(d)
        dest = Path(d) / "bundle"
        result = disc.build(recipient="Orion Partners",
                            paths=[published["path"]], destination=dest,
                            agreement="NDA 2026-14", now=AT)

        assert result["included"] == 1
        copied = next((dest / "records").iterdir())
        # The bundle's file hashes to the value anchored on chain.
        assert checksum_bytes(copied.read_bytes()) == published["checksum"]

        manifest = json.loads((dest / "manifest.json").read_text())
        record = manifest["records"][0]
        assert record["transaction"] == "0xabc"
        assert record["registry_id"] == 4298
        assert record["environment"] == "testnet"


def test_documents_are_copied_byte_for_byte():
    """Re-serialising would change the hash and fail the recipient's first check."""
    with tempfile.TemporaryDirectory() as d:
        pub, _, _, disc, published = setup(d)
        dest = Path(d) / "bundle"
        disc.build(recipient="Orion", paths=[published["path"]],
                   destination=dest, now=AT)
        original = (pub.root / published["path"]).read_bytes()
        copied = next((dest / "records").iterdir()).read_bytes()
        assert copied == original


def test_verify_instructions_are_written_for_a_stranger():
    with tempfile.TemporaryDirectory() as d:
        _, _, _, disc, published = setup(d)
        dest = Path(d) / "bundle"
        disc.build(recipient="Orion Partners", paths=[published["path"]],
                   destination=dest, agreement="NDA 2026-14", now=AT)
        text = (dest / "VERIFY.md").read_text()
        assert "Orion Partners" in text
        assert "sha256sum" in text
        assert "NDA 2026-14" in text
        # It must be honest about what the proof does not cover.
        assert "does not prove" in text


def test_an_unanchored_record_says_so():
    """Better an explicit gap than an implied proof that does not exist."""
    with tempfile.TemporaryDirectory() as d:
        pub = Publisher(Path(d) / "priv", visibility="private")
        published = pub.publish_dossier(dossier(), day="2026-08-29")
        disc = Disclosure(publisher=pub,
                          anchor_index=AnchorIndex(Path(d) / "empty.jsonl"))
        dest = Path(d) / "bundle"
        disc.build(recipient="Orion", paths=[published["path"]],
                   destination=dest, now=AT)
        manifest = json.loads((dest / "manifest.json").read_text())
        assert manifest["records"][0]["anchored"] is False
        assert "not yet anchored" in (dest / "VERIFY.md").read_text()


def test_a_missing_file_is_reported_not_silently_dropped():
    with tempfile.TemporaryDirectory() as d:
        _, _, _, disc, published = setup(d)
        dest = Path(d) / "bundle"
        result = disc.build(recipient="Orion",
                            paths=[published["path"], "trades/gone.json"],
                            destination=dest, now=AT)
        assert result["included"] == 1
        assert result["missing"] == ["trades/gone.json"]


# ── the disclosure log ────────────────────────────────────────────────────────

def test_every_disclosure_is_logged():
    """'Who have you shown this to' should not depend on our memory."""
    with tempfile.TemporaryDirectory() as d:
        _, _, log, disc, published = setup(d)
        disc.build(recipient="Orion Partners", paths=[published["path"]],
                   destination=Path(d) / "b1", agreement="NDA 2026-14", now=AT)
        disc.build(recipient="Vega Capital", paths=[published["path"]],
                   destination=Path(d) / "b2", agreement="NDA 2026-22", now=AT)

        entries = log.entries()
        assert [e["recipient"] for e in entries] == ["Orion Partners",
                                                     "Vega Capital"]
        assert entries[0]["agreement"] == "NDA 2026-14"
        assert entries[0]["checksums"] == [published["checksum"]]


def test_the_log_can_be_queried_per_recipient():
    with tempfile.TemporaryDirectory() as d:
        _, _, log, disc, published = setup(d)
        for name in ("Orion", "Vega", "Orion"):
            disc.build(recipient=name, paths=[published["path"]],
                       destination=Path(d) / f"b-{name}-{id(name)}", now=AT)
        assert len(log.for_recipient("Orion")) == 2
        assert len(log.for_recipient("Vega")) == 1


def test_the_log_survives_a_restart():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "disclosures.jsonl"
        DisclosureLog(path).append({"recipient": "Orion", "count": 1})
        assert len(DisclosureLog(path).entries()) == 1


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
