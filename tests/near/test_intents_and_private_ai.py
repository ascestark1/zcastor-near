"""
NEAR Intents funding quotes and the NEAR AI Cloud risk note, against fakes.
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "near"))

import intents  # noqa: E402
import private_ai  # noqa: E402

AGENT = "0x49A6bdc6b5ef4B80d1e5BF6eE86F933623022096"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


# ── intents ──────────────────────────────────────────────────────────────────

class FakeOneClick:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        if url.endswith("/v0/tokens"):
            return [{"symbol": "USDC", "blockchain": "near", "decimals": 6,
                     "assetId": intents.NEAR_USDC}]
        return {"quote": {"amountIn": "5000000", "amountInFormatted": "5.0",
                          "amountOut": "480000000", "amountOutFormatted": "4.8",
                          "timeEstimate": 20}}


def test_the_funding_request_matches_the_documented_hyperliquid_route():
    api = intents.OneClick(FakeOneClick())
    r = api.funding_request(usdc=5, recipient=AGENT, refund_to="stark1.near", now=NOW)
    assert r["dry"] is True and r["swapType"] == "EXACT_INPUT"
    assert r["originAsset"] == intents.NEAR_USDC and r["amount"] == "5000000"
    assert r["destinationAsset"] == intents.HYPERLIQUID_USDC
    assert r["recipient"] == AGENT and r["recipientType"] == "DESTINATION_CHAIN"
    assert r["depositType"] == "ORIGIN_CHAIN" and r["refundType"] == "ORIGIN_CHAIN"
    assert r["refundTo"] == "stark1.near" and r["deadline"] == "2026-11-11T12:30:00.000Z"


def test_a_quote_is_priced_and_summarised():
    fake = FakeOneClick()
    api = intents.OneClick(fake)
    req = api.funding_request(usdc=5, recipient=AGENT, refund_to="stark1.near", now=NOW)
    out = intents.summarise(req, api.quote(req))
    assert out["send"] == "5.0" and out["receive"] == "4.8" and out["dry_run"] is True
    assert fake.calls[-1][0] == "POST" and fake.calls[-1][1].endswith("/v0/quote")


def test_it_refuses_to_request_a_live_quote():
    api = intents.OneClick(FakeOneClick())
    req = api.funding_request(usdc=5, recipient=AGENT, refund_to="stark1.near",
                              dry=False, now=NOW)
    try:
        api.quote(req)
        assert False
    except intents.IntentsError as exc:
        assert "only prices routes" in str(exc)


def test_bad_inputs_are_refused_before_any_request():
    fake = FakeOneClick()
    api = intents.OneClick(fake)
    for kw in (dict(usdc=0.4, recipient=AGENT, refund_to="a.near"),
               dict(usdc=5, recipient="stark1.near", refund_to="a.near"),
               dict(usdc=5, recipient=AGENT, refund_to="")):
        try:
            api.funding_request(**kw)
            assert False, kw
        except intents.IntentsError:
            pass
    assert fake.calls == []


def test_a_jwt_is_sent_as_a_bearer_token():
    fake = FakeOneClick()
    intents.OneClick(fake, jwt="abc").tokens()
    assert fake.calls[0][3] == {"Authorization": "Bearer abc"}


# ── private AI ───────────────────────────────────────────────────────────────

class FakeEnclave:
    """Signs `<request sha256>:<response sha256>` the way NEAR AI Cloud does."""

    def __init__(self, tamper=False, wrong_attestation=False, two_part=False,
                 other_model=False):
        self.two_part, self.other_model = two_part, other_model
        from eth_account import Account
        self.acct = Account.from_key("0x" + "22" * 32)
        self.tamper, self.wrong_attestation = tamper, wrong_attestation
        self.sent = None
        self.hashes = None

    def __call__(self, method, url, body, headers):
        assert headers["Authorization"] == "Bearer sk-test"
        if url.endswith("/v1/chat/completions"):
            self.sent = json.loads(body)
            raw = json.dumps({"id": "chatcmpl-1", "choices": [{"message": {
                "content": '{"risk":"medium","note":"Counter-trend entry in a spent zone."}'}}]}).encode()
            self.hashes = (hashlib.sha256(body).hexdigest(),
                           hashlib.sha256(raw).hexdigest())
            return raw
        if "/v1/signature/" in url:
            from eth_account.messages import encode_defunct
            req, resp = self.hashes
            text = f"{req}:{'0' * 64 if self.tamper else resp}"
            if not self.two_part:
                model = "some/other-model" if self.other_model else self.sent["model"]
                text = f"{model}:{text}"
            sig = self.acct.sign_message(encode_defunct(text=text)).signature.hex()
            return json.dumps({"text": text, "signature": "0x" + sig.removeprefix("0x"),
                               "signing_address": self.acct.address,
                               "signing_algo": "ecdsa"}).encode()
        if "/v1/attestation/report" in url:
            addr = "0x" + "00" * 20 if self.wrong_attestation else self.acct.address
            return json.dumps({"model_attestations": [
                {"signing_address": addr, "intel_quote": "abcd"}]}).encode()
        raise AssertionError(url)


SIGNAL = {"symbol": "BTC", "direction": "DOWN", "timeframe": "15m", "session": "London",
          "regime": "trending", "confidence": "high", "zone_status": "caution",
          "entry_price": 110250.5, "atr": 331.2, "nearest_resistance_price": 110900,
          "range_low": 109800, "pred_conf": 0.83}


def test_only_the_coarse_description_of_a_signal_leaves_the_machine():
    fake = FakeEnclave()
    private_ai.PrivateAI("sk-test", transport=fake).review(SIGNAL)
    sent = json.dumps(fake.sent)
    for private in ("110250", "331.2", "110900", "109800", "0.83", "entry_price", "atr"):
        assert private not in sent, private
    assert '\\"direction\\": \\"DOWN\\"' in sent and '\\"symbol\\": \\"BTC\\"' in sent


def test_a_genuine_answer_passes_every_check():
    rec = private_ai.PrivateAI("sk-test", transport=FakeEnclave()).review(SIGNAL)
    v = rec["verification"]
    assert rec["answer"] == {"risk": "medium", "note": "Counter-trend entry in a spent zone."}
    assert v["hashes_match"] and v["signer_recovered"] and v["signer_in_attestation"]
    assert v["attestation_has_intel_quote"] and len(v["attestation_sha256"]) == 64
    assert "not checked here" in v["tdx_quote_verified"]


def test_a_signature_over_a_different_response_is_caught():
    rec = private_ai.PrivateAI("sk-test", transport=FakeEnclave(tamper=True)).review(SIGNAL)
    v = rec["verification"]
    assert v["request_hash_matches"] and not v["response_hash_matches"]
    assert not v["hashes_match"]


def test_the_real_signed_text_from_11_nov_verifies():
    # Copied from the first live run: model, request hash, response hash.
    text = ("Qwen/Qwen3.8-27B:75ab25ab724f00155d3d28d4a11b514014f8b56a66f399fa67b5a84f515beb41"
            ":a96ae33f5ae24599406af57703ce7b7c1c5f5f17186e883d81b02e224bbbd27a")
    sig = ("0x808cf7d100700ecd39dec0a4bce46bd8719f7fe716d69f798e1895346353b36d3129ef00aa76a4"
           "1a67e71faa539d706fe0a992a04d7ac934bdd0b19e852c1b321b")
    signer = "0x835ab53d20294ef795906dff21ae4c045336aa26"

    def live(method, url, body, headers):
        if "/v1/signature/" in url:
            return json.dumps({"text": text, "signature": sig,
                               "signing_address": signer, "signing_algo": "ecdsa"}).encode()
        return json.dumps({"model_attestations": [
            {"signing_address": signer, "intel_quote": "ab"}]}).encode()

    v = private_ai.PrivateAI("sk-test", model="Qwen/Qwen3.8-27B", transport=live).verify(
        "e9c9ce7370b844609efec1eb920951b0",
        "75ab25ab724f00155d3d28d4a11b514014f8b56a66f399fa67b5a84f515beb41",
        "a96ae33f5ae24599406af57703ce7b7c1c5f5f17186e883d81b02e224bbbd27a")
    assert v["hashes_match"] and v["signer_recovered"] and v["signer_in_attestation"]
    assert v["signed_model"] == "Qwen/Qwen3.8-27B"


def test_the_older_two_part_signed_text_still_verifies():
    rec = private_ai.PrivateAI("sk-test", transport=FakeEnclave(two_part=True)).review(SIGNAL)
    assert rec["verification"]["hashes_match"]


def test_a_signature_naming_a_different_model_is_caught():
    rec = private_ai.PrivateAI("sk-test", transport=FakeEnclave(other_model=True)).review(SIGNAL)
    v = rec["verification"]
    assert v["request_hash_matches"] and v["response_hash_matches"]
    assert not v["signed_model_matches"] and not v["hashes_match"]


def test_a_signer_missing_from_the_attestation_is_caught():
    rec = private_ai.PrivateAI("sk-test",
                               transport=FakeEnclave(wrong_attestation=True)).review(SIGNAL)
    assert rec["verification"]["signer_recovered"]
    assert not rec["verification"]["signer_in_attestation"]


def test_no_key_no_call():
    import os
    os.environ.pop("NEAR_AI_CLOUD_API_KEY", None)
    try:
        private_ai.PrivateAI("")
        assert False
    except private_ai.PrivateAIError as exc:
        assert "NEAR_AI_CLOUD_API_KEY" in str(exc)


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
