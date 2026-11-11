#!/usr/bin/env python3
"""
Run every suite.

    python3 tests/run_all.py

No pytest, no plugins, no config. The suites are plain scripts with no
dependencies beyond the standard library, so they run identically on a laptop,
in CI, and on the trading box — and a green result never depends on which
version of a test runner happened to be installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    suites = sorted(p for p in (ROOT / "tests").rglob("test_*.py"))
    if not suites:
        print("no suites found")
        return 1

    width = max(len(p.stem) for p in suites)
    total = failures = 0
    failed_suites = []

    print()
    for suite in suites:
        result = subprocess.run([sys.executable, str(suite)],
                                capture_output=True, text=True)
        summary = (result.stdout.strip().splitlines() or ["no output"])[-1]

        if result.returncode == 0:
            print(f"  {suite.stem:<{width}}  {summary}")
        else:
            print(f"  {suite.stem:<{width}}  {summary}   <-- FAILED")
            failed_suites.append(suite)
            for line in result.stdout.splitlines():
                if "FAIL" in line:
                    print(f"      {line.strip()}")
            if result.stderr.strip() and "Traceback" in result.stderr:
                print(f"      {result.stderr.strip().splitlines()[-1]}")

        try:
            passed = int(summary.split()[0])
            total += passed
        except (ValueError, IndexError):
            pass
        failures += result.returncode != 0

    print(f"\n  {len(suites)} suites, {total} tests, "
          f"{'all passing' if not failures else f'{failures} suite(s) failing'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
