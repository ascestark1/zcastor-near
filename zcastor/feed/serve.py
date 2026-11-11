"""
The follower feed server — the only Zcastor process meant to face the internet.

It holds no keys. Every event in the journal was signed by the engine when it
was written; this process reads the file and hands events to followers. If it,
the tunnel in front of it, or anything between here and a follower were taken
over, the worst it could do is withhold or delay events. It cannot forge or
alter one, because each follower checks every signature against the operator
address it pinned at setup.

Run next to the engine and expose it with a tunnel — the engine itself stays
on 127.0.0.1 and is never reachable from outside:

    python3 -m zcastor.feed.serve                       # 127.0.0.1:8090
    cloudflared tunnel --url http://127.0.0.1:8090      # quick public URL

Followers each get their own token, so one can be cut off without the others:

    python3 -m zcastor.feed.serve --add-follower alice  # prints alice's token
    python3 -m zcastor.feed.serve --revoke alice
    python3 -m zcastor.feed.serve --list

Only token hashes are stored, so the tokens file is not itself a credential.

API (Authorization: Bearer <token>):

    GET /v1/info                        signer, venue, symbol, latest seq
    GET /v1/events?after=N&wait=25      events with seq > N; waits up to
                                        `wait` seconds for one to arrive
    GET /health                         liveness, no token needed

Long-poll rather than websockets or SSE: it works through any proxy or tunnel,
a follower that drops off resumes from its last seq with nothing lost, and
the whole server is the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("zcastor.feed.serve")

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
MAX_WAIT = 25.0          # under every tunnel's idle timeout (Cloudflare: 100 s)
MAX_BATCH = 500
POLL = 0.5


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── tokens ────────────────────────────────────────────────────────────────────

class Tokens:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.error("tokens file unreadable — no follower can connect")
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def add(self, name: str) -> str:
        data = self._read()
        token = "zcf_" + secrets.token_urlsafe(32)
        data[name] = {"sha256": _hash(token), "created": _now()}
        self._write(data)
        return token

    def revoke(self, name: str) -> bool:
        data = self._read()
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    def names(self) -> list[str]:
        return sorted(self._read())

    def who(self, token: str) -> Optional[str]:
        """The follower a token belongs to, or None. Re-read each call, so a
        revoke takes effect without a restart."""
        if not token:
            return None
        digest = _hash(token)
        for name, entry in self._read().items():
            if hmac.compare_digest(digest, str(entry.get("sha256", ""))):
                return name
        return None


# ── journal reader ────────────────────────────────────────────────────────────

class JournalReader:
    """Reads the engine's journal, re-parsing only when the file changes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] = (-1, -1)
        self._events: list[dict[str, Any]] = []

    def events(self) -> list[dict[str, Any]]:
        try:
            st = self.path.stat()
            stamp = (st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            return []
        with self._lock:
            if stamp != self._stamp:
                parsed = []
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        env = json.loads(line)
                        int(env["seq"])
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                        continue        # a torn last line; the next read has it
                    parsed.append(env)
                self._events, self._stamp = parsed, stamp
            return self._events

    def after(self, seq: int) -> list[dict[str, Any]]:
        return [e for e in self.events() if int(e["seq"]) > seq][:MAX_BATCH]

    def latest(self) -> int:
        events = self.events()
        return int(events[-1]["seq"]) if events else 0

    def updated(self) -> str:
        try:
            ts = self.path.stat().st_mtime
        except FileNotFoundError:
            return ""
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── HTTP ──────────────────────────────────────────────────────────────────────

def make_handler(reader: JournalReader, tokens: Tokens):
    class Handler(BaseHTTPRequestHandler):
        server_version = "zcastor-feed/1"

        def log_message(self, fmt, *args):            # quiet the default
            pass

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, {"ok": True, "time": _now()})

            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            follower = tokens.who(token)
            if follower is None:
                return self._send(401, {"error": "unknown or missing token"})

            if url.path == "/v1/info":
                events = reader.events()
                signer = events[-1].get("signer", "") if events else ""
                venue = symbol = ""
                if events:
                    try:
                        p = json.loads(events[-1]["payload"])
                        venue, symbol = p.get("venue", ""), p.get("symbol", "")
                    except (KeyError, json.JSONDecodeError):
                        pass
                return self._send(200, {
                    "signer": signer, "venue": venue, "symbol": symbol,
                    "latest_seq": reader.latest(),
                    "journal_updated": reader.updated(),
                    "server_time": _now(), "follower": follower})

            if url.path == "/v1/events":
                q = parse_qs(url.query)
                try:
                    after = int(q.get("after", ["0"])[0])
                    wait = min(max(float(q.get("wait", ["0"])[0]), 0.0), MAX_WAIT)
                except ValueError:
                    return self._send(400, {"error": "after and wait must be numbers"})
                deadline = time.monotonic() + wait
                batch = reader.after(after)
                while not batch and time.monotonic() < deadline:
                    time.sleep(POLL)
                    batch = reader.after(after)
                if batch:
                    logger.info("%s ← %d event(s) after #%d", follower,
                                len(batch), after)
                return self._send(200, {"events": batch,
                                        "latest_seq": reader.latest()})

            return self._send(404, {"error": "not found"})

    return Handler


def serve(host: str, port: int, journal: Path, tokens: Tokens) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(JournalReader(journal), tokens))
    httpd.daemon_threads = True
    return httpd


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Zcastor follower feed")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--journal", default=str(DATA / "signal_feed.jsonl"))
    ap.add_argument("--tokens", default=str(DATA / "feed_tokens.json"))
    ap.add_argument("--add-follower", metavar="NAME")
    ap.add_argument("--revoke", metavar="NAME")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    tokens = Tokens(Path(args.tokens))
    if args.add_follower:
        token = tokens.add(args.add_follower)
        print(f"token for {args.add_follower} (shown once — send it privately):")
        print(token)
        return 0
    if args.revoke:
        print("revoked" if tokens.revoke(args.revoke) else "no such follower")
        return 0
    if args.list:
        print("\n".join(tokens.names()) or "no followers")
        return 0

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    if not tokens.names():
        logger.warning("no followers yet — add one with --add-follower NAME")
    httpd = serve(args.host, args.port, Path(args.journal), tokens)
    logger.info("feed on http://%s:%d — journal %s", args.host, args.port,
                args.journal)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
