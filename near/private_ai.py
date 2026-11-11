#!/usr/bin/env python3
"""
A private second opinion on a decision, from NEAR AI Cloud.

    .venv/bin/python near/private_ai.py note --last           # latest captured signal
    .venv/bin/python near/private_ai.py note --symbol ETH --direction DOWN --timeframe 15m
    .venv/bin/python near/private_ai.py models

The desk's signals are its alpha, so they can't go to an ordinary hosted model.
NEAR AI Cloud runs the model inside a hardware enclave (TEE) and signs each
answer with a key that lives only in that enclave. This tool sends a compact
description of one decision, gets back a short risk note, and then checks the
evidence rather than taking the privacy on trust:

1. it hashes the exact request bytes it sent and the exact response bytes it got;
2. it fetches the enclave's signature for that chat id and checks the signed
   text is `<model>:<request sha256>:<response sha256>` (the form observed on
   11 Nov 2026; the older two-part form is accepted too);
3. it recovers the signer from the signature and checks it is the address the
   signature claims;
4. it fetches the attestation report and checks that address appears in it.

The result (the note, the hashes, the signature and what each check found) is
saved to data/near/inference/<chat id>.json, ready to go into a decision record.

What it does NOT do: verify the Intel TDX quote and NVIDIA evidence inside the
attestation report. That needs the vendors' verification libraries; NEAR's own
tool for it is github.com/nearai/nearai-cloud-verifier. This tool records the
report's checksum so that verification can be run on exactly the report seen.

The note is advisory. It is recorded, never fed back into the gates, so replay
stays deterministic.

Environment: NEAR_AI_CLOUD_API_KEY (required), NEAR_AI_MODEL (optional).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://cloud-api.near.ai"
DEFAULT_MODEL = "Qwen/Qwen3.8-27B"
# Room for a reasoning model to think before it writes the short answer. At 200
# the model spent the whole budget reasoning and returned an empty reply.
MAX_TOKENS = 2000

# (method, url, body bytes or None, headers) -> raw response bytes
Http = Callable[[str, str, Optional[bytes], dict], bytes]

# Only these fields of a signal leave the machine. No prices, levels or scores.
SHARED_FIELDS = ("symbol", "direction", "timeframe", "session", "regime",
                 "tf_regime", "confidence", "zone_status", "zone_phase")

SYSTEM = ("You are a risk reviewer for a systematic perpetual-futures desk. You are "
          "given one trade decision. Reply with JSON only: "
          '{"risk":"low|medium|high","note":"<at most 40 words>"}. '
          "Judge only what you are given. Do not predict price.")


class PrivateAIError(RuntimeError):
    pass


def http(method: str, url: str, body: Optional[bytes], headers: dict) -> bytes:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")[:300] if exc.fp else ""
        raise PrivateAIError(f"NEAR AI Cloud answered HTTP {exc.code}: {text}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PrivateAIError(f"cannot reach NEAR AI Cloud: {getattr(exc, 'reason', exc)}") from None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def describe(signal: dict) -> dict:
    """The part of a signal that is sent. Everything else stays on the desk."""
    return {k: signal[k] for k in SHARED_FIELDS if signal.get(k) not in (None, "")}


class PrivateAI:
    def __init__(self, api_key: str = "", model: str = "", transport: Http = http,
                 base: str = BASE) -> None:
        self.key = api_key or os.environ.get("NEAR_AI_CLOUD_API_KEY", "")
        if not self.key:
            raise PrivateAIError("set NEAR_AI_CLOUD_API_KEY in .env")
        self.model = model or os.environ.get("NEAR_AI_MODEL", "") or DEFAULT_MODEL
        self._http = transport
        self._base = base.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json", "User-Agent": "zcastor-near"}

    def models(self) -> list[str]:
        raw = self._http("GET", f"{self._base}/v1/models", None, self._headers())
        return sorted(m.get("id", "") for m in json.loads(raw).get("data", []))

    def review(self, signal: dict) -> dict:
        shared = describe(signal)
        body = json.dumps({
            "model": self.model, "temperature": 0, "max_tokens": MAX_TOKENS, "stream": False,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(shared, sort_keys=True)}],
        }, separators=(",", ":")).encode()
        raw = self._http("POST", f"{self._base}/v1/chat/completions", body,
                         self._headers())
        try:
            reply = json.loads(raw)
            chat_id = reply["id"]
            choice = reply["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError) as exc:
            raise PrivateAIError(f"unexpected reply shape: {exc}") from None

        record = {
            "model": self.model, "chat_id": chat_id, "sent": shared,
            "answer": _parse_note(text), "answer_raw": text,
            "finish_reason": choice.get("finish_reason"),
            "request_sha256": sha256(body), "response_sha256": sha256(raw),
        }
        record["verification"] = self.verify(chat_id, record["request_sha256"],
                                             record["response_sha256"])
        return record

    def verify(self, chat_id: str, request_hash: str, response_hash: str) -> dict:
        out: dict = {"signature_fetched": False, "hashes_match": False,
                     "signer_recovered": False, "signer_in_attestation": False,
                     "tdx_quote_verified": "not checked here; see nearai-cloud-verifier"}
        try:
            sig = json.loads(self._http(
                "GET", f"{self._base}/v1/signature/{chat_id}"
                       f"?model={self.model}&signing_algo=ecdsa", None, self._headers()))
        except (PrivateAIError, ValueError) as exc:
            out["error"] = f"signature: {exc}"
            return out
        out["signature_fetched"] = True
        text = str(sig.get("text", ""))
        out.update({"signed_text": text, "signature": sig.get("signature"),
                    "signing_address": sig.get("signing_address"),
                    "signing_algo": sig.get("signing_algo")})
        # `<model>:<request>:<response>`; a model id may itself contain colons,
        # so the two hashes are taken from the right.
        parts = text.rsplit(":", 2)
        signed_req, signed_resp = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", "")
        if len(parts) == 3:
            out["signed_model"] = parts[0]
            out["signed_model_matches"] = parts[0] == self.model
        out["request_hash_matches"] = signed_req == request_hash
        out["response_hash_matches"] = signed_resp == response_hash
        out["hashes_match"] = (out["request_hash_matches"] and out["response_hash_matches"]
                               and out.get("signed_model_matches", True))

        try:
            from eth_account import Account
            from eth_account.messages import encode_defunct
            recovered = Account.recover_message(encode_defunct(text=text),
                                                signature=sig["signature"])
            out["recovered_address"] = recovered
            out["signer_recovered"] = (
                recovered.lower() == str(sig.get("signing_address", "")).lower())
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"recover: {exc}"

        try:
            nonce = secrets.token_hex(32)
            raw = self._http(
                "GET", f"{self._base}/v1/attestation/report?model={self.model}"
                       f"&signing_algo=ecdsa&nonce={nonce}", None, self._headers())
            out["attestation_sha256"] = sha256(raw)
            out["attestation_nonce"] = nonce
            report = json.loads(raw)
            addresses = {str(report.get("signing_address", "")).lower()}
            for m in report.get("model_attestations") or []:
                addresses.add(str(m.get("signing_address", "")).lower())
            for m in report.get("all_attestations") or []:
                addresses.add(str(m.get("signing_address", "")).lower())
            out["signer_in_attestation"] = (
                str(sig.get("signing_address", "")).lower() in addresses - {""})
            out["attestation_has_intel_quote"] = bool(
                report.get("intel_quote") or any(
                    m.get("intel_quote") for m in report.get("model_attestations") or []))
        except (PrivateAIError, ValueError) as exc:
            out["error"] = f"attestation: {exc}"
        return out


def _parse_note(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            got = json.loads(text[start:end + 1])
            if isinstance(got, dict):
                return {"risk": str(got.get("risk", "")).lower(),
                        "note": str(got.get("note", ""))[:400]}
        except ValueError:
            pass
    return {"risk": "", "note": text.strip()[:400]}


def last_captured_signal(root: Path = ROOT / "data" / "origins") -> dict:
    files = sorted(root.glob("*.jsonl"))
    for path in reversed(files):
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = dict(rec.get("payload") or {})
            payload.setdefault("symbol", rec.get("symbol", ""))
            return payload
    raise PrivateAIError(f"no captured signals in {root}")


def save(record: dict, root: Path = ROOT / "data" / "near" / "inference") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in record["chat_id"] if c.isalnum() or c in "-_")[:80]
    path = root / f"{safe}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="near/private_ai.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models")
    n = sub.add_parser("note")
    n.add_argument("--last", action="store_true", help="use the latest captured signal")
    for f in SHARED_FIELDS:
        n.add_argument(f"--{f.replace('_', '-')}", default="")
    a = ap.parse_args(argv)
    try:
        ai = PrivateAI()
        if a.cmd == "models":
            print("\n".join(ai.models()))
            return 0
        signal = last_captured_signal() if a.last else {
            f: getattr(a, f) for f in SHARED_FIELDS if getattr(a, f)}
        if not describe(signal):
            ap.error("nothing to review: give --last or at least --direction")
        record = ai.review(signal)
    except PrivateAIError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    record["saved"] = str(save(record))
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
