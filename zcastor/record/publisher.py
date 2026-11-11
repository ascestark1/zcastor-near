"""
Publishing — where a dossier actually goes.

Without this the record layer computes a checksum, anchors it, and the artifact
exists nowhere. A counterparty gets a hash with nothing to hash. This is the
piece that turns a `uri` field into a real document.

Layout — every path is CONTENT-ADDRESSED and never overwritten:

    dossiers/
    ├── trades/2026-08-27/sig_8d66ac748b02.1da0ae2c.json
    ├── decisions/2026-08-27.7e16e4d9.json
    ├── policy/sha256-0ea50bc8.json
    └── manifest.json

The checksum prefix in each filename is not decoration. Without it a path is
mutable, and a mutable path breaks verification in a way that cannot be
distinguished from tampering: republish a session and the bytes at an
already-anchored URL change, so an auditor fetching it computes a different
hash. Raw file hosting adds CDN caching on top, so they may not even be
fetching the current version. Failed verification then means "stale, republished
or tampered with" and there is no way to tell which.

With the checksum in the path, a URL resolves to exactly one byte sequence
forever. A republished session is a NEW file and a NEW record, and the previous
record is marked Superseded rather than silently replaced.

`manifest.json` is a mutable convenience index mapping each day to the
checksums published for it, newest last. Nothing is anchored against it and no
verification depends on it — it exists so a human can find the current version
without listing a directory.

The rule from `canonical.py` holds throughout: the bytes written here are the
bytes hashed. An auditor runs `sha256sum` on what they downloaded and gets the
value on chain, with no knowledge of our serialiser.

Visibility
----------
Two postures, chosen per publisher instance:

**public** — the document is world-readable and `uri` resolves for anyone. This
is the right mode for our own book, where open verification IS the product, and
it is what makes an audit possible with no involvement from us.

**private** — the document stays on our infrastructure and `uri` carries a
non-resolvable identifier. The checksum still anchors, so the record is still
permanent, attributable and timestamped; it simply cannot be read by the world.
Disclosure to a named counterparty is a deliberate act (see `disclosure.py`),
and they verify exactly as a public reader would.

Private is the default for client mandates. A decision log is a fairly direct
description of a strategy: the refusals say what a desk will not trade and
under what conditions, which is precisely the part a client is paying to keep.
Publishing that openly would be a confidentiality failure dressed up as
transparency.

This mirrors NVNM's own framing — the proof goes on chain, the data stays home —
and the primitive supports both without change, because what is anchored is a
checksum and a URI, not the document.

The PUBLIC root is intended to be a separate repo from the code. The code repo
holds credentials in `.env` and must stay private; a public dossier repo holds
only published claims and must be world-readable or the audit path breaks.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .canonical import checksum as checksum_of
from .canonical import verify, write_canonical
from .dgml import dossier_checksum

logger = logging.getLogger("zcastor.record.publisher")


class Publisher:
    def __init__(
        self,
        root: str | Path,
        *,
        uri_template: str = "",
        visibility: str = "public",
        holder: str = "afritensor",
    ) -> None:
        """
        `uri_template` is the URL pattern for a public dossier repo, e.g.
        "https://raw.githubusercontent.com/user/zcastor-dossiers/main/{path}".

        In private mode the template is ignored and a `held://` identifier is
        recorded instead. That is deliberate: an empty URI says nothing, while
        `held://afritensor/...` states that a document exists, who holds it, and
        that it is available on request. A reader can tell "withheld" from
        "never recorded".
        """
        if visibility not in ("public", "private"):
            raise ValueError(f"visibility must be public or private, "
                             f"got {visibility!r}")
        self.root = Path(root)
        self.uri_template = uri_template
        self.visibility = visibility
        self.holder = holder
        # Called with a short reason after every file written, so the owner can
        # sync the repo (see repo_sync.py). Never allowed to fail a publish.
        self.on_published: Optional[Callable[[str], None]] = None

    def _notify(self, reason: str) -> None:
        if self.on_published is None:
            return
        try:
            self.on_published(reason)
        except Exception:  # noqa: BLE001
            logger.exception("on_published hook failed")

    @property
    def is_public(self) -> bool:
        return self.visibility == "public"

    # ── paths ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _short(checksum: str) -> str:
        return checksum.replace("sha256:", "")[:8]

    def _relative(self, kind: str, name: str, day: str = "",
                  checksum: str = "") -> str:
        stem = f"{name}.{self._short(checksum)}" if checksum else name
        return f"{kind}/{day}/{stem}.json" if day else f"{kind}/{stem}.json"

    def uri_for(self, relative: str) -> str:
        if not self.is_public:
            # Not a fetchable URL, and not meant to be. It identifies the
            # document and names its custodian.
            return f"held://{self.holder}/{relative}"
        if not self.uri_template:
            return ""
        return self.uri_template.format(path=relative)

    # ── publishing ────────────────────────────────────────────────────────────

    def publish_dossier(self, dossier: Mapping[str, Any], *,
                        day: str) -> dict[str, str]:
        """
        Write a trade dossier and return what the anchor layer needs.

        THE PROVENANCE BLOCK IS STRIPPED BEFORE WRITING. It has to be: provenance
        is filled in after the record lands, so a file containing it would stop
        matching its own anchored checksum the moment the transaction confirmed.
        Publishing the stripped document keeps `sha256sum <file>` equal to the
        value on chain, which is the whole basis of the audit path.

        Nothing is lost. The chain IS the provenance — where a dossier landed is
        a fact about the record, not a claim the dossier makes about itself.
        """
        signal_id = str(dossier.get("identity", {}).get("signal_id", "unknown"))

        # Hash first, then name the file after it. The checksum covers content
        # only, so the filename cannot affect it — no circularity.
        stable = {k: v for k, v in dossier.items() if k != "provenance"}
        checksum = dossier_checksum(dossier)
        relative = self._relative("trades", signal_id, day, checksum)
        write_canonical(self.root / relative, stable)

        # Cheap insurance against the two paths ever drifting apart again.
        if checksum != dossier_checksum(dossier):
            raise RuntimeError(
                f"published bytes do not match the dossier checksum for "
                f"{signal_id} — the audit path would break silently"
            )

        self._record_in_manifest("trades", signal_id, relative, checksum)
        logger.info("published dossier %s", relative)
        self._notify("trade dossier")
        return {"path": relative, "checksum": checksum,
                "uri": self.uri_for(relative)}

    def publish_decision_log(self, log: Mapping[str, Any]) -> dict[str, str]:
        """
        Write a session's decision log — every verdict including every refusal.

        This is the record the thesis actually rests on. Fills are easy to show;
        a complete, dated, hashed list of what the system declined and why is
        the part that is hard to fake after the fact.
        """
        day = str(log.get("session_date", "unknown"))
        checksum = checksum_of(dict(log))
        relative = self._relative("decisions", day, checksum=checksum)
        write_canonical(self.root / relative, dict(log))

        previous = self._record_in_manifest("decisions", day, relative, checksum)
        logger.info("published decision log %s (%s decisions)",
                    relative, len(log.get("decisions", [])))
        self._notify(f"decision log {day}")
        return {"path": relative, "checksum": checksum,
                "uri": self.uri_for(relative),
                # The checksum this one replaces, so the caller can mark the
                # earlier record Superseded instead of leaving two live.
                "supersedes": previous}

    def publish_policy(self, policy: Mapping[str, Any],
                       policy_hash: str) -> dict[str, str]:
        """
        Publish the policy itself — Proof of Process.

        Named by its own hash so republishing an unchanged policy is a no-op and
        every version stays retrievable. A decision record referencing a policy
        hash must always resolve to the policy that was actually in force.
        """
        short = policy_hash.replace("sha256:", "")[:16]
        relative = self._relative("policy", f"sha256-{short}")
        path = self.root / relative

        if path.exists():
            return {"path": relative, "checksum": policy_hash,
                    "uri": self.uri_for(relative), "existing": "true"}

        checksum = write_canonical(path, dict(policy))
        logger.info("published policy %s", relative)
        self._notify("policy")
        return {"path": relative, "checksum": checksum,
                "uri": self.uri_for(relative)}

    # ── manifest ──────────────────────────────────────────────────────────────

    def _manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def manifest(self) -> dict[str, Any]:
        path = self._manifest_path()
        if not path.exists():
            return {"trades": {}, "decisions": {}}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("manifest unreadable — starting a new one")
            return {"trades": {}, "decisions": {}}

    def _record_in_manifest(self, kind: str, key: str, relative: str,
                            checksum: str) -> Optional[str]:
        """
        Append to the manifest and return the checksum this entry replaces.

        Mutable by design and deliberately outside the verification path —
        nothing is anchored against it. It exists so a person can find the
        current version of a day's log without listing a directory.
        """
        data = self.manifest()
        entries = data.setdefault(kind, {}).setdefault(key, [])
        previous = entries[-1]["checksum"] if entries else None

        if any(e["checksum"] == checksum for e in entries):
            return None            # identical content, nothing superseded

        entries.append({"checksum": checksum, "path": relative,
                        "uri": self.uri_for(relative)})
        self._manifest_path().parent.mkdir(parents=True, exist_ok=True)
        self._manifest_path().write_text(json.dumps(data, indent=2,
                                                    sort_keys=True) + "\n")
        return previous

    def latest(self, kind: str, key: str) -> Optional[dict[str, Any]]:
        entries = self.manifest().get(kind, {}).get(key, [])
        return entries[-1] if entries else None

    # ── verification ──────────────────────────────────────────────────────────

    def verify(self, relative: str, expected_checksum: str) -> bool:
        """The auditor's side, run locally. Catches drift before anyone else does."""
        path = self.root / relative
        if not path.exists():
            return False
        return verify(path, expected_checksum)

    def stats(self) -> dict[str, int]:
        def count(kind: str) -> int:
            base = self.root / kind
            return len(list(base.rglob("*.json"))) if base.exists() else 0

        manifest = self.manifest()
        return {"visibility": self.visibility,
                "trades": count("trades"), "decisions": count("decisions"),
                "policy": count("policy"),
                "sessions": len(manifest.get("decisions", {}))}
