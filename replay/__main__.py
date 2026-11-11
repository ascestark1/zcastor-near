"""
Replay captured signals through the gate stack.

    python -m replay check                 # re-decide under the policy each was
                                           # decided under; expect zero differences
    python -m replay diff --policy P.json  # decide each under its own policy AND
                                           # under P; list every verdict that moves
    python -m replay stats                 # what has been captured so far

Options: --origins DIR (default data/origins), --since/--until YYYY-MM-DD,
--symbol BTCUSDT, --show N (differences to print, default 10), --verbose.

"Undecidable offline" means the policy under test needed a venue read the
original run skipped (expensive gates do not run after an early refusal). For
the BTC identity check that count must be zero, like the differences.

Exit codes: 0 no differences, 1 differences found, 2 nothing to replay.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from replay import harness  # noqa: E402


def _args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m replay")
    ap.add_argument("command", choices=("check", "diff", "stats"))
    ap.add_argument("--origins", default=str(ROOT / "data" / "origins"))
    ap.add_argument("--policy", help="policy file to compare against (diff)")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--symbol", default="")
    ap.add_argument("--show", type=int, default=10)
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.CRITICAL,
                        format="%(levelname)s %(name)s: %(message)s")
    root = Path(a.origins)
    records = list(harness.load_records(root, since=a.since, until=a.until,
                                        symbol=a.symbol))
    if not records:
        print(f"nothing captured in {root}. Is \"capture_origins\": true in "
              f"config/runtime.json, and has the engine received signals?")
        return 2

    if a.command == "stats":
        return _stats(records)

    other = None
    if a.command == "diff":
        if not a.policy:
            print("diff needs --policy PATH")
            return 2
        other = json.loads(Path(a.policy).read_text())

    cache: dict = {}
    same = changed = skipped = undecidable = 0
    actions: Counter = Counter()
    shown = 0
    for rec in records:
        base = harness.load_policy_for(root, rec.get("policy_hash", ""), cache)
        if base is None:
            skipped += 1
            continue
        if a.command == "check":
            before, after = rec["outcome"], harness.replay_one(rec, base)
        else:
            before = harness.replay_one(rec, base).outcome
            after = harness.replay_one(rec, other)
        lines = harness.diff(before, after.outcome)
        if after.misses:
            # The policy under test needed a venue read the original run never
            # made (an expensive gate it skipped after an early refusal). The
            # replayed verdict is then a fail-closed artefact, not an answer.
            undecidable += 1
            lines = [f"undecidable offline: needed {m}" for m in after.misses]
        elif not lines:
            same += 1
            continue
        else:
            changed += 1
        actions[(before["action"], after.outcome["action"])] += 1
        if shown < a.show:
            shown += 1
            print(f"\n{rec['signal_id']}  {rec['received_at']}  "
                  f"{rec.get('symbol', '')}  "
                  f"{before['action']} -> {after.outcome['action']}")
            for line in lines[:12]:
                print(f"    {line}")
            if len(lines) > 12:
                print(f"    ... {len(lines) - 12} more")

    total = same + changed + undecidable
    days = sorted({r["received_at"][:10] for r in records})
    print(f"\nreplayed {total} signals, {days[0]} to {days[-1]}"
          + (f" ({skipped} skipped: policy copy missing)" if skipped else ""))
    print(f"identical: {same}   different: {changed}   "
          f"undecidable offline: {undecidable}")
    if actions:
        print("verdict moves: " + ", ".join(
            f"{x}->{y} x{n}" for (x, y), n in actions.most_common()))
    return 1 if changed or undecidable else 0


def _stats(records: list[dict]) -> int:
    by_symbol = Counter(r.get("symbol", "?") for r in records)
    by_action = Counter(r["outcome"]["action"] for r in records)
    by_reason = Counter(r["outcome"]["reason"] for r in records
                        if r["outcome"]["reason"])
    days = sorted({r["received_at"][:10] for r in records})
    print(f"{len(records)} captured signals, {days[0]} to {days[-1]}")
    print("by symbol: " + ", ".join(f"{k} {v}" for k, v in by_symbol.most_common()))
    print("by action: " + ", ".join(f"{k} {v}" for k, v in by_action.most_common()))
    print("top reasons: " + ", ".join(f"{k} {v}"
                                      for k, v in by_reason.most_common(8)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
