"""
Canonical form and checksums.

This module is the load-bearing piece of the whole accountability story. If a
counterparty recomputes a checksum and gets a different answer than the one
anchored on NVNM, the dossier is worthless — and they will not debug our
serialiser to find out why.

THE DESIGN RULE THAT MAKES THIS SAFE: we hash the exact bytes we publish.

`canonical_bytes()` produces the byte string, those same bytes are written to the
dossier repo, and the checksum is sha256 of them. An auditor therefore never has
to reimplement our canonicalisation — they run `sha256sum dossier.json` on the
file they downloaded. Any scheme where the auditor must re-serialise before
hashing is a scheme that breaks the first time a float or a unicode escape
renders differently, and it breaks silently.

Serialisation rules, all chosen for cross-language stability:

- keys sorted, so dict ordering never matters
- no insignificant whitespace
- UTF-8, unescaped — `ensure_ascii=False`, so a Zürich renders as itself in the
  published file rather than as \\u00fc
- NaN and Infinity rejected outright. They are not JSON, Python emits them
  anyway, and a checksum over a file no other parser will read is a trap
- trailing newline, so the file is a well-formed text file and `sha256sum` on a
  git-checked-out copy matches
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SHA256_PREFIX = "sha256:"


class CanonicalError(ValueError):
    """The value cannot be represented in a stable, portable canonical form."""


def _reject_non_finite(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CanonicalError(
                f"non-finite float at {path}: {value!r}. NaN and Infinity are "
                f"not valid JSON and would produce a file other parsers reject."
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError(
                    f"non-string key at {path}: {k!r}. Key ordering across "
                    f"mixed types is not portable."
                )
            _reject_non_finite(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_non_finite(v, f"{path}[{i}]")


def canonical_bytes(value: Any) -> bytes:
    """The exact bytes to publish AND to hash. Never hash anything else."""
    _reject_non_finite(value)
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def checksum(value: Any) -> str:
    """sha256 of the canonical bytes, prefixed for use in a Record."""
    return SHA256_PREFIX + hashlib.sha256(canonical_bytes(value)).hexdigest()


def checksum_bytes(data: bytes) -> str:
    """sha256 of bytes already on disk — what an auditor computes."""
    return SHA256_PREFIX + hashlib.sha256(data).hexdigest()


def bare(digest: str) -> str:
    """Strip the prefix. The anchoring module takes the raw hex."""
    return digest[len(SHA256_PREFIX):] if digest.startswith(SHA256_PREFIX) else digest


def write_canonical(path, value: Any) -> str:
    """
    Write the canonical bytes to disk and return their checksum.

    The single place a dossier reaches the filesystem, so the published file and
    the anchored checksum cannot drift apart.
    """
    data = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return checksum_bytes(data)


def verify(path, expected: str) -> bool:
    """Recompute a file's checksum — the auditor's side of the loop."""
    return checksum_bytes(path.read_bytes()) == expected
