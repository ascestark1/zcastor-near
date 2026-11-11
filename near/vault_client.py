#!/usr/bin/env python3
"""
The desk's side of the vault: ask for permits, settle them, report equity, and
turn a vault signature into a real NVNM transaction.

    .venv/bin/python near/vault_client.py status
    .venv/bin/python near/vault_client.py permit btc --signal demo-3 --side Buy \\
        --notional 1000 --risk 40 --leverage 3
    .venv/bin/python near/vault_client.py settle btc 0 --pnl 12.50
    .venv/bin/python near/vault_client.py address btc
    .venv/bin/python near/vault_client.py registry btc "zcastor-desk-btc"
    .venv/bin/python near/vault_client.py record btc <registry_id> <sha256> <uri>

Configuration, from the environment (see .env.example):
    NEAR_VAULT      vault.zcastor-desk.testnet
    NEAR_OWNER      zcastor-desk.testnet       (agents are <name>.<owner>)
    NEAR_NETWORK    testnet
    NVNM_RPC_URL, NVNM_CHAIN_ID

NEAR transactions go through the `near` CLI, which signs with the keys in
~/.near-credentials. This module never reads a key itself. NVNM transactions
are not signed here at all: the vault builds the signing hash on chain and NEAR
Chain Signatures signs it, so the agent's EVM key exists nowhere. What this
module does with the signature is public work: put it on the transaction and
broadcast it.

Before broadcasting it checks two things itself, because a wrong answer here
would send a transaction nobody intended:
  - the hash the vault says it would sign equals the hash computed locally;
  - the signature recovers to the agent's known address.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = "0x0000000000000000000000000000000000000A00"
ADDRESS_CACHE = ROOT / "data" / "near" / "addresses.json"

Runner = Callable[[list[str]], tuple[int, str, str]]


class VaultError(RuntimeError):
    """The vault (or the network) said no. `.reason` is the contract's message."""

    def __init__(self, reason: str, raw: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    return p.returncode, p.stdout, p.stderr


def parse_return(stdout: str) -> Any:
    """The JSON value the CLI prints to stdout; None for an empty return."""
    text = stdout.strip()
    for i, ch in enumerate(text):
        if ch in "{[\"" or ch.isdigit() or text.startswith(("true", "false", "null"), i):
            try:
                value, _ = json.JSONDecoder().raw_decode(text[i:])
                return value
            except ValueError:
                continue
    return None


def parse_failure(output: str) -> str:
    m = re.search(r"Smart contract panicked: (.*?)\"?\)?\s*$", output, re.M)
    if m:
        return m.group(1).strip().rstrip('")')
    m = re.search(r"Error:\s*\n?\s*0?:?\s*(.+)", output)
    return (m.group(1) if m else output.strip()[-300:]).strip()


class Vault:
    def __init__(self, vault: str = "", owner: str = "", network: str = "",
                 runner: Runner = _run, near_bin: str = "near") -> None:
        self.vault = vault or os.environ.get("NEAR_VAULT", "")
        self.owner = owner or os.environ.get("NEAR_OWNER", "")
        self.network = network or os.environ.get("NEAR_NETWORK", "testnet")
        if not self.vault or not self.owner:
            raise VaultError("set NEAR_VAULT and NEAR_OWNER (see .env.example)")
        self._run = runner
        self._near = near_bin

    def account(self, name: str) -> str:
        """`btc` -> `btc.<owner>`; a full account id is returned unchanged."""
        return name if "." in name else f"{name}.{self.owner}"

    # ── raw calls ─────────────────────────────────────────────────────────────

    def call(self, signer: str, method: str, args: dict, tgas: int = 30) -> Any:
        code, out, err = self._run([
            self._near, "contract", "call-function", "as-transaction", self.vault,
            method, "json-args", json.dumps(args, separators=(",", ":")),
            "prepaid-gas", f"{tgas}.0 Tgas", "attached-deposit", "0 NEAR",
            "sign-as", self.account(signer), "network-config", self.network,
            "sign-with-legacy-keychain", "send"])
        if code != 0:
            raise VaultError(parse_failure(out + "\n" + err), raw=err)
        return parse_return(out)

    def view(self, method: str, args: Optional[dict] = None) -> Any:
        code, out, err = self._run([
            self._near, "contract", "call-function", "as-read-only", self.vault,
            method, "json-args", json.dumps(args or {}, separators=(",", ":")),
            "network-config", self.network, "now"])
        if code != 0:
            raise VaultError(parse_failure(out + "\n" + err), raw=err)
        return parse_return(out)

    # ── the desk's verbs ──────────────────────────────────────────────────────

    def request_permit(self, agent: str, *, signal_hash: str, side: str,
                       notional_cents: int, risk_cents: int,
                       leverage_x10: int) -> dict:
        """Always returns the vault's decision; `granted` says which way it went."""
        return self.call(agent, "request_permit", {
            "signal_hash": signal_hash[:80], "side": side.capitalize(),
            "notional_cents": int(notional_cents), "risk_cents": int(risk_cents),
            "leverage_x10": int(leverage_x10)})

    def settle(self, agent: str, permit_id: int, pnl_cents: int) -> None:
        self.call(agent, "settle", {"permit_id": int(permit_id),
                                    "pnl_cents": int(pnl_cents)})

    def report_equity(self, equity_cents: int, reporter: str = "reporter") -> bool:
        return bool(self.call(reporter, "report_equity",
                              {"equity_cents": int(equity_cents)}))

    def status(self) -> dict:
        return self.view("get_status")

    def limits(self) -> dict:
        return self.view("get_limits")

    def agent(self, agent: str) -> Optional[dict]:
        return self.view("get_agent", {"agent": self.account(agent)})

    def decisions(self, start: int = 0, limit: int = 100) -> list:
        return self.view("list_decisions", {"from": start, "limit": limit}) or []

    # ── the sandboxed signer ──────────────────────────────────────────────────

    def sign_anchor(self, agent: str, *, nonce: int, max_priority_fee_wei: int,
                    max_fee_wei: int, gas_limit: int, data: bytes) -> dict:
        return self.call(agent, "sign_anchor", {
            "nonce": int(nonce), "max_priority_fee_wei": str(int(max_priority_fee_wei)),
            "max_fee_wei": str(int(max_fee_wei)), "gas_limit": int(gas_limit),
            "data_hex": "0x" + data.hex()}, tgas=300)

    def anchor_tx_hash(self, *, nonce: int, max_priority_fee_wei: int,
                       max_fee_wei: int, gas_limit: int, data: bytes) -> str:
        return self.view("anchor_tx_hash", {
            "nonce": int(nonce), "max_priority_fee_wei": str(int(max_priority_fee_wei)),
            "max_fee_wei": str(int(max_fee_wei)), "gas_limit": int(gas_limit),
            "data_hex": "0x" + data.hex()})


# ── EVM transaction assembly (no keys involved) ──────────────────────────────

def signing_hash(chain_id: int, nonce: int, tip: int, fee: int, gas: int,
                 data: bytes) -> bytes:
    import rlp
    from eth_utils import keccak
    body = rlp.encode([chain_id, nonce, tip, fee, gas, bytes.fromhex(ANCHOR[2:]),
                       0, data, []])
    return keccak(b"\x02" + body)


def assemble(chain_id: int, nonce: int, tip: int, fee: int, gas: int,
             data: bytes, signature: dict) -> bytes:
    """The signed EIP-1559 transaction, from the MPC contract's signature."""
    import rlp
    r = int(signature["big_r"]["affine_point"][2:], 16)
    s = int(signature["s"]["scalar"], 16)
    v = int(signature["recovery_id"])
    return b"\x02" + rlp.encode([chain_id, nonce, tip, fee, gas,
                                 bytes.fromhex(ANCHOR[2:]), 0, data, [], v, r, s])


def recover(tx_hash: bytes, signature: dict) -> str:
    from eth_account import Account
    r = int(signature["big_r"]["affine_point"][2:], 16)
    s = int(signature["s"]["scalar"], 16)
    return Account._recover_hash(tx_hash, vrs=(int(signature["recovery_id"]), r, s))


def cached_address(account: str) -> Optional[str]:
    try:
        return json.loads(ADDRESS_CACHE.read_text()).get(account)
    except (OSError, ValueError):
        return None


def remember_address(account: str, address: str) -> None:
    ADDRESS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        book = json.loads(ADDRESS_CACHE.read_text())
    except (OSError, ValueError):
        book = {}
    book[account] = address
    ADDRESS_CACHE.write_text(json.dumps(book, indent=2, sort_keys=True) + "\n")


class AnchorSigner:
    """Sends anchoring calls from an agent's vault-controlled address."""

    def __init__(self, vault: Vault, *, rpc_url: str = "", chain_id: int = 0,
                 w3: Any = None) -> None:
        self.vault = vault
        self.chain_id = int(chain_id or os.environ.get("NVNM_CHAIN_ID", 787223))
        if w3 is None:
            from web3 import Web3
            url = rpc_url or os.environ.get("NVNM_RPC_URL",
                                            "https://rpc.nvnm.testnet.nvnmchain.io")
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
        self.w3 = w3
        sys.path.insert(0, str(ROOT))
        from zcastor.anchor.nvnm import ANCHORING_ABI
        self.contract = w3.eth.contract(address=w3.to_checksum_address(ANCHOR),
                                        abi=ANCHORING_ABI)

    def address(self, agent: str) -> str:
        """
        The agent's EVM address. Learned from its first signature (the address
        that signature recovers to) and remembered in data/near/addresses.json.
        Getting it costs one signature over a transaction that is never sent.
        """
        account = self.vault.account(agent)
        known = cached_address(account)
        if known:
            return known
        data = bytes.fromhex("64d25295")            # addRecord selector, no arguments
        fields = dict(nonce=0, max_priority_fee_wei=0, max_fee_wei=1, gas_limit=21000)
        sig = self.vault.sign_anchor(agent, data=data, **fields)
        addr = recover(signing_hash(self.chain_id, 0, 0, 1, 21000, data), sig)
        remember_address(account, addr)
        return addr

    def _fees(self) -> tuple[int, int]:
        base = int(self.w3.eth.get_block("latest").get("baseFeePerGas") or 0)
        try:
            tip = int(self.w3.eth.max_priority_fee)
        except Exception:  # noqa: BLE001
            tip = 10 ** 9
        return tip, base * 2 + tip

    def send(self, agent: str, function: str, args: list) -> dict:
        """Encode, have the vault sign, check, broadcast, wait for the receipt."""
        sender = self.address(agent)
        fn = getattr(self.contract.functions, function)(*args)
        data = bytes.fromhex(fn.build_transaction(
            {"from": sender, "nonce": 0, "gas": 1, "chainId": self.chain_id,
             "maxFeePerGas": 1, "maxPriorityFeePerGas": 0})["data"][2:])
        cap = int(self.vault.limits()["evm_gas_limit_cap"])
        try:
            gas = min(int(fn.estimate_gas({"from": sender}) * 1.3), cap)
        except Exception as exc:  # noqa: BLE001
            raise VaultError(f"gas estimate failed for {function} from {sender}: {exc}. "
                             "Does the address hold nUSD for gas, and does it have "
                             "a role on the registry?") from None
        nonce = self.w3.eth.get_transaction_count(sender)
        tip, fee = self._fees()
        fields = dict(nonce=nonce, max_priority_fee_wei=tip, max_fee_wei=fee,
                      gas_limit=gas, data=data)

        local = signing_hash(self.chain_id, nonce, tip, fee, gas, data)
        onchain = self.vault.anchor_tx_hash(**fields)       # raises if not allowed
        if onchain != local.hex():
            raise VaultError(f"hash mismatch: vault {onchain}, local {local.hex()}")
        sig = self.vault.sign_anchor(agent, **fields)
        if recover(local, sig).lower() != sender.lower():
            raise VaultError("signature does not recover to the agent's address")

        raw = assemble(self.chain_id, nonce, tip, fee, gas, data, sig)
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        from zcastor.anchor.nvnm import record_ids_from_receipt, registry_id_from_receipt
        record_id, index = record_ids_from_receipt(receipt)
        return {"tx": tx_hash.hex(), "status": int(receipt.get("status", 0)),
                "block": int(receipt.get("blockNumber", 0)), "from": sender,
                "gas_used": int(receipt.get("gasUsed", 0)),
                "record_id": record_id, "index": index,
                "registry_id": registry_id_from_receipt(receipt)}

    def add_record(self, agent: str, registry_id: int, checksum: str, uri: str,
                   metadata: str = "") -> dict:
        if len(checksum) != 64:
            raise VaultError("checksum must be 64 hex characters (sha256)")
        record = (uri, checksum, "sha256", metadata or checksum[:16], "", "Active",
                  0, 0, False, int(registry_id))
        return self.send(agent, "addRecord", [record])


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cents(dollars: float) -> int:
    return int(round(float(dollars) * 100))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="near/vault_client.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("permit")
    p.add_argument("agent")
    p.add_argument("--signal", required=True)
    p.add_argument("--side", choices=("Buy", "Sell"), required=True)
    p.add_argument("--notional", type=float, required=True, help="US dollars")
    p.add_argument("--risk", type=float, required=True, help="US dollars")
    p.add_argument("--leverage", type=float, default=1.0)
    p = sub.add_parser("settle")
    p.add_argument("agent")
    p.add_argument("permit_id", type=int)
    p.add_argument("--pnl", type=float, required=True, help="US dollars, signed")
    p = sub.add_parser("equity")
    p.add_argument("dollars", type=float)
    p = sub.add_parser("decisions")
    p.add_argument("--from", dest="start", type=int, default=0)
    p = sub.add_parser("address")
    p.add_argument("agent")
    p = sub.add_parser("registry")
    p.add_argument("agent")
    p.add_argument("name")
    p = sub.add_parser("record")
    p.add_argument("agent")
    p.add_argument("registry_id", type=int)
    p.add_argument("checksum")
    p.add_argument("uri")
    a = ap.parse_args(argv)

    try:
        v = Vault()
        if a.cmd == "status":
            out = {"status": v.status(), "limits": v.limits()}
        elif a.cmd == "permit":
            out = v.request_permit(a.agent, signal_hash=a.signal, side=a.side,
                                   notional_cents=_cents(a.notional),
                                   risk_cents=_cents(a.risk),
                                   leverage_x10=int(round(a.leverage * 10)))
        elif a.cmd == "settle":
            v.settle(a.agent, a.permit_id, _cents(a.pnl))
            out = v.agent(a.agent)
        elif a.cmd == "equity":
            out = {"halted": v.report_equity(_cents(a.dollars)), "status": v.status()}
        elif a.cmd == "decisions":
            out = v.decisions(a.start)
        elif a.cmd == "address":
            out = {"agent": v.account(a.agent), "evm_address": AnchorSigner(v).address(a.agent)}
        elif a.cmd == "registry":
            out = AnchorSigner(v).send(a.agent, "addRegistry", [a.name, "", ""])
        else:
            out = AnchorSigner(v).add_record(a.agent, a.registry_id, a.checksum, a.uri)
    except VaultError as exc:
        print(f"refused: {exc.reason}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
