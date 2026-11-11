"""
NVNM Chain anchoring backend.

STILL NEVER EXECUTED AGAINST THE CHAIN. What changed is that the untestable part
is now one small class instead of the whole module.

Everything that encodes a decision — which arguments go where, how metadata is
serialised, which failures are transient — is testable against a fake transport.
`Web3Transport` is the only piece that needs a node, and it is deliberately thin
enough to read in one sitting. When the first real transaction lands, that is
where the surprises will be.

No custom contract. The chain's anchoring module is a precompile at 0x…0A00 and
provides registries, checksummed records, versioning, status and role-based
access. `NvnmReceipts.sol` and the three Xenea-era contracts are archived.

The key never leaves the process. There is no gateway in the write path, so
custody does not change.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from .base import (
    AnchorBackend,
    AnchorRejected,
    AnchorResult,
    AnchorUnavailable,
    PendingRecord,
)

logger = logging.getLogger("zcastor.anchor.nvnm")

ANCHORING_PRECOMPILE = "0x0000000000000000000000000000000000000A00"

# Observed 28 Aug 2026. The fee asset differs by environment — testnet charges
# in NVNM, mainnet in mmUSD — so "which token pays for gas" has no single
# answer and must not be hardcoded.
ENVIRONMENTS = {
    "testnet": {
        "chain_id": 787111,                    # 0xc02a7
        "rpc_url": "https://evm.testnet.nvnmchain.io",
        "explorer": "https://explorer.evm.testnet.nvnmchain.io",
        "gas_symbol": "NVNM",
    },
    "mainnet": {
        "chain_id": 1611,
        "rpc_url": "",                         # not yet confirmed
        "explorer": "",
        "gas_symbol": "mmUSD",
    },
    # The sovereign L1 testnet, live 1 Oct 2026. Tempo stack: no native coin,
    # gas is paid in nUSD (TIP-20 at 0x20C0...0000), so eth_getBalance returns
    # a placeholder; read nUSD.balanceOf instead. Anchoring is a contract at
    # the same 0x...0A00 address rather than a precompile.
    "l1-testnet": {
        "chain_id": 787223,
        "rpc_url": "https://rpc.nvnm.testnet.nvnmchain.io",
        "explorer": "https://explorer.nvnm.testnet.nvnmchain.io",
        "gas_symbol": "nUSD",
    },
}

# CORRECTED 28 Aug 2026 against the live chain via the NVNM MCP server.
# The previous version, written from the documentation, was wrong in five ways:
# registry was a string name, record ids were our checksums, empty metadata was
# sent, the status vocabulary was invented, and none of it had ever run.
ANCHORING_ABI = json.loads("""[
  {"name":"addRegistry","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"name","type":"string"},
             {"name":"description","type":"string"},
             {"name":"metadata","type":"string"}],
   "outputs":[{"name":"registryId","type":"uint64"}]},

  {"name":"grantRole","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"registryId","type":"uint64"},
             {"name":"checksum","type":"string"},
             {"name":"account","type":"address"},
             {"name":"role","type":"string"}],
   "outputs":[{"name":"success","type":"bool"}]},

  {"name":"revokeRole","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"registryId","type":"uint64"},
             {"name":"checksum","type":"string"},
             {"name":"account","type":"address"},
             {"name":"role","type":"string"}],
   "outputs":[{"name":"success","type":"bool"}]},

  {"name":"addRecord","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"record","type":"tuple","components":[
       {"name":"uri","type":"string"},
       {"name":"checksum","type":"string"},
       {"name":"checksumAlgo","type":"string"},
       {"name":"metadata","type":"string"},
       {"name":"timestamp","type":"string"},
       {"name":"status","type":"string"},
       {"name":"recordId","type":"uint64"},
       {"name":"index","type":"uint64"},
       {"name":"isLatest","type":"bool"},
       {"name":"registryId","type":"uint64"}]}],
   "outputs":[{"name":"recordId","type":"uint64"}]},

  {"name":"updateRecordStatus","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"registryId","type":"uint64"},
             {"name":"recordId","type":"uint64"},
             {"name":"index","type":"uint64"},
             {"name":"status","type":"string"}],
   "outputs":[{"name":"success","type":"bool"}]}
]""")

# Function selectors, for checking our ABI against calldata the chain accepts.
# Only addRegistry has been verified against a real prepared transaction; the
# rest are derived from the same conventions and remain unconfirmed.
# Selectors CONFIRMED by at least one successful on-chain call. Anything not
# in here is a guess from the documentation, and the documentation has been
# wrong about this precompile more than once.
SELECTORS_VERIFIED = {
    # Verified against calldata the MCP server produced.
    "addRegistry": "318b38b1",          # (string,string,string)
    # From the module documentation's function table, 28 Aug 2026. Not yet
    # confirmed against a landed transaction, but each corrects a signature
    # that demonstrably reverted.
    "grantRole": "b8fdd1a7",            # (uint64,string,address,string)
    "revokeRole": "acd58bc7",           # (uint64,string,address,string)
    # The documented struct (leading `string registry`, no registryId) hashes
    # to 9b7b7869. That selector succeeded on this chain until 11 June 2026 and
    # has failed every time since; 64d25295 has been in continuous use since
    # 14 August. Confirmed by decoding calldata from calls that succeeded today
    # and matching the selector exactly.
    "addRecord": "64d25295",            # (uri,checksum,algo,metadata,ts,
                                        #  status,recordId,index,isLatest,
                                        #  registryId)
    # Decoded 16 Sep 2026 from calls that succeeded on chain, the same way
    # addRecord was. The documented five-argument form carrying a checksum
    # (1dccdf99) has never appeared on chain — not one call, successful or
    # failed. There is no checksum argument; a record is addressed by
    # (registryId, recordId, index).
    "updateRecordStatus": "97b40c25",   # (uint64,uint64,uint64,string)
}

# The event addRegistry emits, observed on testnet 28 Aug 2026.
#   topic0  event signature
#   topic1  creator address (indexed)
#   data    [uint256 registryId, string offset, string name]
REGISTRY_ADDED_TOPIC = (
    "181791bc379acedd3615cf065d3c275dfa6a3c4614c9065d54c98773f576108d"
)


# The event addRecord emits, observed on testnet 31 Aug 2026.
#   topic0  event signature
#   topic1  sender (indexed)
#   data    [uint64 registryId, uint64 recordId, uint64 index, string checksum]
RECORD_ADDED_TOPIC = (
    "1a3295fa8cc0e28c95d21912c9e6958f3bc740231781f7640ad885c972a352fd"
)


def _log_words(receipt: Any, topic: str) -> list[int]:
    """Data words of the first log matching a topic, as integers."""
    for log in (receipt.get("logs") or []):
        topics = [t.hex() if hasattr(t, "hex") else str(t)
                  for t in log.get("topics", [])]
        if not topics or topics[0].replace("0x", "").lower() != topic:
            continue
        data = log.get("data")
        data = data.hex() if hasattr(data, "hex") else str(data or "")
        body = data[2:] if data.startswith("0x") else data
        return [int(body[i:i + 64], 16)
                for i in range(0, len(body) - 63, 64)]
    return []


def record_ids_from_receipt(receipt: Any) -> tuple[int, int]:
    """
    The (recordId, index) the chain assigned to a new record.

    Both appear only here. Without them a record cannot be addressed again, so
    every status update fails — which is exactly what happened while this was
    unread. Note recordId is the SECOND word: the first is the registry.
    """
    words = _log_words(receipt, RECORD_ADDED_TOPIC)
    if len(words) < 3:
        return 0, 0
    return words[1], words[2]


def registry_id_from_receipt(receipt: Any) -> int:
    """
    Read the assigned registry ID out of a receipt.

    The ID is not predictable by the sender and appears only here, so a client
    that cannot decode this cannot grant roles on what it just created.
    """
    for log in (receipt.get("logs") or []):
        topics = [t.hex() if hasattr(t, "hex") else str(t)
                  for t in log.get("topics", [])]
        if not topics:
            continue
        if topics[0].replace("0x", "").lower() != REGISTRY_ADDED_TOPIC:
            continue
        data = log.get("data")
        data = data.hex() if hasattr(data, "hex") else str(data or "")
        body = data[2:] if data.startswith("0x") else data
        if len(body) >= 64:
            return int(body[:64], 16)
    return 0


# Substrings meaning the request itself is wrong and will never succeed.
_PERMANENT_MARKERS = (
    "unauthorized", "not authorized", "permission", "role",
    "registry not found", "invalid", "already exists", "revert",
)


# ── transport ─────────────────────────────────────────────────────────────────

class Transport(ABC):
    """Builds, signs and submits one call. The only part that needs a node."""

    address: str = ""

    @abstractmethod
    def call(self, function: str, args: list) -> dict:
        """Returns {"tx": hash, "status": 1|0, "block": n}. Raises on failure."""


class Web3Transport(Transport):
    def __init__(self, *, rpc_url: str, private_key: str, chain_id: int,
                 gas_limit: int = 3_000_000,
                 precompile: str = ANCHORING_PRECOMPILE,
                 legacy_tx: bool = False) -> None:
        try:
            from eth_account import Account            # noqa: PLC0415
            from web3 import Web3                      # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "web3 and eth-account are required for the NVNM backend. "
                "Use NullBackend for offline runs."
            ) from exc

        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._account = Account.from_key(private_key)
        self._chain_id = chain_id
        self._gas_limit = gas_limit
        self._legacy_tx = legacy_tx
        self._contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(precompile), abi=ANCHORING_ABI)
        self.address = self._account.address

    def _fee_params(self) -> dict:
        """Base fee plus a tip, read from the chain rather than guessed."""
        try:
            latest = self._w3.eth.get_block("latest")
            base = int(latest.get("baseFeePerGas") or 0)
        except Exception:  # noqa: BLE001
            base = 0
        if not base:
            return {}                       # pre-1559 node; let web3 decide
        try:
            tip = int(self._w3.eth.max_priority_fee)
        except Exception:  # noqa: BLE001
            tip = self._w3.to_wei(1, "gwei")
        return {"maxFeePerGas": base * 2 + tip, "maxPriorityFeePerGas": tip}

    # Ceiling for an estimated write. The L1 testnet (787223) prices new
    # storage far above Ethereum: addRegistry alone estimated 3.62M gas on
    # 2 Oct 2026, past the old flat 3M limit. Estimating per call, with
    # headroom, keeps a fixed limit from silently failing every write.
    GAS_HEADROOM = 1.3
    GAS_CEILING = 30_000_000

    def _gas_for(self, fn) -> int:
        """Estimated gas plus headroom; the configured limit if estimation fails."""
        try:
            estimate = int(fn.estimate_gas({"from": self._account.address}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("gas estimate failed (%s); using limit %s",
                           exc, self._gas_limit)
            return self._gas_limit
        return min(max(int(estimate * self.GAS_HEADROOM), self._gas_limit),
                   self.GAS_CEILING)

    def call(self, function: str, args: list) -> dict:
        fn = getattr(self._contract.functions, function)(*args)
        params = {
            "from": self._account.address,
            "nonce": self._w3.eth.get_transaction_count(self._account.address),
            "chainId": self._chain_id,
            "gas": self._gas_for(fn),
        }
        # The chain's own prepare endpoints default to EIP-1559 (type 2), so
        # that is the default here too. A legacy type-0 transaction is still
        # available for a node that rejects type 2.
        if not self._legacy_tx:
            params.update(self._fee_params())
        tx = fn.build_transaction(params)
        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        # addRegistry and addRecord emit different events; read whichever is
        # present rather than assuming which call this was.
        record_id, index = record_ids_from_receipt(receipt)
        if not record_id:
            record_id = registry_id_from_receipt(receipt)
            index = 1
        return {"tx": tx_hash.hex(),
                "status": int(receipt.get("status", 0)),
                "block": int(receipt.get("blockNumber", 0)),
                "record_id": record_id,
                "index": index}


# ── backend ───────────────────────────────────────────────────────────────────

class NvnmBackend(AnchorBackend):
    name = "nvnm"

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    @property
    def address(self) -> str:
        return self.transport.address

    # ── records ───────────────────────────────────────────────────────────────

    def add_record(self, entry: PendingRecord) -> AnchorResult:
        if not entry.checksum:
            raise AnchorRejected("record has no checksum")
        if not entry.registry_id:
            raise AnchorRejected(
                f"record has no numeric registry_id (name {entry.registry!r}). "
                f"addRecord identifies the registry by ID, not by name.")
        if len(entry.checksum) != 64:
            # A prefixed or truncated hash would anchor a value that never
            # matches the published file — worse than not anchoring at all,
            # because it looks verifiable.
            raise AnchorRejected(
                f"checksum must be 64 hex chars, got {len(entry.checksum)}")

        # One Record struct. The registry is identified by NUMERIC ID at the
        # END of the tuple — not by a leading name string, which is what the
        # documentation describes and what the chain stopped accepting in June.
        record = (
            entry.uri,
            entry.checksum,
            entry.checksum_algo or "sha256",
            self._metadata(entry.metadata, fallback=entry.checksum[:16]),
            "",                                               # timestamp
            entry.status or "Active",
            0,                                                # recordId
            0,                                                # index
            False,                                            # isLatest
            int(entry.registry_id),
        )
        return self._send("addRecord", [record])

    def update_status(self, entry: PendingRecord) -> AnchorResult:
        if not entry.record_id:
            raise AnchorRejected("status update has no record_id")
        if not entry.registry_id:
            raise AnchorRejected("status update has no registry_id")
        # A record is addressed by (registryId, recordId, index) — no checksum.
        # The index identifies WHICH version's status changes; records observed
        # on chain use index 1 for their first version, so 0 would address
        # nothing.
        return self._send(
            "updateRecordStatus",
            [int(entry.registry_id), int(entry.record_id),
             int(entry.index or 1), entry.status],
            record_id=int(entry.record_id))

    # ── administration (one-time, run from a script — never from the engine) ──

    def add_registry(self, name: str, description: str = "",
                     metadata: str = "") -> AnchorResult:
        """
        Create a registry. The chain assigns its id — capture it from the
        result, because it is the only safe way to refer to the registry again.

        THREE string arguments. Verified 28 Aug 2026 by decoding the calldata
        the MCP server produces: selector 318b38b1 is
        addRegistry(string,string,string). The two-argument version we had
        hashes to 90dee1b1, which the precompile does not implement, so every
        call reverted after being mined. Unlike an unknown method on a normal
        contract, a precompile gives no useful revert reason, so this cost four
        transactions to find.

        Metadata may be empty here, unlike addRecord where it is rejected.
        """
        return self._send("addRegistry", [name, description, metadata])

    def grant_role(self, registry_id: int, account: str,
                   role: str = "editor", checksum: str = "") -> AnchorResult:
        """
        Grant a role. An empty checksum means registry-level scope; a specific
        checksum scopes the role to one record.

        The checksum is the SECOND argument, before the account. Our first
        version omitted it entirely and reverted six times.
        """
        if role not in ("admin", "editor"):
            raise AnchorRejected(f"unknown role {role!r}")
        return self._send("grantRole",
                          [int(registry_id), checksum, account, role])

    def revoke_role(self, registry_id: int, account: str,
                    role: str = "editor", checksum: str = "") -> AnchorResult:
        return self._send("revokeRole",
                          [int(registry_id), checksum, account, role])

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _metadata(metadata: dict, fallback: str = "record") -> str:
        """
        Canonical JSON, so identical metadata always encodes identically.

        The precompile REJECTS an empty metadata string, and "{}" counts as
        empty — so a record with nothing to say still needs a label rather
        than an empty object.
        """
        if not metadata:
            return json.dumps({"label": fallback}, separators=(",", ":"))
        return json.dumps(metadata, sort_keys=True, separators=(",", ":"))

    def _send(self, function: str, args: list,
              record_id: int = 0) -> AnchorResult:
        try:
            result = self.transport.call(function, args)
        except Exception as exc:  # noqa: BLE001
            raise self._classify(exc) from exc

        if result.get("status") != 1:
            # Mined and reverted. The chain is up, so this one is ours.
            raise AnchorRejected(
                f"{function} reverted: {result.get('tx', 'unknown tx')}")

        return AnchorResult(
            transaction=str(result.get("tx", "")),
            # The chain assigns the record id; fall back to what we were given
            # for calls (status updates) that target an existing one.
            record_id=int(result.get("record_id") or record_id or 0),
            index=int(result.get("index") or 1),
        )

    @staticmethod
    def _classify(exc: Exception) -> Exception:
        """
        Transient or permanent?

        Wrong in the safe direction costs a retry. Wrong in the other direction
        parks a record that could have landed — the exact failure this layer
        exists to prevent — so anything unrecognised is treated as transient.
        """
        text = str(exc).lower()
        if any(marker in text for marker in _PERMANENT_MARKERS):
            return AnchorRejected(str(exc))
        return AnchorUnavailable(f"{type(exc).__name__}: {exc}")


# ── registry and role definitions ─────────────────────────────────────────────

# Registry NAMES for creation and logging. The numeric ids the chain assigns
# are the authoritative reference and live in config/runtime.json once created.
REGISTRIES = {
    "afritensor-policy": "Pipeline identity: model version, gate configuration "
                         "and thresholds. Proof of Process.",
    "afritensor-decisions": "Daily decision log — every gate verdict, including "
                            "every refusal.",
    "afritensor-dossiers": "One DGML dossier per executed trade.",
    "afritensor-archive": "Historical Xenea-era dossiers, retained as lineage.",
}

# Which agent may write where. Scope is enforced by the chain via role grants,
# not by our code — revoking an agent is one transaction.
#
# OPEN QUESTION FOR INVENIAM: whether KYA expects one credential per operating
# system or one per accountable component. This models four; it may need to
# collapse to one.
AGENT_ROLES = {
    "signal": ["afritensor-decisions"],
    "execution": ["afritensor-decisions", "afritensor-dossiers"],
    "watcher": ["afritensor-decisions", "afritensor-dossiers"],
    "oracle": ["afritensor-dossiers"],
}


def build_backend(*, private_key: str, environment: str = "testnet",
                  rpc_url: str = "", chain_id: int = 0,
                  gas_limit: int = 3_000_000,
                  legacy_tx: bool = False) -> NvnmBackend:
    env = ENVIRONMENTS.get(environment, {})
    rpc = rpc_url or env.get("rpc_url", "")
    cid = int(chain_id or env.get("chain_id", 0))
    if not rpc or not cid:
        raise ValueError(
            f"environment {environment!r} needs an rpc_url and chain_id; "
            f"known: {sorted(ENVIRONMENTS)}")
    return NvnmBackend(Web3Transport(rpc_url=rpc, private_key=private_key,
                                     chain_id=cid, gas_limit=gas_limit,
                                     legacy_tx=legacy_tx))
