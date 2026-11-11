#!/usr/bin/env python3
"""
Reconcile the local index against what the chain actually holds.

The index once recorded a status the moment an update was QUEUED rather than
when it landed. Fifty-five records therefore read Superseded locally while every
status update sat parked and the chain still said Active. That has been fixed
going forward, but the wrong history remains — and disclosure bundles read
status from this index, so a counterparty would be told a record was superseded
when the chain says otherwise.

This reads every updateRecordStatus our wallet has sent, decodes it, and
corrects the index to match. Where the two disagree, the chain wins.

    python3 ops/scripts/reconcile_index.py --dry-run
    python3 ops/scripts/reconcile_index.py

Read-only against the chain. Stop the engine first.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.app.lock import EngineRunning, require_engine_stopped  # noqa: E402

try:
    require_engine_stopped(ROOT / "data")
except EngineRunning as _exc:
    print(f"\n  refusing to run: {_exc}\n")
    sys.exit(2)

UPDATE_STATUS = "97b40c25"
EXPLORER = {"testnet": "https://explorer.evm.testnet.nvnmchain.io"}
DATA = ROOT / "data"


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Zcastor/2.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def decode(input_hex: str) -> tuple[int, int, int, str]:
    """updateRecordStatus(uint64 registryId, uint64 recordId, uint64 index, string status)"""
    d = input_hex[10:] if input_hex.startswith("0x") else input_hex[8:]
    word = lambda i: int(d[i * 64:(i + 1) * 64], 16)
    offset = word(3) * 2
    length = int(d[offset:offset + 64], 16)
    status = bytes.fromhex(d[offset + 64:offset + 64 + length * 2]).decode()
    return word(0), word(1), word(2), status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    address = os.environ.get("ZCASTOR_AGENT_EXECUTION", "")
    if not address:
        print("set ZCASTOR_AGENT_EXECUTION to the sending address")
        return 1
    base = EXPLORER[os.environ.get("NVNM_ENV", "testnet")]

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

    # (registryId, recordId) -> status, last write wins.
    on_chain: dict[tuple[int, int], str] = {}
    for tx in rows:
        if str(tx.get("input", ""))[2:10].lower() != UPDATE_STATUS:
            continue
        if str(tx.get("isError", "0")) == "1":
            continue
        try:
            reg, rec, _idx, status = decode(str(tx["input"]))
        except (ValueError, IndexError, UnicodeDecodeError):
            continue
        on_chain[(reg, rec)] = status

    print(f"\n  {len(rows)} transactions from this wallet")
    print(f"  {len(on_chain)} record(s) whose status the chain has changed\n")

    index_path = DATA / "anchor_index.jsonl"
    state: dict[str, dict] = {}
    for line in index_path.read_text(encoding="utf-8").splitlines():
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

    corrections = {}
    for sid, row in state.items():
        key = (int(row.get("registry_id") or 0), int(row.get("record_id") or 0))
        truth = on_chain.get(key, "Active")
        if row.get("status") != truth:
            corrections[sid] = truth

    claimed = sum(1 for r in state.values() if r.get("status") == "Superseded")
    print(f"  index claims {claimed} superseded")
    print(f"  chain confirms {sum(1 for v in on_chain.values() if v == 'Superseded')}")
    print(f"  {len(corrections)} correction(s) needed\n")

    for sid, truth in list(corrections.items())[:12]:
        print(f"    {sid[:34]:36} {state[sid].get('status'):12} -> {truth}")
    if len(corrections) > 12:
        print(f"    … and {len(corrections) - 12} more")

    if args.dry_run:
        print("\n  DRY RUN — nothing changed\n")
        return 0
    if not corrections:
        print("\n  index already agrees with the chain\n")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copyfile(index_path, index_path.with_suffix(f".{stamp}.bak"))
    with index_path.open("a", encoding="utf-8") as fh:
        for sid, truth in corrections.items():
            fh.write(json.dumps({"signal_id": sid, "status": truth,
                                 "reconciled": stamp}, sort_keys=True) + "\n")

    print(f"\n  corrected {len(corrections)}, backup alongside as .{stamp}.bak")
    print("  the index now reports what the chain holds\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
