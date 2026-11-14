# Zcastor desk

**AI trading agents that cannot exceed their limits and do not hold a key.**
Built for NEAR HACKATHON (1–14 November 2026) by Christopher Stark.

## Why I built this

I run a quant desk. Its execution agent, Zcastor, decides in milliseconds
whether to trade, wait or refuse, and since August it has left a public record
of every one of those decisions.

The record answers "what did the agent do?" It does not answer the question I
get asked more often: **"what stops it?"** Until now the honest answer was "its
own code". The agent held the signing key and enforced its own risk limits. If
the code had a bug, or someone tricked it, nothing stood behind it.

For this hackathon I moved the two things an agent should never control, its
limits and its keys, onto NEAR.

## What it does, in plain terms

| The question an owner asks | The answer here |
|---|---|
| Can the agent trade more than I allow? | No. Every trade needs a permit from a contract that holds my limits. No permit, no order. |
| Can the agent change those limits? | No. Only the owner's account can. |
| What if it starts losing badly? | The contract halts the whole desk when equity falls past a line I set. Only I can restart it. |
| Can a hacked agent move money? | No. The agent has no signing key for other chains: NEAR's signing network signs for it, and my contract only allows one kind of transaction, writing a record. On NEAR itself, the agent's key can make three calls to the vault and nothing else. |
| How do I know any of this is true? | Every permit and every refusal is on chain with its reason. You can check it without asking me. |
| Do I have to reveal my strategy? | No. The strategy stays private. The limits and the track record are public. |

That last row is the point. A client, a partner or a regulator can use one of
these agents and verify it stays inside its limits, without seeing how it thinks.

## What was proven, on a live network

All on 11 November 2026, on NEAR testnet. Transaction ids are in `near/evidence/`.

- **A trade inside the limits was granted; one over them was refused.** A $40-risk
  BTC trade got a permit. A $90-risk one was refused as `over_trade_risk`. Both
  decisions are stored on chain.
- **The desk stopped itself.** Equity reported at $2,400 against a $10,000 start
  halted the desk. The next permit was refused as `desk_halted`. The owner
  resumed it.
- **An agent with no key signed a real transaction on another chain.** NEAR Chain
  Signatures signed for the BTC agent, which gave it the address
  `0x49A6bdc6b5ef4B80d1e5BF6eE86F933623022096`. From that address, registry
  `2675` (tx `0x5416c4f5…93f5`) and record `1` (tx `0xb50f5c37…bdcf`) were
  written on NVNM L1 testnet (chain 787223). The record is the checksum of the
  deployed vault.
- **The same agent was refused when it asked for a token transfer.** The contract
  stopped the request before it reached the signer.
- **Funding was priced through NEAR Intents.** 5 USDC on NEAR becomes 4.68 USDC
  in the agent's Hyperliquid balance in about 30 seconds. This was a dry-run
  quote; no funds moved.
- **The agents' own NEAR keys were cut down to three calls.** Each agent
  account's full-access key was deleted. What is left can only ask the vault for
  a permit, settle one, or request an anchor signature. An attempt to send
  0.01 NEAR from each agent failed: "Transaction requires a full permission
  access key." (`near/evidence/agent_keys.json`)
- **A private second opinion was checked, not trusted.** NEAR AI Cloud returned a
  risk note from inside a secure enclave. The enclave's signature matched the
  exact request and response.

