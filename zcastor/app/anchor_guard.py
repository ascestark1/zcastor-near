"""
Refuse to run with anchoring silently off.

On 2 Oct 2026 `.env` lost its `export` prefixes, ANCHOR_BACKEND never reached
the engine, and it fell back to the NullBackend. The null backend accepts
everything, so the policy record and a decision log were marked "anchored"
with receipts that never touched a chain, and the outbox would have skipped
them forever as already done.

NullBackend is right for the demo and for tests. It is wrong for an engine
whose runtime.json names real registries. This module tells the two apart and
stops the second at startup, unless the operator says, explicitly, that an
offline run is intended.

Pure, so it is testable without FastAPI or a chain.
"""

from __future__ import annotations

from typing import Any, Mapping


class AnchorMisconfigured(RuntimeError):
    pass


OVERRIDE = "ZCASTOR_ALLOW_NULL_ANCHOR"


def check_anchor_config(backend_name: str, runtime: Mapping[str, Any],
                        env: Mapping[str, str]) -> str:
    """
    Returns "ok", "offline-allowed" or raises AnchorMisconfigured.

    - a real backend: always fine
    - no registry ids configured: a deliberate offline setup, fine
    - registry ids configured but a null backend: refuse, unless overridden
    """
    if backend_name != "null":
        return "ok"
    ids = {k: v for k, v in dict(runtime.get("registry_ids") or {}).items() if v}
    if not ids:
        return "ok"
    if str(env.get(OVERRIDE, "")).strip().lower() == "yes":
        return "offline-allowed"
    raise AnchorMisconfigured(
        "runtime.json names registries "
        f"{sorted(ids.values())} but anchoring is OFF (ANCHOR_BACKEND is not "
        "'nvnm' in this process). Records would be marked anchored without "
        "reaching the chain. Fix: make sure .env lines use 'export', or start "
        "with `set -a; source .env; set +a`. For a deliberate offline run, set "
        f"{OVERRIDE}=yes."
    )
