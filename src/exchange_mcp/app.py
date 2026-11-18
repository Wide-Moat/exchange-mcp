"""The process: one Starlette app with the MCP endpoint, OAuth, the sign-in page and file links. Tokens
are sealed (`keys.seal`), so a restart with the same key keeps everyone signed in. Used sign-in codes,
attempt counters and the send bucket live in this process, so one replica is the supported count.
"""

from __future__ import annotations

import contextlib
import logging
import os
import ssl
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from exchange_mcp.config import Settings
from exchange_mcp.core import context
from exchange_mcp.keys.harden import non_dumpable
from exchange_mcp.keys.providers import make_sealer
from exchange_mcp.keys.seal import Sealer
from exchange_mcp.links import routes as link_routes
from exchange_mcp.links.links import Links
from exchange_mcp.oauth.provider import Provider
from exchange_mcp.server import ExchangeServer
from exchange_mcp.signin import AuthorizeErrorPage, SignIn, static

log = logging.getLogger("exchange_mcp")


def http_client(settings: Settings, *, connections: int | None = None) -> httpx.AsyncClient:
    """TLS is always verified, against the operator's CA bundle when there is one (manifesto rule 22)."""
    verify: ssl.SSLContext = ssl.create_default_context(cafile=str(settings.ca_bundle) if settings.ca_bundle else None)
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections or 20)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=10), verify=verify, follow_redirects=False, limits=limits
    )


def build(
    settings: Settings, http: httpx.AsyncClient | None = None, sealer: Sealer | None = None
) -> tuple[Starlette, ExchangeServer, Provider]:
    if http is None:
        from exchange_mcp.ews import client as ews_client

        http = http_client(settings)
        ews_client.isolated = lambda: http_client(settings, connections=1)
    sealer = sealer or make_sealer(settings, http)
    rt = context.Runtime(settings=settings, http=http, sealer=sealer)
    rt.links = Links(settings.public_url, sealer)
    context.install(rt)

    adfs = None
    renew = None
    if settings.auth_mode == "adfs":
        from exchange_mcp.auth.adfs import Adfs

        adfs = Adfs(settings, http, sealer)
        renew = adfs.renew

    provider = Provider(
        sealer,
        public_url=settings.public_url,
        refresh_ttl=settings.refresh_hours * 3600,
        renew=renew,
        client_hosts=settings.client_hosts,
    )
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=f"{settings.public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        # Off: this server stores nothing, so a revoked token would go on working (docs/auth-modes.md).
        revocation_options=RevocationOptions(enabled=False),
        validate_token_resource=False,
    )
    server = ExchangeServer(settings.areas, auth=auth, auth_server_provider=provider)
    signin = SignIn(provider, adfs)

    server.custom_route("/signin", methods=["GET"])(signin.page)
    server.custom_route("/signin", methods=["POST"])(signin.submit)
    server.custom_route("/static/{path:path}", methods=["GET"])(static)
    server.custom_route("/files/d/{lid}", methods=["GET"])(link_routes.download)
    server.custom_route("/files/u/{lid}", methods=["PUT", "POST"])(link_routes.upload)
    if adfs is not None:
        server.custom_route("/adfs/callback", methods=["GET"])(adfs.callback_route(provider))

    async def healthz(_: Request) -> Response:
        return JSONResponse({"ok": True})

    server.custom_route("/healthz", methods=["GET"])(healthz)
    host = urlsplit(settings.public_url).netloc
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host], allowed_origins=[settings.public_url]
        ),
    )
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):  # the outbound connection pool closes with the app
        async with inner(a) as state:
            try:
                yield state
            finally:
                await http.aclose()

    app.router.lifespan_context = lifespan
    app.add_middleware(AuthorizeErrorPage)
    return app, server, provider


def main() -> None:
    import uvicorn

    from exchange_mcp.observability import configure

    configure()
    # before any setting or key is read (keys/harden.py)
    if not non_dumpable():
        log.warning("this system cannot hide the process memory from its own user; run it on Linux in production")
    settings = Settings.from_env()
    app, _, _ = build(settings)
    log.info("starting mode=%s keys=%s", settings.auth_mode, settings.key_provider)
    uvicorn.run(
        app,
        # A container listens on all interfaces; a laptop run binds loopback (scripts/run-local.sh).
        host=os.environ.get("EXCHANGE_MCP_LISTEN", "0.0.0.0"),  # noqa: S104
        port=settings.port,
        log_level="warning",
        access_log=False,  # access logs carry query strings
        log_config=None,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
