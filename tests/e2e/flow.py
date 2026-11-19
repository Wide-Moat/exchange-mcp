"""OAuth and sign-in steps against one pod, for scripts/chart-e2e.sh. Standard library only.

    python tests/e2e/flow.py code <port> <login> <password>     -> prints: <client_id> <verifier> <code>
    python tests/e2e/flow.py redeem <port> <client_id> <verifier> <code>   -> "200", or "<status> <error>"
    python tests/e2e/flow.py attempt <port> <login> <password>  -> prints the sign-in page's HTTP status
    python tests/e2e/flow.py draft <port> <login> <password>    -> signs in, calls save_draft over MCP with
                                                                   the access token, prints the result JSON

Each step talks to 127.0.0.1:<port> (a port-forward to one pod) with the public host in the Host header,
as a browser and an MCP client would through the ingress. `code` fails unless the sign-in succeeded.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

HOST = "mcp.example.com"
ORIGIN = f"https://{HOST}"
REDIRECT = "http://127.0.0.1:9/cb"


def call(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(method, path, body=body, headers={"Host": HOST, **(headers or {})})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), data.decode("utf-8", "replace")


def register(port: int) -> str:
    body = json.dumps(
        {
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
    ).encode()
    status, _, text = call(port, "POST", "/register", body, {"Content-Type": "application/json"})
    if status != 201:
        raise SystemExit(f"register answered {status}")
    return json.loads(text)["client_id"]


def sign_in(port: int, login: str, password: str) -> tuple[int, dict, str, str]:
    """Registers, opens the sign-in page, posts the form. Returns (status, headers, client_id, verifier)."""
    cid = register(port)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode(
        {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
        }
    )
    status, headers, _ = call(port, "GET", f"/authorize?{query}")
    if status != 302:
        raise SystemExit(f"authorize answered {status}")
    rid = parse_qs(urlsplit(headers["location"]).query)["request"][0]
    status, _, page = call(port, "GET", "/signin?" + urlencode({"request": rid}))
    if status != 200:
        raise SystemExit(f"the sign-in page answered {status}")
    token = re.search(r'name="form" value="([^"]+)"', page).group(1)
    form = urlencode({"request": rid, "login": login, "password": password, "form": token}).encode()
    status, headers, _ = call(
        port,
        "POST",
        "/signin",
        form,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": ORIGIN,
            "Cookie": f"exchange_mcp_form={token}",
        },
    )
    return status, headers, cid, verifier


def token(port: int, login: str, password: str) -> str:
    status, headers, cid, verifier = sign_in(port, login, password)
    if status != 302:
        raise SystemExit(f"sign-in answered {status}, not a redirect with a code")
    code = parse_qs(urlsplit(headers["location"]).query)["code"][0]
    form = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": cid,
            "code_verifier": verifier,
        }
    ).encode()
    status, _, text = call(port, "POST", "/token", form, {"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        raise SystemExit(f"the token endpoint answered {status}")
    return json.loads(text)["access_token"]


def mcp(port: int, access: str, method: str, params: dict, rid: int) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    status, _, text = call(port, "POST", "/mcp", body, headers)
    if status != 200:
        raise SystemExit(f"{method} answered {status}")
    return json.loads(text)


def main(argv: list[str]) -> None:
    what, port = argv[0], int(argv[1])
    if what == "code":
        status, headers, cid, verifier = sign_in(port, argv[2], argv[3])
        if status != 302:
            raise SystemExit(f"sign-in answered {status}, not a redirect with a code")
        code = parse_qs(urlsplit(headers["location"]).query)["code"][0]
        print(cid, verifier, code)
    elif what == "draft":
        access = token(port, argv[2], argv[3])
        mcp(
            port,
            access,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}},
            1,
        )
        args = {"to": [argv[2] if "@" in argv[2] else f"{argv[2]}@corp.example"], "subject": "e2e", "body": "Hello"}
        answer = mcp(port, access, "tools/call", {"name": "save_draft", "arguments": args}, 2)
        result = answer.get("result") or {}
        if result.get("isError"):
            raise SystemExit("save_draft failed: " + json.dumps(result.get("content"))[:400])
        print(json.dumps(result.get("structuredContent") or {}))
    elif what == "attempt":
        print(sign_in(port, argv[2], argv[3])[0])
    elif what == "redeem":
        cid, verifier, code = argv[2:5]
        form = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": cid,
                "code_verifier": verifier,
            }
        ).encode()
        status, _, text = call(port, "POST", "/token", form, {"Content-Type": "application/x-www-form-urlencoded"})
        if status == 200:
            print(status)
        else:
            try:
                print(status, json.loads(text).get("error", "unknown"))
            except ValueError:
                print(status, "not-json")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
