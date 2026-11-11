"""
verify_record.py tests — the counterparty's tool, so the offline parts are
pinned here: which network it defaults to, which registries it searches, and
how it matches a checksum. Network calls are not exercised.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "verify_record", ROOT / "ops" / "scripts" / "verify_record.py")
vr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vr)

CS = "62bbba4cb967490f" + "0" * 48


def row(checksum, reg=2195, rid=1):
    return ("https://x.test/d.json", checksum, "sha256", "{}", "2026-10-02",
            "Active", rid, 0, True, reg)


def test_defaults_to_the_l1():
    assert vr.default_network({}) == "l1-testnet"
    assert vr.default_network({"NVNM_ENV": "l1-testnet"}) == "l1-testnet"


def test_honours_a_known_nvnm_env_and_ignores_unknown():
    assert vr.default_network({"NVNM_ENV": "testnet"}) == "testnet"
    assert vr.default_network({"NVNM_ENV": "mainnet-someday"}) == "l1-testnet"


def test_l1_registries_are_the_new_ones():
    assert vr.NETWORKS["l1-testnet"] == "https://rpc.nvnm.testnet.nvnmchain.io"
    assert sorted(vr.REGISTRIES_BY_NETWORK["l1-testnet"]) == [2194, 2195, 2196, 2197]
    assert sorted(vr.REGISTRIES_BY_NETWORK["testnet"]) == [4296, 4297, 4298, 4299]


def test_all_searches_both_eras():
    assert set(vr.SEARCH_ALL) == {"l1-testnet", "testnet"}


def test_both_testnets_absent_reverts_mean_absent():
    assert vr.is_absent(Exception("execution reverted: record does not exist"))
    assert vr.is_absent(Exception("rpc error: not found"))
    assert not vr.is_absent(Exception("connection refused"))


def test_matching_ignores_prefix_and_case():
    rows = [row("sha256:" + CS.upper()), row("ff" * 32, rid=2)]
    found = vr.matches(rows, CS)
    assert len(found) == 1 and found[0][0] == 2195 and found[0][1] == 1


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
