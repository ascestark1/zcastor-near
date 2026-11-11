#!/usr/bin/env python3
"""
Verify a Zcastor record against the chain, trusting nothing but the chain.

    python3 verify_record.py decisions/2026-10-02.62bbba4c.json
    python3 verify_record.py -n all decisions/2026-09-08.beb40056.json
    python3 verify_record.py beb400566f7b05704485e4b890e6ffa53676da541a1e65e79783c36d005320b4

Zcastor moved from the L2 testnet (787111) to the NVNM L1 testnet (787223) on
2 Oct 2026. Both chains keep running; `-n all` searches both.

Given a file, it hashes the bytes you actually hold. Given a checksum, it uses
that. Either way it then asks the anchoring contract directly — one eth_call to
records(registryId, checksum, …) — whether that checksum was anchored, when,
under which status, and whether it is the latest version.

This is the step that used to need someone else's software: the MCP server,
the explorer's API, or our own index. None of those are consulted here. The
only thing trusted is the chain, and the only thing a counterparty needs is
this file, Python and web3.

Read-only. Nothing is signed or sent.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

NETWORKS = {
    # Sovereign L1 testnet, chain 787223. Zcastor anchors here from 2 Oct 2026.
    "l1-testnet": "https://rpc.nvnm.testnet.nvnmchain.io",
    # The earlier L2 testnet, chain 787111. Still running; holds 6 Sep–2 Oct.
    "testnet": "https://evm.testnet.nvnmchain.io",
    "canary": "https://rpc.nvnm.canary.mantrachain.dev",
}
# Afritensor's registries per network. A counterparty passes their own with -r.
REGISTRIES_BY_NETWORK = {
    "l1-testnet": {2194: "policy", 2195: "decisions", 2196: "dossiers", 2197: "archive"},
    "testnet": {4296: "policy", 4297: "decisions", 4298: "dossiers", 4299: "archive"},
    "canary": {},
}
# Records made before the move live on 787111, newer ones on 787223.
# `-n all` searches both, so an auditor need not know which era a file is from.
SEARCH_ALL = ("l1-testnet", "testnet")
ANCHORING = "0x0000000000000000000000000000000000000a00"

# Only the one read function is needed, so it is declared inline rather than
# depending on anyone's ABI file. Matches NVNM-Chain/nvnmchain-contracts.
RECORDS_ABI = [{
    "type": "function", "name": "records", "stateMutability": "view",
    "inputs": [
        {"name": "registryId", "type": "uint64"},
        {"name": "checksum", "type": "string"},
        {"name": "recordId", "type": "uint64"},
        {"name": "index", "type": "uint64"},
        {"name": "pagination", "type": "tuple", "components": [
            {"name": "key", "type": "bytes"}, {"name": "offset", "type": "uint64"},
            {"name": "limit", "type": "uint64"}, {"name": "countTotal", "type": "bool"},
            {"name": "reverse", "type": "bool"}]}],
    "outputs": [
        {"name": "recordsOut", "type": "tuple[]", "components": [
            {"name": "uri", "type": "string"}, {"name": "checksum", "type": "string"},
            {"name": "checksumAlgo", "type": "string"}, {"name": "metadata", "type": "string"},
            {"name": "timestamp", "type": "string"}, {"name": "status", "type": "string"},
            {"name": "recordId", "type": "uint64"}, {"name": "index", "type": "uint64"},
            {"name": "isLatest", "type": "bool"}, {"name": "registryId", "type": "uint64"}]},
        {"name": "paginationOut", "type": "tuple", "components": [
            {"name": "nextKey", "type": "bytes"}, {"name": "total", "type": "uint64"}]}],
}]


def checksum_of(target: str) -> tuple[str, str]:
    """(checksum, where it came from). A file is hashed; a hex string is used."""
    path = Path(target)
    if path.exists():
        return hashlib.sha256(path.read_bytes()).hexdigest(), f"sha256 of {path.name}"
    cleaned = target.lower().removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", cleaned):
        return cleaned, "as given"
    sys.exit(f"not a file and not a 64-character sha256: {target}")


def default_network(env: dict | None = None) -> str:
    """NVNM_ENV if it names a known network, else the L1 testnet."""
    value = (env if env is not None else os.environ).get("NVNM_ENV", "")
    return value if value in NETWORKS else "l1-testnet"


def is_absent(exc: Exception) -> bool:
    """A revert that means 'no record with this checksum here', not a failure."""
    text = str(exc).lower()
    return "not found" in text or "does not exist" in text


def matches(rows, checksum: str) -> list[tuple]:
    """Rows from records() whose checksum equals ours, as display tuples."""
    out = []
    for row in rows:
        uri, cs, _algo, _meta, ts, status, rid, idx, latest, reg_id = row
        if cs.lower().removeprefix("sha256:") == checksum:
            out.append((reg_id, rid, idx, status, latest, ts, uri))
    return out


def search(network: str, rpc: str, checksum: str,
           registries: list[int]) -> list[tuple]:
    """Query one network's anchoring contract for `checksum` in each registry."""
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    contract = w3.eth.contract(address=Web3.to_checksum_address(ANCHORING),
                               abi=RECORDS_ABI)
    try:
        chain = w3.eth.chain_id
    except Exception as exc:  # noqa: BLE001
        print(f"  {network}: unreachable — {str(exc)[:90]}\n")
        return []
    print(f"  network   {network}  chain {chain}  {rpc}")

    found = []
    for reg in registries:
        try:
            rows, _page = contract.functions.records(
                reg, checksum, 0, 0, (b"", 0, 50, True, False)).call()
        except Exception as exc:  # noqa: BLE001
            # Both testnets report "no such record here" as a revert — 787111
            # says "not found", the L1 "record does not exist". The canary
            # returns an empty list. All three mean absent.
            if is_absent(exc):
                continue
            print(f"  registry {reg}: query failed — {str(exc)[:90]}")
            continue
        found += [(network, chain, *m) for m in matches(rows, checksum)]
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="a record file, or its sha256")
    ap.add_argument("-n", "--network", default=default_network(),
                    choices=sorted(NETWORKS) + ["all"],
                    help="where to look; 'all' searches the L1 and the "
                         "787111 testnet (default: $NVNM_ENV or l1-testnet)")
    ap.add_argument("--rpc", help="override the RPC endpoint (single network only)")
    ap.add_argument("-r", "--registry", type=int, action="append",
                    help="registry id to search (repeatable); "
                         "default: Afritensor's four on that network")
    args = ap.parse_args()

    if args.network == "all" and args.rpc:
        sys.exit("--rpc names one endpoint; pick a network with -n instead of 'all'")
    networks = list(SEARCH_ALL) if args.network == "all" else [args.network]

    checksum, source = checksum_of(args.target)
    print(f"\n  checksum  {checksum}  ({source})")

    found = []
    for net in networks:
        registries = args.registry or list(REGISTRIES_BY_NETWORK.get(net, {}))
        if not registries:
            print(f"  {net}: no registries known — pass them with -r")
            continue
        found += search(net, args.rpc or NETWORKS[net], checksum, registries)
    print()

    if not found:
        print("  NOT ANCHORED — no record with this checksum in the registries "
              "searched.\n  If the file was edited after anchoring, its hash "
              "will not match anything.")
        if args.network != "all":
            print("  Records from before 2 Oct 2026 are on 787111: try -n all.")
        print()
        return 1

    for net, chain, reg_id, rid, idx, status, latest, ts, uri in found:
        name = REGISTRIES_BY_NETWORK.get(net, {}).get(reg_id, "")
        print(f"  ANCHORED  {net} (chain {chain})  registry {reg_id} {name}  "
              f"record {rid}  version {idx}")
        print(f"            status    {status}{'  (latest version)' if latest else ''}")
        print(f"            anchored  {ts}")
        print(f"            uri       {uri}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
