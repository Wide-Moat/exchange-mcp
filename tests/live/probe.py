"""A live probe: an MCP client that signs in through the browser and calls the read-only tools.

    uv run python tests/live/probe.py http://localhost:8765

It registers itself, opens the sign-in page in the default browser (the person types their password
there, never here), receives the code on a loopback port, and calls each read tool once. It prints the
shape of every answer (keys, counts, errors), never mail content, names or addresses.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

READS = [
    ("get_connection", {}),
    ("list_messages", {"limit": 3}),
    ("list_messages", {"kind": "folders", "limit": 5}),
    ("list_messages", {"query": "report", "limit": 2}),
    ("list_events", {"start": "2026-11-17 00:00", "end": "2026-11-24 00:00", "limit": 3}),
    ("list_tasks", {"limit": 3}),
    ("list_people", {"source": "rooms", "query": "", "limit": 3}),
]


def shape(value, depth: int = 0):
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()} if depth < 2 else f"dict[{len(value)}]"
    if isinstance(value, list):
        return [shape(value[0], depth + 1), f"... {len(value)} items"] if value else []
    if isinstance(value, bool) or value is None:
        return value
    return type(value).__name__


def catch_code(port: int) -> dict:
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urlsplit(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in parse_qs(u.query).items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Connected. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)

    def serve() -> None:
        while not got:
            srv.handle_request()

    Thread(target=serve, daemon=True).start()
    return got


SEND_TO = sys.argv[2] if len(sys.argv) > 2 else None  # the person's own word: an address to send one test to
EXTERNAL = "--external" in sys.argv
TOKEN_FILE = os.environ.get("PROBE_TOKEN_FILE")  # optional: reuse one sign-in across runs (0600)


async def signed_in(base: str) -> dict:
    """A token for `base`: the saved one refreshed, or a new browser sign-in."""
    if TOKEN_FILE and os.path.exists(TOKEN_FILE):
        saved = json.loads(open(TOKEN_FILE).read())
        if saved.get("base") not in (None, base):
            sys.exit("the saved sign-in belongs to another server; remove PROBE_TOKEN_FILE or point at that server")
        if "client_id" not in saved:  # an old file that cannot be refreshed: sign in again
            saved = {"client_id": "", "refresh_token": ""}
        async with httpx.AsyncClient(base_url=base) as h:  # a fresh access token from the refresh token
            r = await h.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": saved["refresh_token"],
                    "client_id": saved["client_id"],
                },
            )
        if r.status_code == 200:
            tok = {**r.json(), "client_id": saved["client_id"], "base": base}
            save(tok)
            return tok
        print("the saved sign-in has ended; signing in again", flush=True)
    port = 33418
    redirect = f"http://127.0.0.1:{port}/callback"
    async with httpx.AsyncClient(base_url=base) as h:
        cid = (
            await h.post(
                "/register",
                json={
                    "redirect_uris": [redirect],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
        ).json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(8)
        got = catch_code(port)
        url = f"{base}/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": redirect,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        if not webbrowser.open(url):
            print("open this address in a browser:", url, flush=True)
        print("sign-in page opened in the browser; waiting...", flush=True)
        for _ in range(int(os.environ.get("PROBE_WAIT_SECONDS", "300")) * 2):
            if got:
                break
            await asyncio.sleep(0.5)
        if got.get("state") != state or "code" not in got:
            sys.exit(f"no code came back: {sorted(got)}")
        tok = (
            await h.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": got["code"],
                    "redirect_uri": redirect,
                    "client_id": cid,
                    "code_verifier": verifier,
                },
            )
        ).json()
    print("token:", sorted(tok), "expires_in", tok.get("expires_in"))
    tok["client_id"] = cid
    tok["base"] = base
    save(tok)
    return tok


async def main(base: str) -> None:
    await probe(base, await signed_in(base))


def save(tok: dict) -> None:
    if TOKEN_FILE:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # also when the file existed with wider rights
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(tok))


async def probe(base: str, tok: dict) -> None:
    headers = {"Authorization": f"Bearer {tok['access_token']}"}
    import httpx2

    client = httpx2.AsyncClient(headers=headers, timeout=60)
    async with (
        streamable_http_client(f"{base}/mcp", http_client=client) as streams,
        ClientSession(streams[0], streams[1]) as s,
    ):
        await s.initialize()
        tools = await s.list_tools()
        print("tools:", len(tools.tools))
        if SEND_TO:
            await send_check(s, SEND_TO)
            return
        for name, args in READS:
            res = await s.call_tool(name, args)
            out = res.structured_content
            text = " ".join(getattr(c, "text", "") for c in res.content)
            print(f"\n== {name} {json.dumps(args)} error={res.is_error}")
            print(text[:300] if res.is_error else json.dumps(shape(out), ensure_ascii=False)[:1200])


async def send_check(s: ClientSession, to: str) -> None:
    """Draft, read the preview, send with the person's confirmation, find it in Sent Items. Any expectation
    that does not hold stops the probe with a non-zero exit."""

    def expect(ok: bool, what: str) -> None:
        print(("ok    " if ok else "FAIL  ") + what, flush=True)
        if not ok:
            sys.exit(1)

    subject = f"exchange-mcp live check {secrets.token_hex(3)}"
    body = "Test message from the exchange-mcp live check: draft, preview, then send. No reply needed."
    d = await s.call_tool("save_draft", {"to": [to], "subject": subject, "body": body})
    expect(not d.is_error, "save_draft saved a draft")
    draft = d.structured_content
    expect(draft.get("to") == [to] or draft.get("to") == [to.lower()], "the draft is addressed as asked")
    if EXTERNAL:  # the person said the address is outside the organisation
        expect(draft.get("external_recipients") == draft["to"], "the preview flags the external recipient")
    refused = await s.call_tool("send_draft", {"id": draft["id"], "user_confirmed": "yes"})
    expect(refused.is_error, 'send_draft refuses "yes" as a confirmation')
    sent = await s.call_tool("send_draft", {"id": draft["id"], "user_confirmed": True})
    expect(not sent.is_error and (sent.structured_content or {}).get("sent") is True, "send_draft sent it")
    again = await s.call_tool("send_draft", {"id": draft["id"], "user_confirmed": True})
    expect(again.is_error, "the same draft is not sent twice")
    # Not by search: Exchange indexes a new message minutes later (live), so the newest items are read instead.
    for _ in range(10):
        await asyncio.sleep(2)
        found = await s.call_tool("list_messages", {"folder": "sent", "limit": 3})
        items = (found.structured_content or {}).get("items", [])
        if any(subject in i.get("subject", "") for i in items[:1]):
            break
    expect(any(subject in i.get("subject", "") for i in items[:1]), "the newest item in Sent Items is this one")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1].rstrip("/")))
