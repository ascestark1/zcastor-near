# near/: the money, limits and key layer

Built in the hackathon window. Everything here runs on NEAR **testnet**.

## vault/ — the contract (built 11 Nov)

`vault/src/lib.rs`, 28 unit tests (`cd near/vault && cargo test`). Three jobs:

1. **Permits.** Before any fill an agent calls `request_permit` with the trade's
   notional, risk and leverage. The vault answers granted or refused with the
   reason (`over_trade_risk`, `over_agent_budget`, `over_desk_budget`,
   `over_leverage`, `over_agent_notional`, `agent_disabled`, `desk_halted`).
   Both outcomes are stored and logged as events.
2. **Dead-man halt.** A reporter posts equity; 75% under the day's start halts
   the desk. Only the owner resumes. The reporter can only make it stricter.
3. **Sandboxed signer.** `sign_anchor` builds an NVNM anchoring transaction
   itself and asks NEAR Chain Signatures (`v1.signer-prod.testnet`) to sign it
   with a key derived from the vault's account and the agent's id. It refuses
   anything but a zero-value call to `0x…0A00` with an allowed selector
   (`addRecord`, `updateRecordStatus`) on chain 787223 under the gas caps.
   `anchor_tx_hash` is a view, so anyone can check what it will and won't sign.
   The transaction hash it computes is tested against eth-account's.

## Deploy

```bash
near/deploy_testnet.sh <your-account>.testnet
```
Builds, creates `vault.`, `reporter.`, `btc.` and `eth.` sub-accounts, deploys,
registers the agents, and prints the state plus three commands to try.

## Live on testnet (11 Nov)

- Vault: `vault.zcastor-desk.testnet`, owner `zcastor-desk.testnet`.
- Agents: `btc.` and `eth.zcastor-desk.testnet`. The BTC agent's NVNM address,
  derived by Chain Signatures, is `0x49A6bdc6b5ef4B80d1e5BF6eE86F933623022096`.
- NVNM L1 testnet (chain 787223), both sent from that address with no private
  key anywhere: registry `2675` (tx `5416c4f5…93f5`) and record `1`, the
  checksum of the deployed vault wasm (tx `b50f5c37…bdcf`).

## The engine asks first

With `"vault": {"enabled": true}` in `config/runtime.json`, every order goes
through `zcastor/execution/vaulted.py`: permit first, then the fill, then a
settlement with the realised PnL when the trade closes. A refusal is recorded
as `vault_refused:<reason>`; an unreachable vault means no order. The engine
also posts equity to the vault every five minutes, which is what arms the
dead-man halt. `near/vault_client.py` is the same client as a command line.

## Also here

- `restrict_agent_keys.sh`: swaps an agent's full-access key for a function-call
  key limited to the three vault methods. Done on testnet for all three agents; see
  `evidence/agent_keys.json`.
- `allocator.py`: agents bid for the desk's one risk budget; the vault decides.
  `demo --offline` runs it with no keys.
- `intents.py`: NEAR Intents 1-Click dry-run quote for funding the Hyperliquid balance.
- `private_ai.py`: NEAR AI Cloud risk note, with the enclave's signature checked.
- `judge.py`: re-checks the recorded evidence with no keys (`--live` also reads testnet).
