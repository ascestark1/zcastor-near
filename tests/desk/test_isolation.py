"""
Isolation tests. Each one is a way the desk copy could reach live Zcastor.
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_isolation", ROOT / "ops" / "check_isolation.py")
iso = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = iso
spec.loader.exec_module(iso)

KEY_A = "0x" + "a1" * 32
KEY_B = "0x" + "b2" * 32


def world(desk_rt=None, live_env="", live_rt=None, desk_env=None, prefix=None,
          starting=None, busy=()):
    """A home folder with live Zcastor and the desk side by side."""
    home = Path(tempfile.mkdtemp())
    live, desk = home / "zcastor-v2", home / "zcastor-near"
    (live / "config").mkdir(parents=True)
    (desk / "config").mkdir(parents=True)
    (live / ".env").write_text(live_env)
    (live / "config" / "runtime.json").write_text(json.dumps(live_rt or {
        "venue": "mt5", "registry_ids": {"policy": 2194, "decisions": 2195}}))
    rt = {"venue": "hyperliquid-paper", "dossier_root": str(desk / "data" / "dossiers"),
          "dossier_autopush": False, "registry_ids": {}}
    if desk_rt is not None:
        rt.update(desk_rt)
        rt = {k: v for k, v in rt.items() if v is not None}
    (desk / "config" / "runtime.json").write_text(json.dumps(rt))
    results = iso.check(desk, desk_env or {}, live,
                        prefix=prefix or str(desk / ".venv"), starting=starting,
                        port_free=lambda p: p not in busy)
    return results, home


def fails(results):
    return [m for lvl, m in results if lvl == iso.FAIL]


def test_a_clean_desk_passes():
    r, _ = world(desk_env={"NVNM_PRIVATE_KEY": KEY_B},
                 live_env=f"export NVNM_PRIVATE_KEY={KEY_A}\n")
    assert fails(r) == [], fails(r)


def test_the_mt5_default_venue_is_refused():
    r, _ = world(desk_rt={"venue": None})
    assert any("defaults to mt5" in m for m in fails(r))


def test_the_default_dossier_root_is_refused_because_it_is_the_live_repo():
    r, _ = world(desk_rt={"dossier_root": None})
    assert any("zcastor-dossiers" in m for m in fails(r))


def test_pointing_dossiers_at_the_live_repo_is_refused():
    _, home = world()
    desk = home / "zcastor-near"
    rt = json.loads((desk / "config" / "runtime.json").read_text())
    rt["dossier_root"] = str(home / "zcastor-dossiers")
    (desk / "config" / "runtime.json").write_text(json.dumps(rt))
    r = iso.check(desk, {}, home / "zcastor-v2", prefix=str(desk / ".venv"))
    assert any("live Zcastor's dossier repo" in m for m in fails(r))


def test_dossiers_anywhere_outside_the_desk_are_refused():
    elsewhere = Path(tempfile.mkdtemp())
    r, _ = world(desk_rt={"dossier_root": str(elsewhere)})
    assert any("outside this folder" in m for m in fails(r))


def test_autopush_must_be_off():
    r, _ = world(desk_rt={"dossier_autopush": True})
    assert any("autopush" in m for m in fails(r))


def test_reusing_a_live_registry_id_is_refused():
    r, _ = world(desk_rt={"registry_ids": {"decisions": 2195}})
    assert any("2195" in m for m in fails(r))


def test_sharing_the_anchoring_key_is_refused_without_printing_it():
    r, _ = world(desk_env={"NVNM_PRIVATE_KEY": KEY_A},
                 live_env=f'NVNM_PRIVATE_KEY="{KEY_A}"\n')
    msgs = fails(r)
    assert any("nonce" in m for m in msgs)
    assert not any(KEY_A in m for _, m in r)


def test_the_feed_fallback_to_the_anchoring_key_is_caught():
    # Live feed signs with its NVNM key (no ZCASTOR_FEED_KEY); the desk sets
    # that same key as its feed key.
    r, _ = world(desk_env={"ZCASTOR_FEED_KEY": KEY_A, "NVNM_PRIVATE_KEY": KEY_B},
                 live_env=f"export NVNM_PRIVATE_KEY={KEY_A}\n")
    assert any("same key as the live feed" in m for m in fails(r))


def test_sharing_the_discord_webhook_is_refused():
    hook = "https://discord.com/api/webhooks/1/abc"
    r, _ = world(desk_env={"DISCORD_WEBHOOK_URL": hook},
                 live_env=f"export DISCORD_WEBHOOK_URL={hook}\n")
    assert any("live channel" in m for m in fails(r))


def test_binance_is_no_longer_a_desk_venue():
    r, _ = world(desk_rt={"venue": "binance"})
    assert any("hyperliquid-paper" in m for m in fails(r))


def test_mt5_credentials_in_the_desk_environment_are_refused():
    r, _ = world(desk_env={"MT5_LOGIN": "101"})
    assert any("MT5" in m for m in fails(r))


def test_a_live_port_is_refused():
    r, _ = world(desk_env={"DESK_ENGINE_PORT": "8000"})
    assert any("live engine" in m for m in fails(r))


def test_a_busy_desk_port_is_refused_when_starting():
    r, _ = world(starting="engine", busy={8100})
    assert any("already in use" in m for m in fails(r))


def test_running_in_the_live_venv_is_refused():
    home = Path(tempfile.mkdtemp())
    r, h = world()
    r2 = iso.check(h / "zcastor-near", {}, h / "zcastor-v2",
                   prefix=str(h / "zcastor-v2" / ".venv"))
    assert any("live Zcastor's venv" in m for m in fails(r2))


def test_env_parsing_handles_export_quotes_and_comments():
    p = Path(tempfile.mkdtemp()) / ".env"
    p.write_text("# c\nexport A=1\nB='two words'\nC=\"3\"  \nD=4 # note\n\nE\n")
    assert iso.parse_env_file(p) == {"A": "1", "B": "two words", "C": "3", "D": "4"}


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
