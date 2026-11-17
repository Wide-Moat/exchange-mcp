"""What one tool call runs with: the runtime of the server, and the person behind the token.

`call()` opens the access token and takes the person's credential out of it: the server keeps none of
its own. A tool never sees a token, a host or a mailbox argument (manifesto rule 16): the person is
whoever the token belongs to, and the EWS address is the operator's.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token

from exchange_mcp.auth.credential import Credential, person_key
from exchange_mcp.config import Settings
from exchange_mcp.core.cursor import Cursors
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.core.guard import Guard
from exchange_mcp.ews.client import Ews
from exchange_mcp.keys.seal import Sealer


@dataclass
class Runtime:
    settings: Settings
    http: httpx.AsyncClient
    sealer: Sealer
    guard: Guard = field(default_factory=Guard)
    clock: Callable[[], float] = time.time
    cursors: Cursors = field(init=False)
    links: Any = None  # links.Links, set by the app

    def __post_init__(self) -> None:
        self.cursors = Cursors(self.sealer)


_RUNTIME: Runtime | None = None
# Set by tests (and nothing else) to act as a person without an HTTP request.
credential_override: ContextVar[Credential | None] = ContextVar("credential_override", default=None)


def install(rt: Runtime) -> None:
    global _RUNTIME
    _RUNTIME = rt


def runtime() -> Runtime:
    if _RUNTIME is None:
        raise RuntimeError("runtime not installed")
    return _RUNTIME


@dataclass
class Call:
    tool: str
    person: str
    cred: Credential
    ews: Ews
    rt: Runtime

    def guard(self, *, kind: str, user_confirmed: bool | None, recipients: int = 0, target: str | None = None) -> dict:
        return self.rt.guard.check(
            self.person, kind=kind, user_confirmed=user_confirmed, recipients=recipients, target=target
        )

    async def cursor_in(self, cursor: str | None, scope: dict | None = None) -> dict | None:
        return await self.rt.cursors.read(self.tool, self.person, cursor, scope) if cursor else None

    async def cursor_out(self, state: dict | None, scope: dict | None = None) -> str | None:
        return await self.rt.cursors.make(self.tool, self.person, state, scope) if state else None

    @property
    def me(self) -> str:
        return self.cred.address

    @property
    def tz(self):
        from zoneinfo import ZoneInfo

        return ZoneInfo(self.rt.settings.timezone)


async def _credential(rt: Runtime) -> Credential | None:
    override = credential_override.get()
    if override is not None:
        return override
    token = get_access_token()
    if token is None:
        return None
    body = await rt.sealer.open("access", token.token)
    return Credential.from_payload(body["c"]) if body else None


@asynccontextmanager
async def call(tool: str) -> AsyncIterator[Call]:
    rt = runtime()
    cred = await _credential(rt)
    if cred is None:
        raise ExchangeError("reconnect", signin=f"{rt.settings.public_url}/signin")
    ews = Ews(rt.http, rt.settings.ews_url, cred.auth(), timezone_id=None)
    yield Call(tool=tool, person=person_key(cred.login), cred=cred, ews=ews, rt=rt)
