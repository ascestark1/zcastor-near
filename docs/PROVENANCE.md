# Provenance: what existed before, what was built in the window

Zcastor is an existing product. NEAR HACKATHON allows that (HACKATHON.md Q-002
to Q-004) as long as the work is separated, and counts code from the
announcement on 19 October 2026 as inside the window (Q-001). This file says
which is which. The git tags `pre-hackathon` and `hackathon-submission` mark
the same split, and the diff between them is the submission.

## Baseline: Zcastor, before 19 October 2026

Copied from the private Zcastor v2 repository. Not offered for judging.

- `zcastor/gates/` (except `assembly.py`), `zcastor/targets/`, `zcastor/risk/`,
  `zcastor/entry/`: the gate stack, target optimizer, zone memory, entry watcher
- `zcastor/record/` (except `capture.py`): DGML dossiers, canonical hashing,
  decision logs, publishing
- `zcastor/anchor/`: the NVNM anchoring client, outbox and index
- `zcastor/execution/` (except `vaulted.py`), `zcastor/data/`, `zcastor/resolve/`,
  `zcastor/feed/`, `zcastor/notify/`, `zcastor/app/`
- `zcastor/market/mt5.py`, `levels.py`, `session.py`, `snapshot.py`
- `ops/scripts/`: read-only NVNM checks
- `tests/` for the above

Baseline files changed in the window:

- `zcastor/app/server.py`: Hyperliquid paper venue, vault wiring, equity
  reports to the vault, origin capture, refusal of signals for other markets
- `zcastor/execution/engine.py`: records a vault refusal as a decision; hands
  the gates recording wrappers when capture is on
- `config/policy.json`: replaced by a demo policy (see below)

## Built in the window (19 October – 14 November 2026)

- **`near/`** (11 November): the vault contract and its 28 tests, the testnet
  deploy script, the desk-side client, NEAR Intents quotes, the NEAR AI Cloud
  note, judge mode, and the recorded evidence
- **`zcastor/execution/vaulted.py`** (11 November): every order needs a permit
- **`zcastor/market/hyperliquid.py`, `hub.py`, `hl_paper.py`** (24 October):
  Hyperliquid market data, a data hub, and a paper venue
- **`zcastor/record/capture.py`, `zcastor/gates/assembly.py`, `replay/`**
  (20 October): origin capture and offline replay
- **`universe/`** (20–24 October): perp selection, bar history, volatility profiles
- **`ops/check_isolation.py`, `ops/run_hub.sh`, `ops/run_engine.sh`, `setup.sh`**,
  `config/universe.json`, `config/desk.json`, `config/runtime.example.json`
- Tests: `tests/near/`, `tests/replay/`, `tests/universe/`, `tests/desk/`,
  `tests/execution/test_vaulted.py`, `tests/market/test_hyperliquid.py`,
  `tests/market/test_hl_paper.py`

## Left out of this repository

- **The signal generator.** The dashboard that produces the desk's signals is
  private. The engine takes signals over `POST /signal` from any source.
- **The tuned policy.** `config/policy.json` here is a demo: tuned numbers are
  round placeholders. The engine and gates run on it; the desk does not.
- **25 tests** that pin the private policy's numbers. The rest pass on the demo.
- **The counterparty follower**, whose order executor is still exchange-specific.

## Tools and libraries

near-sdk (Rust), cargo-near, near-cli-rs, FastAPI, web3.py, eth-account.
AI coding assistance (Claude) was used throughout the window.
