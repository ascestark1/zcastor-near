# replay/: proving the ATR switch leaves BTC alone

Built 20 October 2026. Capture is on in the desk (`"capture_origins": true` in
`config/runtime.json`); it is off unless that line says so, so an engine that
never asked for it behaves exactly as before.

## What gets captured

The engine used to keep only the hash of each signal. Now, for every signal,
`zcastor/record/capture.py` appends one line to `data/origins/YYYY-MM-DD.jsonl`
holding:

| Field | What it is |
|---|---|
| `payload` | The signal exactly as received. Its checksum is the origin hash already in the decision log. |
| `state` | Zone memory, signal coherence, entry cooldown and feed staleness just before the gates ran; hourly levels just after (a query may refresh them). |
| `tape` | Every call the gates made to the venue, positions, edge ledger and risk adapter, with arguments and results, in order. Exceptions too. |
| `clock` | The time the gates saw. Replay freezes time there. |
| `outcome` | The pipeline's verdict, reason, binding gate, every gate's result and detail, and every derived price (stop, target, entry), before the engine acted. |

Each policy file is stored once under `data/origins/policies/<hash>.json`.
Capture never changes a decision: if it fails, it logs and the signal is
decided exactly as before (tested).

## Running it

```bash
.venv/bin/python -m replay stats                 # what has been captured
.venv/bin/python -m replay check                 # expect: different: 0
.venv/bin/python -m replay diff --policy P.json  # what P would change
```

`--since/--until YYYY-MM-DD`, `--symbol BTCUSDT`, `--show N`, `--verbose`.
Exit code 0 means no differences, 1 means differences, 2 means nothing captured.

`check` re-decides each signal under the policy it was decided under and
compares every field with what was recorded. It proves the capture is complete.
Anything other than `different: 0` is a capture bug.

## What is re-run and what is played back

Re-run with the policy under test: every gate (built from
`zcastor/gates/assembly.py`, the same function the server uses), the target
optimizer, zone memory and signal coherence. These read point distances, so a
policy change must be able to move them.

Played back from the tape: prices, spread, balance, margin, positions, session
opens, the edge ledger, model coherence and today's PnL. Those are facts about
that moment, not judgements.

## Undecidable offline

Once a cheap gate refuses, the pipeline skips the expensive ones, so their
venue reads never happen and are not on the tape. If a policy under test lifts
that early refusal, the replay needs a read it doesn't have. It reports the
signal as **undecidable offline** and names the missing call. It doesn't guess.
For the BTC identity proof this count must be zero, like the differences.

## The ATR switch proof

1. Run the desk's BTCUSDT tab for a week with capture on.
2. `python -m replay check` → `different: 0`.
3. Make the switch. Then `python -m replay diff --policy <BTC policy read through
   data/profiles/BTCUSDT.json>` → `different: 0`, `undecidable offline: 0`.
   Any difference is a bug in the switch, not a tuning opportunity.

Captures hold the full signal payload, which is the dashboard's private output.
`data/` is gitignored; keep it that way.
