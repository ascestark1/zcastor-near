#!/usr/bin/env python3
"""
Judge mode: check the desk's claims with no keys and no accounts.

    python near/judge.py            # offline: maths and recorded evidence only
    python near/judge.py --live     # also reads the deployed vault on NEAR testnet

Each step prints what it checks, then PASS or FAIL. Offline steps need nothing
but this repository and its Python requirements. `--live` makes read-only view
calls to a public NEAR RPC; it signs nothing.

What is checked:

 1. The vault's transaction hash is Ethereum's. The EIP-1559 signing hash
    computed here from the recorded request equals the one the vault logged on
    chain when it asked NEAR Chain Signatures to sign.
 2. The Chain Signatures signature is real and belongs to the agent. The
    recorded MPC signature recovers to the agent's address, and the assembled
    transaction is one any Ethereum node would accept as coming from it.
 3. The private inference was signed by the enclave over the exact bytes. The
    recorded NEAR AI Cloud signature covers `model:request-hash:response-hash`
    and recovers to the signing address named in the attestation.
 4. The engine obeys the vault. A real engine, with the vault's rules applied
    locally, fills a trade the rules allow and records a refusal for one they
    don't, without placing an order.
 5. (--live) The vault on testnet holds the limits and the decision history
    that near/evidence/deployment.json describes.

The local rules in step 4 mirror `refusal_reason` in near/vault/src/lib.rs so
the engine can be exercised offline. The contract is the authority; its own 28
tests are `cd near/vault && cargo test`.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "near"))
EVIDENCE = ROOT / "near" / "evidence"

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok)))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f"\n         {detail}" if detail else ""))
    return bool(ok)


def load(name: str) -> dict:
    return json.loads((EVIDENCE / name).read_text())


def step_hash_and_signature() -> None:
    import vault_client as vc
    ev = load("chain_signature.json")
    r = ev["request"]
    data = bytes.fromhex(r["data_hex"][2:])
    fields = (r["chain_id"], r["nonce"], int(r["max_priority_fee_wei"]),
              int(r["max_fee_wei"]), r["gas_limit"], data)

    print("\n1. The vault builds the same hash Ethereum would sign")
    local = vc.signing_hash(*fields)
    check("hash computed here equals the hash the vault logged on NEAR",
          local.hex() == ev["hash_logged_by_vault"], local.hex())

    print("\n2. NEAR Chain Signatures signed it, as the agent")
    signer = vc.recover(local, ev["mpc_response"])
    check("the MPC signature recovers to the agent's address",
          signer == ev["agent_evm_address"], signer)
    from eth_account import Account
    raw = vc.assemble(*fields, ev["mpc_response"])
    check("the assembled transaction is valid and from that address",
          Account.recover_transaction(raw) == ev["agent_evm_address"],
          f"type-2 transaction, {len(raw)} bytes, to {r['to']}")
    check("the transaction can only be an anchoring call",
          r["to"].lower().endswith("0a00") and r["value"] == 0
          and r["data_hex"][2:10] in ("64d25295", "97b40c25"),
          f"to the anchoring module, value 0, selector {r['data_hex'][2:10]} (addRecord)")


def step_private_inference() -> None:
    print("\n3. The enclave signed the exact request and response")
    ev = load("private_inference.json")
    v = ev["verification"]
    model, req, resp = v["signed_text"].rsplit(":", 2)
    check("signed text is model : request hash : response hash",
          model == ev["model"] and req == ev["request_sha256"]
          and resp == ev["response_sha256"], v["signed_text"][:70] + "...")
    from eth_account import Account
    from eth_account.messages import encode_defunct
    signer = Account.recover_message(encode_defunct(text=v["signed_text"]),
                                     signature=v["signature"])
    check("the signature recovers to the enclave's signing address",
          signer.lower() == v["signing_address"].lower(), signer)
    check("only a coarse description of the signal was sent",
          set(ev["sent"]) <= {"symbol", "direction", "timeframe", "session", "regime",
                              "tf_regime", "confidence", "zone_status", "zone_phase"},
          "fields sent: " + ", ".join(sorted(ev["sent"])))
    print("         not checked here: the Intel TDX quote inside the attestation "
          f"(sha256 {v['attestation_sha256'][:16]}...); use nearai-cloud-verifier")


class LocalRules:
    """The vault's refusal rules, applied locally so step 4 needs no network."""

    def __init__(self, limits: dict, agent: dict) -> None:
        self.limits, self.agent_cfg = limits, agent
        self.open_risk = 0
        self.decisions: list[dict] = []

    def request_permit(self, agent, *, signal_hash, side, notional_cents,
                       risk_cents, leverage_x10):
        a, lim = self.agent_cfg, self.limits
        reason = None
        if risk_cents == 0 or notional_cents == 0:
            reason = "zero_size"
        elif leverage_x10 > a["max_leverage_x10"] or leverage_x10 > lim["max_leverage_x10"]:
            reason = "over_leverage"
        elif notional_cents > a["max_notional_cents"]:
            reason = "over_agent_notional"
        elif risk_cents > lim["per_trade_risk_cents"]:
            reason = "over_trade_risk"
        elif self.open_risk + risk_cents > a["risk_budget_cents"]:
            reason = "over_agent_budget"
        elif self.open_risk + risk_cents > lim["total_risk_cents"]:
            reason = "over_desk_budget"
        d = {"id": len(self.decisions), "granted": reason is None,
             "reason": reason or "within_limits", "risk_cents": risk_cents,
             "notional_cents": notional_cents}
        if reason is None:
            self.open_risk += risk_cents
        self.decisions.append(d)
        return d

    def settle(self, agent, permit_id, pnl_cents):
        self.open_risk -= self.decisions[permit_id]["risk_cents"]


