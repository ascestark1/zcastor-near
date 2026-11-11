"""
Kline history — one CSV per contract per UTC day.

Daily files because they make the download resumable and the result checkable:
a day is either complete (all its bars) or it is fetched again, and
nothing is ever appended to a file that already passed. Today is never written,
since it is not over.

    data/history/ETH/5m/2026-10-19.csv

Columns: open_time (UTC, ISO), open, high, low, close, volume, quote_volume,
trades. Prices are written exactly as the venue sent them — strings, not floats —
so a file hashes the same however many times it is rebuilt.

Hyperliquid keeps only its most recent ~5,000 candles per interval, so the desk
uses 5-minute bars (about 17 days reachable) rather than 1-minute bars (3½).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
                    "1h": 60, "2h": 120, "4h": 240}
COLUMNS = ["open_time", "open", "high", "low", "close", "volume",
           "quote_volume", "trades"]


def bars_per_day(interval: str) -> int:
    return 1440 // INTERVAL_MINUTES[interval]


def day_path(root: Path, symbol: str, interval: str, day: datetime) -> Path:
    return root / symbol / interval / f"{day:%Y-%m-%d}.csv"


def _complete(path: Path, expected: int) -> bool:
    if not path.exists():
        return False
    with path.open() as f:
        return sum(1 for _ in f) - 1 == expected


def fetch_day(client, symbol: str, interval: str, day: datetime) -> list[list[str]]:
    """All bars of one UTC day, in order, de-duplicated."""
    start = int(day.timestamp() * 1000)
    end = start + 86_400_000 - 1
    step = INTERVAL_MINUTES[interval] * 60_000
    rows: dict[int, list[str]] = {}
    cursor = start
    while cursor <= end:
        batch = client.klines(symbol, interval, cursor, end, limit=1000)
        if not batch:
            break
        for k in batch:
            t = int(k[0])
            if start <= t <= end:
                rows[t] = [_iso(t), k[1], k[2], k[3], k[4], k[5], k[7], str(k[8])]
        last = int(batch[-1][0])
        if last < cursor:
            break
        cursor = last + step
    return [rows[t] for t in sorted(rows)]


def write_day(path: Path, rows: Iterable[list[str]]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(buf.getvalue())
    tmp.replace(path)


def download(client, symbols: list[str], *, root: Path, interval: str = "1m",
             days: int = 30, now: Optional[datetime] = None,
             log=print) -> dict[str, dict[str, int]]:
    """
    Fetch the last `days` complete UTC days for each symbol. Returns per-symbol
    counts: fetched, skipped (already complete), short (the venue had fewer bars,
    e.g. a listing day or a halt — kept, and reported, not hidden).
    """
    now = now or datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    expected = bars_per_day(interval)
    report: dict[str, dict[str, int]] = {}
    for sym in symbols:
        r = {"fetched": 0, "skipped": 0, "short": 0}
        for back in range(days, 0, -1):
            day = today - timedelta(days=back)
            path = day_path(root, sym, interval, day)
            if _complete(path, expected):
                r["skipped"] += 1
                continue
            rows = fetch_day(client, sym, interval, day)
            if not rows:
                continue
            write_day(path, rows)
            r["fetched"] += 1
            if len(rows) < expected:
                r["short"] += 1
        report[sym] = r
        log(f"  {sym:<14} fetched {r['fetched']:>3}  skipped {r['skipped']:>3}"
            f"  short {r['short']:>2}   (weight used {client.used_weight})")
    return report


def load(root: Path, symbol: str, interval: str = "1m",
         days: Optional[int] = None) -> list[dict]:
    """Bars as dicts with float OHLC and epoch-second `t`, oldest first."""
    folder = root / symbol / interval
    if not folder.exists():
        return []
    files = sorted(folder.glob("*.csv"))
    if days:
        files = files[-days:]
    out = []
    for f in files:
        with f.open() as fh:
            for row in csv.DictReader(fh):
                out.append({
                    "t": datetime.strptime(row["open_time"], "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc).timestamp(),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]),
                })
    return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
