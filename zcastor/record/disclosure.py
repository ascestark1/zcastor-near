"""
Disclosure — releasing a privately held record to a named counterparty.

Private records anchor their checksum on chain and keep the document on our
infrastructure. Disclosure hands that document to one named party, who then
verifies exactly as a public reader would: hash the file, compare to the record.
The chain never had to hold the content for the proof to work.

Two properties this preserves that emailing a PDF does not.

**The recipient can verify without trusting us.** The checksum was anchored
before they ever saw the document, by an identified agent, at a timestamp
neither party controls. If we had altered anything after the fact the hash would
not match, and they can check that in one command.

**Disclosure is itself recorded.** Every bundle appends to a disclosure log: who
received what, when, and under which agreement. That log is hashable and
anchorable like anything else, so "who have you shown this to" has an answer
that does not depend on our memory or our goodwill. For a desk operating under
NDA that is not bureaucracy, it is the thing that makes selective disclosure
defensible.

The bundle is a directory, not a database export:

    disclosure-2026-08-29-orionpartners/
    ├── VERIFY.md              instructions, written for a stranger
    ├── manifest.json          what is included, with checksums and record refs
    └── records/…              the documents themselves, byte-identical
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .canonical import checksum_bytes

logger = logging.getLogger("zcastor.record.disclosure")

VERIFY_TEMPLATE = """# Verifying these records

Prepared for **{recipient}** on {date} by {holder}.

Each document below was hashed and the hash was written to NVNM Chain
(`{environment}`) at the time the decision was made, before any outcome was
known. Verification does not require our cooperation, our systems, or our
continued existence.

## What to check

For each entry in `manifest.json`:

**1. The document hashes to the value we claim.**

```
sha256sum records/<file>
```

Compare to the `checksum` field. If it matches, the file is byte-identical to
what we hashed.

**2. That hash is on chain, and predates the outcome.**

Look up the record by checksum in registry `{registry}`. The block timestamp is
when it was anchored. Nothing we do afterwards can move it.

**3. The record was written by an authorised agent.**

The transaction sender is the agent that made the decision. Roles are granted at
the registry level and are visible on chain.

## What this does not prove

That our reasoning was sound, or that the outcome was good. It proves only that
the record you are reading is the record we committed to at the time, and that
we have not revised it since.

## Included

{summary}

---
{agreement_line}
"""


class DisclosureLog:
    """Append-only record of what was released, to whom, and when."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def for_recipient(self, recipient: str) -> list[dict[str, Any]]:
        return [e for e in self.entries() if e.get("recipient") == recipient]


class Disclosure:
    def __init__(
        self,
        *,
        publisher: Any,
        anchor_index: Any = None,
        log: Optional[DisclosureLog] = None,
        environment: str = "testnet",
        registry: str = "afritensor-decisions",
    ) -> None:
        self.publisher = publisher
        self.anchor_index = anchor_index
        self.log = log
        self.environment = environment
        self.registry = registry

    def build(
        self,
        *,
        recipient: str,
        paths: Iterable[str],
        destination: str | Path,
        agreement: str = "",
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """
        Assemble a bundle for one counterparty.

        `paths` are repo-relative document paths as returned by the publisher.
        Files are copied byte-for-byte: re-serialising would change the hash and
        make the recipient's first check fail.
        """
        now = now or datetime.now(timezone.utc)
        dest = Path(destination)
        (dest / "records").mkdir(parents=True, exist_ok=True)

        included, missing = [], []
        for relative in paths:
            source = self.publisher.root / relative
            if not source.exists():
                missing.append(relative)
                continue

            data = source.read_bytes()
            checksum = checksum_bytes(data)
            target = dest / "records" / Path(relative).name
            shutil.copyfile(source, target)

            entry = {"path": relative, "file": target.name,
                     "checksum": checksum}
            if self.anchor_index is not None:
                anchored = self.anchor_index.get_by_checksum(checksum)
                if anchored:
                    entry.update(
                        registry_id=anchored.get("registry_id"),
                        record_id=anchored.get("record_id"),
                        transaction=anchored.get("transaction"),
                        status=anchored.get("status"),
                        environment=anchored.get("environment"))
                else:
                    # Say so rather than implying a proof that does not exist.
                    entry["anchored"] = False
            included.append(entry)

        manifest = {
            "recipient": recipient,
            "disclosed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "holder": getattr(self.publisher, "holder", "afritensor"),
            "environment": self.environment,
            "registry": self.registry,
            "agreement": agreement,
            "records": included,
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        (dest / "VERIFY.md").write_text(self._verify_text(manifest))

        if self.log is not None:
            self.log.append({
                "recipient": recipient,
                "disclosed_at": manifest["disclosed_at"],
                "agreement": agreement,
                "checksums": [e["checksum"] for e in included],
                "count": len(included),
            })

        if missing:
            logger.warning("disclosure for %s omitted %d missing file(s): %s",
                           recipient, len(missing), missing)

        logger.info("disclosed %d record(s) to %s", len(included), recipient)
        return {"destination": str(dest), "included": len(included),
                "missing": missing, "manifest": manifest}

    def _verify_text(self, manifest: dict[str, Any]) -> str:
        lines = []
        for e in manifest["records"]:
            note = "" if e.get("transaction") else "  (not yet anchored)"
            lines.append(f"- `records/{e['file']}`{note}\n"
                         f"  - checksum `{e['checksum']}`")
        agreement = manifest.get("agreement")
        return VERIFY_TEMPLATE.format(
            recipient=manifest["recipient"],
            date=manifest["disclosed_at"][:10],
            holder=manifest["holder"],
            environment=manifest["environment"],
            registry=manifest["registry"],
            summary="\n".join(lines) or "- (none)",
            agreement_line=(f"Disclosed under {agreement}." if agreement
                            else "No agreement reference supplied."),
        )