def step_engine_obeys() -> None:
    print("\n4. The engine fills what the vault allows and records what it refuses")
    sys.path.insert(0, str(ROOT / "tests" / "execution"))
    import logging
    logging.disable(logging.CRITICAL)
    from test_engine import build, signal
    from zcastor.execution.vaulted import VaultedOrders
    from zcastor.gates.base import Verdict

    limits = {"total_risk_cents": 20000, "per_trade_risk_cents": 5000, "max_leverage_x10": 30}
    agent = {"risk_budget_cents": 10000, "max_notional_cents": 500000, "max_leverage_x10": 30}
    for volume, expect in ((0.01, "fills"), (2.0, "refused")):
        with tempfile.TemporaryDirectory() as d:
            engine = build(d)[0]
            engine.orders.volume = volume
            rules = LocalRules(limits, agent)
            engine.orders = VaultedOrders(engine.orders, rules, agent="btc", leverage=3,
                                          store=Path(d) / "permits.json")
            trace = engine.process(signal())
            dec = rules.decisions[-1] if rules.decisions else {}
            placed = len(engine.orders.inner.placed)
            if expect == "fills":
                check(f"a {volume} BTC trade inside the limits is granted and filled",
                      trace.action == Verdict.ALLOW and placed == 1 and dec.get("granted"),
                      f"risk ${dec.get('risk_cents', 0) / 100:.2f} of a $50 per-trade limit")
            else:
                check(f"a {volume} BTC trade over the limits is refused; no order is placed",
                      trace.action == Verdict.SUPPRESS and placed == 0
                      and trace.reason.startswith("vault_refused:"),
                      f"recorded as {trace.reason}")
    logging.disable(logging.NOTSET)


RPCS = [u for u in (os.environ.get("NEAR_RPC_URL", ""), "https://rpc.testnet.near.org",
                    "https://test.rpc.fastnear.com", "https://rpc.testnet.fastnear.com") if u]


def near_view(account: str, method: str, args: dict):
    """A read-only view call. Tries each public RPC in turn (NEAR_RPC_URL first)."""
    last: Exception = RuntimeError("no RPC configured")
    for rpc in RPCS:
        try:
            return _view(rpc, account, method, args)
        except Exception as exc:  # noqa: BLE001
            last = RuntimeError(f"{rpc}: {exc}")
    raise last


def _view(rpc: str, account: str, method: str, args: dict):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "query", "params": {
        "request_type": "call_function", "finality": "final", "account_id": account,
        "method_name": method,
        "args_base64": base64.b64encode(json.dumps(args).encode()).decode()}}).encode()
    req = urllib.request.Request(rpc, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "zcastor-near-judge"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.loads(resp.read())
    if "error" in out or "error" in out.get("result", {}):
        raise RuntimeError(str(out.get("error") or out["result"]["error"])[:200])
    return json.loads(bytes(out["result"]["result"]).decode())


def step_live() -> None:
    print("\n5. The vault on NEAR testnet (read-only)")
    dep = load("deployment.json")
    try:
        status = near_view(dep["vault"], "get_status", {})
        limits = near_view(dep["vault"], "get_limits", {})
        decisions = near_view(dep["vault"], "list_decisions", {"from": 0, "limit": 5})
        selectors = near_view(dep["vault"], "get_selectors", {})
    except Exception as exc:  # noqa: BLE001
        check("the vault answered", False, f"could not read {dep['vault']}: {exc}")
        return
    check("owner and signer are as deployed",
          status["owner"] == dep["owner"] and status["mpc_contract"] == dep["mpc_contract"],
          f"owner {status['owner']}, signer {status['mpc_contract']}")
    check("limits are on chain",
          limits["per_trade_risk_cents"] > 0 and limits["deadman_bps"] > 0,
          f"desk ${limits['total_risk_cents'] / 100:.0f}, per trade "
          f"${limits['per_trade_risk_cents'] / 100:.0f}, leverage "
          f"{limits['max_leverage_x10'] / 10}x, halt at {limits['deadman_bps'] / 100:.0f}% down")
    first = {d["id"]: d for d in decisions}
    check("decision 0 was granted and decision 1 refused as over_trade_risk",
          first.get(0, {}).get("granted") is True
          and first.get(1, {}).get("reason") == "over_trade_risk",
          f"{status['decisions']} decisions: {status['granted']} granted, "
          f"{status['refused']} refused")
    check("the signer's allowlist is the two anchoring calls",
          sorted(selectors) == ["64d25295", "97b40c25"], ", ".join(selectors))


def main(argv: list[str]) -> int:
    print("Zcastor desk: judge mode"
          + ("" if "--live" in argv else "  (offline; add --live to read the testnet vault)"))
    step_hash_and_signature()
    step_private_inference()
    step_engine_obeys()
    if "--live" in argv:
        step_live()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed"
          + ("" if not failed else "; FAILED: " + "; ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
