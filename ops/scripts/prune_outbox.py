#!/usr/bin/env python3
"""
Clear the parked pile using the chain as the authority.

Sixty-five entries accumulated across three generations of bugs, and they are
not one problem:

  - work ALREADY DONE. Records 15 through 19 are parked while the log shows
    those exact status updates landing. Retrying would write a second status
    change for a record that is already superseded.
  - entries missing a record_id that the index now knows, recovered from
    receipts after those entries were parked.
  - add_record entries whose checksum is already anchored.

Retrying the pile blindly would duplicate work. Deleting it blindly would
discard writes that never happened. So each entry is checked against what the
chain holds, and only genuinely outstanding work is requeued.

    python3 ops/scripts/prune_outbox.py --dry-run
    python3 ops/scripts/prune_outbox.py

Stop the engine first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.app.lock import EngineRunning, require_engine_stopped  # noqa: E402

try:
    require_engine_stopped(ROOT / "data")
except EngineRunning as _exc:
    print(f"\n  refusing to run: {_exc}\n")
    sys.exit(2)

from zcastor.anchor.outbox import Outbox  # noqa: E402

UPDATE_STATUS = "97b40c25"
ADD_RECORD = "64d25295"
EXPLORER = {"testnet": "https://explorer.evm.testnet.nvnmchain.io"}
DATA = ROOT / "data"


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Zcastor/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def chain_state(address: str, base: str) -> tuple[dict, set]:
    """What the chain holds: statuses by (registry, record), anchored checksums."""
    rows, page = [], 1
    while page <= 20:
        batch = fetch(f"{base}/api?module=account&action=txlist"
                      f"&address={address}&page={page}&offset=100"
                      f"&sort=asc").get("result") or []
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    statuses: dict[tuple[int, int], str] = {}
    checksums: set[str] = set()
    for tx in rows:
        data = str(tx.get("input", ""))
        if str(tx.get("isError", "0")) == "1":
            continue
        selector = data[2:10].lower()
        body = data[10:]
        try:
            if selector == UPDATE_STATUS:
                reg = int(body[0:64], 16)
                rec = int(body[64:128], 16)
                off = int(body[192:256], 16) * 2
                length = int(body[off:off + 64], 16)
                status = bytes.fromhex(
                    body[off + 64:off + 64 + length * 2]).decode()
                statuses[(reg, rec)] = status
            elif selector == ADD_RECORD:
                # The checksum is the second string in the Record tuple.
                for i in range(2, 40):
                    word = body[i * 64:(i + 1) * 64]
                    if not word:
                        break
                    off = int(word, 16) * 2
                    if 0 < off < len(body) - 64:
                        ln = int(body[off:off + 64], 16)
                        if ln == 64:
                            raw = body[off + 64:off + 64 + 128]
                            try:
                                checksums.add(bytes.fromhex(raw).decode())
                            except (ValueError, UnicodeDecodeError):
                                pass
        except (ValueError, IndexError):
            continue
    return statuses, checksums


def index_by_checksum() -> dict:
    out = {}
    path = DATA / "anchor_index.jsonl"
    state: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = row.get("signal_id")
        if sid:
            state.setdefault(sid, {}).update(row)
    for row in state.values():
        cs = str(row.get("checksum", "")).replace("sha256:", "")
        if cs:
            out[cs] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    address = os.environ.get("ZCASTOR_AGENT_EXECUTION", "")
    if not address:
        print("set ZCASTOR_AGENT_EXECUTION")
        return 1

    base = EXPLORER[os.environ.get("NVNM_ENV", "testnet")]
    statuses, anchored = chain_state(address, base)
    by_checksum = index_by_checksum()
    registry_ids = json.loads((ROOT / "config" / "runtime.json").read_text()
                              ).get("registry_ids", {})
    names = {f"afritensor-{k}": int(v) for k, v in registry_ids.items() if v}

    box = Outbox(DATA / "anchor_outbox.jsonl")
    done, repaired, outstanding = [], [], []

    for entry in list(box.parked):
        cs = str(entry.checksum or "").replace("sha256:", "")

        if entry.op == "update_status":
            # Fill in a record id the index has since learned.
            if not entry.record_id and cs in by_checksum:
                row = by_checksum[cs]
                entry.registry_id = entry.registry_id or int(row.get("registry_id") or 0)
                entry.record_id = int(row.get("record_id") or 0)
                entry.index = int(row.get("index") or 1)
                repaired.append(entry)

            key = (int(entry.registry_id or 0), int(entry.record_id or 0))
            if statuses.get(key) == entry.status:
                done.append(entry)           # the chain already says this
                continue
            if entry.record_id:
                outstanding.append(entry)
            continue

        if entry.op == "add_record":
            if not entry.registry_id:
                entry.registry_id = names.get(entry.registry, 0)
                repaired.append(entry)
            if cs in anchored:
                done.append(entry)           # already on chain
                continue
            if entry.registry_id:
                outstanding.append(entry)

    print(f"\n  {len(box.parked)} parked")
    print(f"    {len(done):3} already done on chain — will be dropped")
    print(f"    {len(repaired):3} repaired with an id the index now knows")
    print(f"    {len(outstanding):3} genuinely outstanding — will be requeued")
    unhandled = len(box.parked) - len(done) - len(outstanding)
    if unhandled > 0:
        print(f"    {unhandled:3} cannot be resolved and stay parked")

    if args.dry_run:
        print("\n  DRY RUN — nothing changed\n")
        return 0

    for entry in done:
        box.mark_done(entry)
    requeued = box.requeue_parked()
    print(f"\n  dropped {len(done)}, requeued {requeued}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
