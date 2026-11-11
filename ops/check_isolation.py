#!/usr/bin/env python3
"""
Refuse to start anything in this folder that could touch live Zcastor.

The desk is a copy of the live engine. A copy inherits the live engine's
defaults, and three of those defaults reach into live Zcastor:

  - venue defaults to MT5, so a desk engine would connect to the live account;
  - dossier_root defaults to the folder NEXT TO the code, i.e. ~/zcastor-dossiers,
    the live public dossier repo, and autopush defaults to on;
  - the dashboard posted to :8000, the live engine.

And three things must never be shared because sharing them is silent:

  - NVNM_PRIVATE_KEY: two processes signing with one key race for nonces, and
    the live engine's anchors start failing with "nonce too low";
  - ZCASTOR_FEED_KEY: counterparties' followers trust that signature; desk
    events signed with it would pass as live signals;
  - DISCORD_WEBHOOK_URL: desk fills would post in the live channel.

Every start script runs this first. It compares keys by hash and never prints
a value. Exit 1 on any FAIL.

    python ops/check_isolation.py                    # report
    python ops/check_isolation.py --starting engine  # also: is the port free?
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
LIVE_DEFAULT = Path.home() / "zcastor-v2"

LIVE_PORTS = {8000: "the live engine", 8090: "the live signal feed",
              8080: "the live dashboard", 18812: "the MT5 bridge"}
DESK_PORTS = {"engine": ("DESK_ENGINE_PORT", 8100),
              "feed": ("DESK_FEED_PORT", 8190),
              "dashboard": ("DESK_DASHBOARD_PORT", 8180),
              "hub": ("DESK_HUB_PORT", 8170)}
# Paper until Hyperliquid order signing lands; then "hyperliquid".
DESK_VENUES = ("hyperliquid-paper", "hyperliquid")

MUST_DIFFER = {
    "NVNM_PRIVATE_KEY": "two signers on one key race for nonces and live anchors fail",
    "ZCASTOR_FEED_KEY": "followers would accept desk events as live signals",
    "DISCORD_WEBHOOK_URL": "desk fills would post in the live channel",
}
SHOULD_DIFFER: dict[str, str] = {}
FORBIDDEN = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_BRIDGE_HOST",
             "MT5_BRIDGE_PORT")

OK, WARN, FAIL = "ok", "warn", "FAIL"


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, with or without `export`, quotes stripped."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        value = value.strip()
        if " #" in value and not value.startswith(("'", '"')):
            value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def check(root: Path, env: Mapping[str, str], live: Path, *,
          starting: Optional[str] = None, prefix: str = sys.prefix,
          port_free: Callable[[int], bool] = _port_free) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(level, msg):
        out.append((level, msg))

    # ── the folder itself ────────────────────────────────────────────────────
    if root.resolve() == live.resolve() or _inside(root, live) or _inside(live, root):
        add(FAIL, f"this folder ({root}) and live Zcastor ({live}) overlap")
    else:
        add(OK, f"separate from live Zcastor ({live})")

    if _inside(Path(prefix), live):
        add(FAIL, f"running in live Zcastor's venv ({prefix}); use {root}/.venv")
    elif _inside(Path(prefix), root):
        add(OK, "running in this folder's own .venv")
    else:
        add(WARN, f"not in this folder's .venv ({prefix})")

    # ── runtime.json ─────────────────────────────────────────────────────────
    rt_path = root / "config" / "runtime.json"
    live_rt_path = live / "config" / "runtime.json"
    live_rt = json.loads(live_rt_path.read_text()) if live_rt_path.exists() else {}
    if not rt_path.exists():
        add(FAIL, "config/runtime.json missing; run ./setup.sh")
    else:
        rt = json.loads(rt_path.read_text())
        venue = str(rt.get("venue", "")).lower()
        if venue not in DESK_VENUES:
            add(FAIL, f"venue is {venue or 'unset (defaults to mt5)'}; the desk "
                      f"runs on {' or '.join(DESK_VENUES)} only and must never "
                      "reach the MT5 account")
        else:
            add(OK, f"venue is {venue}")

        droot = Path(str(rt.get("dossier_root", "")) or "<unset>")
        if not str(rt.get("dossier_root", "")):
            add(FAIL, "dossier_root unset; the engine would default to "
                      f"{root.parent / 'zcastor-dossiers'} (the live dossier repo)")
        else:
            droot = droot if droot.is_absolute() else root / droot
            live_droot = Path(live_rt.get("dossier_root", live.parent / "zcastor-dossiers"))
            if _inside(droot, live_droot) or _inside(droot, live):
                add(FAIL, f"dossier_root {droot} is live Zcastor's dossier repo")
            elif not _inside(droot, root):
                add(FAIL, f"dossier_root {droot} is outside this folder")
            else:
                add(OK, "dossiers stay inside this folder")
        if rt.get("dossier_autopush", True):
            add(FAIL, "dossier_autopush must be false (it defaults to true)")

        ids = {v for v in (rt.get("registry_ids") or {}).values() if v}
        live_ids = {v for v in (live_rt.get("registry_ids") or {}).values() if v}
        for extra in ("registry_ids_787111",):
            live_ids |= {v for v in (live_rt.get(extra) or {}).values() if v}
        if ids & live_ids:
            add(FAIL, f"registry ids {sorted(ids & live_ids)} belong to live Zcastor")
        elif ids:
            add(OK, f"own registries {sorted(ids)}")
        else:
            add(OK, "no registries yet (records queue locally)")

    # ── environment ──────────────────────────────────────────────────────────
    set_forbidden = [k for k in FORBIDDEN if env.get(k)]
    if set_forbidden:
        add(FAIL, f"{', '.join(set_forbidden)} set; the desk never uses MT5")
    else:
        add(OK, "no MT5 credentials in this environment")

    for name, (var, default) in DESK_PORTS.items():
        try:
            port = int(env.get(var) or default)
        except ValueError:
            add(FAIL, f"{var} is not a number"); continue
        if port in LIVE_PORTS:
            add(FAIL, f"{var}={port} is {LIVE_PORTS[port]}")
        elif starting == name and not port_free(port):
            add(FAIL, f"port {port} is already in use (another desk {name}?)")
        elif starting == name:
            add(OK, f"{name} port {port} is free")

    live_env = parse_env_file(live / ".env")
    shared = 0
    for var, why in {**MUST_DIFFER, **SHOULD_DIFFER}.items():
        mine, theirs = env.get(var, ""), live_env.get(var, "")
        if mine and theirs and _h(mine) == _h(theirs):
            shared += 1
            add(FAIL if var in MUST_DIFFER else WARN,
                f"{var} is the same as live Zcastor's: {why}")
    # Each feed falls back to NVNM_PRIVATE_KEY when ZCASTOR_FEED_KEY is empty,
    # so compare the keys each side will actually sign with.
    my_feed = env.get("ZCASTOR_FEED_KEY") or env.get("NVNM_PRIVATE_KEY", "")
    live_feed = live_env.get("ZCASTOR_FEED_KEY") or live_env.get("NVNM_PRIVATE_KEY", "")
    if my_feed and live_feed and _h(my_feed) == _h(live_feed) and \
       not (env.get("ZCASTOR_FEED_KEY") and env.get("ZCASTOR_FEED_KEY") == live_env.get("ZCASTOR_FEED_KEY")):
        shared += 1
        add(FAIL, "the desk feed would sign with the same key as the live feed")
    if not shared:
        add(OK, "no key shared with live Zcastor")

    if str(env.get("ANCHOR_BACKEND", "null")).lower() == "nvnm" and not env.get("NVNM_PRIVATE_KEY"):
        add(FAIL, "ANCHOR_BACKEND=nvnm but NVNM_PRIVATE_KEY is empty")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--starting", choices=sorted(DESK_PORTS))
    ap.add_argument("--live", default=os.environ.get("ZCASTOR_LIVE_DIR", str(LIVE_DEFAULT)))
    args = ap.parse_args(argv)

    env = {**parse_env_file(ROOT / ".env"), **os.environ}
    results = check(ROOT, env, Path(args.live).expanduser(), starting=args.starting)
    print("\n  isolation from live Zcastor\n")
    for level, msg in results:
        print(f"  {level:>4}  {msg}")
    failed = sum(1 for lvl, _ in results if lvl == FAIL)
    print(f"\n  {'REFUSING TO START: ' + str(failed) + ' problem(s)' if failed else 'isolated'}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
