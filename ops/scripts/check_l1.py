#!/usr/bin/env python3
"""
Read-only readiness check for the NVNM L1 testnet (chain 787223).

Answers, before anything is signed:
  - is the RPC up, and is it the chain we think it is?
  - how is gas priced right now (attodollars per gas)?
  - how much nUSD does the operator wallet hold?
  - is the anchoring module live at 0x...0A00?
  - how much gas would addRegistry take, and what would it cost in dollars?
    (the module's write cost decides whether our 3M gas limit is enough)

Nothing is signed and nothing is sent. Every call is an eth_call or estimate.

    python3 ops/scripts/check_l1.py
    python3 ops/scripts/check_l1.py --address 0x... --rpc https://...
"""

from __future__ import annotations

import argparse
import sys

from web3 import Web3

RPC = "https://rpc.nvnm.testnet.nvnmchain.io"
EXPECTED_CHAIN = 787223
OPERATOR = "0x5501Dd00df7482083bEc81a25d96d0347B6F5331"
ANCHORING = "0x0000000000000000000000000000000000000A00"
NUSD = "0x20C0000000000000000000000000000000000000"   # Tempo's default stablecoin slot
OUR_GAS_LIMIT = 3_000_000

ERC20 = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
]

ADD_REGISTRY = [
    {"name": "addRegistry", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "name", "type": "string"},
                {"name": "description", "type": "string"},
                {"name": "metadata", "type": "string"}],
     "outputs": []},
]


def line(label: str, value) -> None:
    print(f"  {label:<22} {value}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=RPC)
    ap.add_argument("--address", default=OPERATOR)
    args = ap.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20}))
    print(f"\n  network   {args.rpc}")
    try:
        chain = w3.eth.chain_id
    except Exception as exc:  # noqa: BLE001
        print(f"  RPC not reachable: {exc}")
        return 1

    ok = "ok" if chain == EXPECTED_CHAIN else f"UNEXPECTED (wanted {EXPECTED_CHAIN})"
    line("chain id", f"{chain}  {ok}")
    head = w3.eth.get_block("latest")
    line("block", f"{head['number']:,}")

    base = int(head.get("baseFeePerGas") or 0)
    try:
        tip = int(w3.eth.max_priority_fee)
    except Exception:  # noqa: BLE001
        tip = 0
    max_fee = base * 2 + tip
    line("base fee / gas", f"{base:,}  (attodollars if priced in USD)")
    line("max fee we'd offer", f"{max_fee:,}")

    who = Web3.to_checksum_address(args.address)
    try:
        token = w3.eth.contract(address=Web3.to_checksum_address(NUSD), abi=ERC20)
        dec = token.functions.decimals().call()
        sym = token.functions.symbol().call()
        bal = token.functions.balanceOf(who).call()
        line("gas token", f"{sym} at {NUSD}")
        line("operator balance", f"{bal / 10**dec:,.6f} {sym}")
    except Exception as exc:  # noqa: BLE001
        line("gas token", f"could not read {NUSD}: {exc}")

    code = w3.eth.get_code(Web3.to_checksum_address(ANCHORING))
    line("anchoring module", f"{len(code):,} bytes of code"
         if code else "no code (precompile, or absent)")

    reg = w3.eth.contract(address=Web3.to_checksum_address(ANCHORING), abi=ADD_REGISTRY)
    try:
        gas = reg.functions.addRegistry(
            "afritensor-l1-check", "readiness probe, not sent", "{}"
        ).estimate_gas({"from": who})
        cost_usd = gas * max_fee / 1e18
        fits = "fits" if gas < OUR_GAS_LIMIT else "EXCEEDS our 3M limit — raise it"
        line("addRegistry gas", f"{gas:,}  ({fits})")
        line("addRegistry cost", f"~${cost_usd:.6f} at max fee")
    except Exception as exc:  # noqa: BLE001
        line("addRegistry estimate", f"failed: {exc}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