You can re-check all of it in under a minute with no account and no key: see
[Try it](#try-it). It should end with `14 of 14 checks passed`.

## How NEAR is used

| NEAR feature | What it does in this project | Code | Evidence |
|---|---|---|---|
| Smart contract | Trade permits, per-agent and desk risk budgets, dead-man halt, decision history, agent win/loss record | `near/vault/src/lib.rs` | `near/evidence/deployment.json` |
| Sub-accounts and function-call keys | One account per agent (`btc.zcastor-desk.testnet`), plus a reporter, under the owner. Each agent's only key can call three vault methods | `near/deploy_testnet.sh`, `near/restrict_agent_keys.sh` | `agent_keys.json` |
| Chain Signatures | Signs NVNM anchoring transactions with a key derived from the vault's account and the agent's id; the vault allowlists what can be signed | `sign_anchor` in the contract, `near/vault_client.py` | `chain_signature.json`, `nvnm_anchors.json` |
| NEAR Intents (1-Click) | Prices the funding route: USDC on NEAR into an agent's Hyperliquid perps balance | `near/intents.py` | `intents_quote.json` |
| NEAR AI Cloud (TEE) | A private risk note on a decision; the enclave's signature over the exact request and response is checked | `near/private_ai.py` | `private_inference.json` |

The trading engine existed before the hackathon. Everything about NEAR was built
in the window. See [What is new](#what-is-new-and-what-is-baseline).

## Try it

Needs Python 3.11+ and, for step 3, Rust. Steps 1–4 use no keys and no accounts.

**1. Install**
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
See: packages install without errors.

**2. Judge mode, offline**
```bash
.venv/bin/python near/judge.py
```
See: eleven lines starting `PASS`, ending `11 of 11 checks passed`. It checks
that the vault's transaction hash is the one Ethereum would sign, that the
recorded Chain Signatures signature recovers to the agent's address, that the
recorded NEAR AI Cloud signature covers the exact request and response, that a
real engine fills a trade the vault's rules allow and records a refusal for one
they don't, and that when three agents bid for one budget the best bids are
funded and the rest refused.

**3. The contract's tests**
```bash
cd near/vault && cargo test && cd ../..
```
See: `test result: ok. 28 passed`. Read the test names: they are the contract's
rules (`an_agent_cannot_raise_its_own_limits`, `it_will_not_sign_a_token_transfer`,
`the_reporter_cannot_resume`, ...).

**4. The live vault, read-only**
```bash
.venv/bin/python near/judge.py --live
```
See: `14 of 14 checks passed`. The five extra checks read
`vault.zcastor-desk.testnet` through a public NEAR RPC: its owner, its limits,
decision 0 (granted) and decision 1 (refused, `over_trade_risk`), the
signer's two-entry allowlist, and that neither agent account holds a
full-access key. Or look yourself:
<https://testnet.nearblocks.io/address/vault.zcastor-desk.testnet>

**5. The Python tests**
```bash
.venv/bin/python tests/run_all.py
```
See: `35 suites, 613 tests, all passing`.

**6. Deploy your own vault** (needs the [`near` CLI](https://github.com/near/near-cli-rs),
[`cargo-near`](https://github.com/near/cargo-near) and a free testnet account)
```bash
near/deploy_testnet.sh <your-account>.testnet
```
See: six numbered steps, then the vault's state and three commands to try. The
first returns `"granted": true`; the second `"granted": false, "reason":
"over_trade_risk"`.

**7. Ask it to sign something it shouldn't** (with your own deployment)
```bash
near contract call-function as-transaction vault.<you>.testnet sign_anchor json-args '{"nonce":0,"max_priority_fee_wei":"1","max_fee_wei":"2","gas_limit":60000,"data_hex":"0xa9059cbb0000000000000000000000000000000000000000000000000000000000000001"}' prepaid-gas '300.0 Tgas' attached-deposit '0 NEAR' sign-as btc.<you>.testnet network-config testnet sign-with-legacy-keychain send
```
See: `Smart contract panicked: refused: the function selector is not one the
vault signs`. `0xa9059cbb` is an ERC-20 transfer.

## How the pieces fit

```
signal ──> gate stack ──> allowed? ──> vault.request_permit ──> granted? ──> fill (paper, Hyperliquid prices)
              │                              │ refused                          │
              │ refused                      ▼                                  ▼ on close
              ▼                    recorded: vault_refused:<reason>      vault.settle(pnl)
        recorded with reason
                                   every 5 min: vault.report_equity ──> dead-man halt
decision records ──> vault.sign_anchor ──> Chain Signatures ──> NVNM (anchored)
```

- `zcastor/execution/vaulted.py` puts the vault between the engine and every
  order. If the vault can't be reached, no order is placed.
- `near/allocator.py` runs a round when several agents want capital at once
  (see [Agents competing for one budget](#agents-competing-for-one-budget)).
- `zcastor/market/hyperliquid.py`, `hub.py`, `hl_paper.py`: Hyperliquid public
  market data and a paper venue (real order book, simulated fills).
- `zcastor/record/capture.py` and `replay/`: every decision's inputs are saved
  and can be re-decided offline.

## What is private, and how to use the agents anyway

The desk's edge is private and is not in this repository:

- **The signal generator** (the dashboard that produces signals) is not here.
  The engine accepts any signal posted to `POST /signal`.
- **The tuned policy** is replaced by `config/policy.json`, a demo policy whose
  tuned numbers are round placeholders. Do not trade on it.
- **25 tests** that pin the private policy's numbers are left out. The 613 here
  pass against the demo policy.

What is public is what a counterparty needs to trust an agent without seeing
how it thinks: its limits, every permit and refusal, its win/loss record and
realised PnL (`get_agent` on the vault), and the proof that its key can only
anchor records. That is the point of the design: you can use an agent, and
verify it stays inside its limits, without access to its alpha.

## What this does not do yet

- **Fills are on paper.** Prices are Hyperliquid's live order book; orders are
  simulated. Placing real Hyperliquid orders with a vault-approved trade-only
  key is designed but not built.
- **Intents is dry-run only.** No funds have been moved through it. Intents has
  no testnet, and the quote's recipient is a testnet-derived address.
- **The TDX quote is not verified here.** `near/private_ai.py` checks the
  enclave's signature and that its signer is in the attestation report; it does
  not verify the Intel quote inside that report. Use
  [nearai-cloud-verifier](https://github.com/nearai/nearai-cloud-verifier).
- **A permit costs a NEAR transaction**, a few seconds before each fill.
- **The engine trades one market (BTC) per process.** Signals for other markets
  are refused until per-market state is added. The allocator's rounds are run
  from a bids file or its demo, not yet from three live engines.
- **A bid's score is the agent's own claim.** The allocator weights it by the
  agent's on-chain record but can't check it.
- **Testnet only.** Nothing here has run on NEAR mainnet.

## What is new and what is baseline

Zcastor is an existing product: a gate-based execution engine that records why
it did and did not trade and anchors those records on NVNM Chain. It ran on MT5
before this hackathon. Per the rules (Q-002 to Q-004), only work in the window
(from 19 October 2026) is offered for judging.

- **Baseline (before the window):** the gate stack, target optimizer, entry
  watcher, decision log and dossiers, NVNM anchoring client, edge ledger, signed
  feed, and their tests. Tag: `pre-hackathon`.
- **Built in the window:** everything under `near/`; `zcastor/execution/vaulted.py`;
  `zcastor/market/hyperliquid.py`, `hub.py`, `hl_paper.py`; `zcastor/record/capture.py`,
  `zcastor/gates/assembly.py` and `replay/`; `universe/`; `ops/check_isolation.py`;
  and their tests. Tag: `hackathon-submission`.

`docs/PROVENANCE.md` lists this file by file. Libraries: near-sdk, FastAPI,
web3.py, eth-account. AI coding assistance (Claude) was used throughout.

## Tracks

AI Security & Defense (`security`), Agent Economy (`agents`), Unstoppable DeFi
(`defi`), Privacy & Private AI (`privacy`), AI Swarms (`swarms`), Autonomous
Business (`business`).

## License

MIT. See `LICENSE`.
