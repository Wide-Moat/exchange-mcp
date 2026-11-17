"""A fake Exchange: it answers only the operations a test registers and records every request, so a test
can assert exactly what reached EWS, including that nothing did.

Response bodies are built from the same namespaces as the client; shapes follow what a real Exchange
returned (docs/live-findings.md).
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from lxml import etree

from exchange_mcp.auth.credential import Credential
from exchange_mcp.config import Settings
from exchange_mcp.core import context
from exchange_mcp.ews.client import WRITES, operation_name
from exchange_mcp.ews.soap import NS_M, NS_S, NS_T, parse, q
from exchange_mcp.keys.seal import LocalSealer

EWS_URL = "https://mail.example.com/EWS/Exchange.asmx"
PUBLIC = "https://mcp.example.com"
ME = "alex@corp.example"


@dataclass
class Seen:
    operation: str
    root: etree._Element
    headers: dict[str, str]
    url: str

    @property
    def body(self) -> etree._Element:
        return self.root.find(q("s", "Body"))[0]

    def xml(self) -> str:
        return etree.tostring(self.root).decode()


def envelope(inner: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{NS_S}" xmlns:m="{NS_M}" xmlns:t="{NS_T}">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    ).encode()


def ok(operation: str, inner: str = "", *, n: int = 1) -> bytes:
    msg = f'<m:{operation}ResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>{inner}</m:{operation}ResponseMessage>'
    return envelope(
        f"<m:{operation}Response><m:ResponseMessages>{msg * n}</m:ResponseMessages></m:{operation}Response>"
    )


def error(operation: str, code: str) -> bytes:
    msg = f'<m:{operation}ResponseMessage ResponseClass="Error"><m:MessageText>x</m:MessageText><m:ResponseCode>{code}</m:ResponseCode></m:{operation}ResponseMessage>'
    return envelope(f"<m:{operation}Response><m:ResponseMessages>{msg}</m:ResponseMessages></m:{operation}Response>")


Handler = Callable[[Seen], Any]


@dataclass
class FakeEws:
    routes: dict[str, list[Any]] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)

    def on(self, operation: str, *responses: Any) -> FakeEws:
        """Answer `operation` with these responses in order; the last one repeats. A response is bytes, an
        int status, or a callable taking the request."""
        self.routes[operation] = list(responses)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        root = parse(request.content)
        op = operation_name(root.find(q("s", "Body"))[0])
        seen = Seen(op, root, dict(request.headers), str(request.url))
        self.seen.append(seen)
        if str(request.url) != EWS_URL:
            return httpx.Response(599, content=b"wrong host")
        queue = self.routes.get(op)
        if not queue:
            raise AssertionError(f"unexpected EWS operation {op}")
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(r):
            r = r(seen)
        if isinstance(r, int):
            return httpx.Response(r)
        return httpx.Response(200, content=r, headers={"Content-Type": "text/xml; charset=utf-8"})

    def ops(self) -> list[str]:
        return [s.operation for s in self.seen]

    def attempts(self) -> int:
        """Separate calls, not handshake legs: NTLM sends an anonymous leg first, without Authorization."""
        return sum(1 for s in self.seen if "authorization" not in {k.lower() for k in s.headers})

    def writes(self) -> list[str]:
        return [o for o in self.ops() if o in WRITES]

    def last(self, operation: str) -> Seen:
        for s in reversed(self.seen):
            if s.operation == operation:
                return s
        raise AssertionError(f"{operation} was not sent")


def key_line(kid: str = "k1", key: bytes | None = None) -> str:
    return f"{kid}:{base64.urlsafe_b64encode(key or os.urandom(32)).rstrip(b'=').decode()}"


def base_env(**extra: str) -> dict[str, str]:
    env = {
        "EXCHANGE_MCP_PUBLIC_URL": PUBLIC,
        "EXCHANGE_MCP_EWS_URL": EWS_URL,
        "EXCHANGE_MCP_AUTH_MODE": "password",
        "EXCHANGE_MCP_KEY_PROVIDER": "memory",
        "EXCHANGE_MCP_LOGIN_FORMAT": "upn",
        "EXCHANGE_MCP_LOGIN_DOMAIN": "corp.example",
        "EXCHANGE_MCP_TIMEZONE": "Europe/Berlin",
    }
    env.update({f"EXCHANGE_MCP_{k}": v for k, v in extra.items()})
    return env


@pytest.fixture
def ews() -> FakeEws:
    return FakeEws()


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env(base_env())


@pytest.fixture
def sealer() -> LocalSealer:
    return LocalSealer([("k1", os.urandom(32))])


@pytest.fixture
def http(ews: FakeEws) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(ews.handler))


@pytest.fixture
def cred() -> Credential:
    return Credential("password", "alex@corp.example", ME, "Alex Example", password="pw-not-in-logs-1")


@pytest.fixture
def rt(settings, http, sealer, cred):
    from exchange_mcp.links.links import Links

    runtime = context.Runtime(settings=settings, http=http, sealer=sealer)
    runtime.links = Links(PUBLIC, sealer)
    context.install(runtime)
    token = context.credential_override.set(cred)
    yield runtime
    context.credential_override.reset(token)


@pytest.fixture
def server(rt):
    from exchange_mcp.server import ExchangeServer

    return ExchangeServer(rt.settings.areas)


@dataclass
class Result:
    is_error: bool
    text: str
    data: Any


@pytest.fixture
def invoke(server):
    from mcp.types import CallToolRequestParams

    async def _invoke(name: str, **args: Any) -> Result:
        r = await server._handle_call_tool(None, CallToolRequestParams(name=name, arguments=args))
        text = " ".join(getattr(c, "text", "") for c in r.content)
        return Result(bool(r.is_error), text, r.structured_content)

    return _invoke
