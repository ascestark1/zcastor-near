"""
The desk's vault client, against a fake `near` CLI. The CLI outputs used here
are the real ones from the testnet deploy of 11 Nov 2026.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "near"))

import vault_client as vc  # noqa: E402

GRANTED_STDOUT = """{
  "agent": "btc.zcastor-desk.testnet",
  "granted": true,
  "id": 0,
  "leverage_x10": 30,
  "notional_cents": 100000,
  "pnl_cents": 0,
  "reason": "within_limits",
  "risk_cents": 4000,
  "settled": false,
  "side": "Buy",
  "signal_hash": "demo-1",
  "symbol": "BTC",
  "timestamp_ms": 1794379823039
}
"""
PANIC_STDERR = """├  ERROR Transaction failed
Error:
   0: Error: An error occurred during a `FunctionCall` action.
      ExecutionError("Smart contract panicked: refused: the function selector is not one the vault signs")

Location:
   src/common.rs:1864
"""
# The MPC contract's real answer for the BTC agent's first signature.
REAL_SIG = {"big_r": {"affine_point": "02c29fc756a6fd74e44d9b2088f3c2d0f1b06547bb0cc24a017a53e9d579f48f11"},
            "recovery_id": 0,
            "s": {"scalar": "2febc86edfd18b3e6f559c5cb2d8b5dfe7b76e1343df5c6a0b9dec0600cfbba5"},
            "scheme": "Secp256k1"}
REAL_HASH = "a68896622b1624f5176146257aafb650e5a19892011ca3153b01398167ac5624"
BTC_ADDRESS = "0x49A6bdc6b5ef4B80d1e5BF6eE86F933623022096"


class FakeNear:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.replies.pop(0)


def vault(replies):
    fake = FakeNear(replies)
    return vc.Vault("vault.zcastor-desk.testnet", "zcastor-desk.testnet",
                    runner=fake), fake


def test_a_granted_permit_is_parsed_from_the_cli():
    v, fake = vault([(0, GRANTED_STDOUT, "├  logs on stderr")])
    d = v.request_permit("btc", signal_hash="demo-1", side="buy",
                         notional_cents=100000, risk_cents=4000, leverage_x10=30)
    assert d["granted"] is True and d["id"] == 0 and d["reason"] == "within_limits"
    cmd = fake.calls[0]
    assert cmd[:5] == ["near", "contract", "call-function", "as-transaction",
                       "vault.zcastor-desk.testnet"]
    assert cmd[cmd.index("sign-as") + 1] == "btc.zcastor-desk.testnet"
    assert json.loads(cmd[cmd.index("json-args") + 1])["side"] == "Buy"
    assert cmd[cmd.index("attached-deposit") + 1] == "0 NEAR"


def test_a_refused_permit_is_a_decision_not_an_error():
    refused = GRANTED_STDOUT.replace("true", "false").replace("within_limits", "over_trade_risk")
    v, _ = vault([(0, refused, "")])
    d = v.request_permit("btc", signal_hash="x", side="Sell", notional_cents=1,
                         risk_cents=9000, leverage_x10=10)
    assert d["granted"] is False and d["reason"] == "over_trade_risk"


def test_a_contract_panic_becomes_a_vault_error_with_the_contracts_words():
    v, _ = vault([(1, "", PANIC_STDERR)])
    try:
        v.sign_anchor("btc", nonce=0, max_priority_fee_wei=1, max_fee_wei=2,
                      gas_limit=400000, data=bytes.fromhex("a9059cbb"))
        assert False
    except vc.VaultError as exc:
        assert exc.reason == "refused: the function selector is not one the vault signs"


def test_empty_and_scalar_returns():
    assert vc.parse_return("") is None
    assert vc.parse_return("true\n") is True
    assert vc.parse_return('"abc"\n') == "abc"
    assert vc.parse_return("[]") == []


def test_short_agent_names_expand_under_the_owner():
    v, _ = vault([])
    assert v.account("eth") == "eth.zcastor-desk.testnet"
    assert v.account("x.other.testnet") == "x.other.testnet"


def test_the_local_signing_hash_matches_the_one_the_vault_signed_on_testnet():
    h = vc.signing_hash(787223, 0, 10**9, 2 * 10**9, 400000,
                        bytes.fromhex("64d2529500000000"))
    assert h.hex() == REAL_HASH


def test_the_real_mpc_signature_recovers_to_the_btc_agents_address():
    assert vc.recover(bytes.fromhex(REAL_HASH), REAL_SIG) == BTC_ADDRESS


def test_the_assembled_transaction_is_valid_and_from_the_agent():
    from eth_account import Account
    raw = vc.assemble(787223, 0, 10**9, 2 * 10**9, 400000,
                      bytes.fromhex("64d2529500000000"), REAL_SIG)
    assert raw[0] == 2
    assert Account.recover_transaction(raw) == BTC_ADDRESS


def test_dollars_become_cents_without_float_drift():
    assert vc._cents(40) == 4000 and vc._cents(12.345) == 1234 and vc._cents(-0.1) == -10


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
