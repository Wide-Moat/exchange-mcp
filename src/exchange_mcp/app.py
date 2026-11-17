"""The process: one Starlette app with the MCP endpoint, OAuth, the sign-in page and file links. It keeps
no state between requests (`keys.seal`), so replicas sharing the sealing key serve the same people.
"""

from __future__ import annotations

import contextlib
import logging
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
from exchange_mcp.keys.providers import make_sealer
from exchange_mcp.keys.seal import Sealer
from exchange_mcp.oauth.provider import Provider
from exchange_mcp.server import ExchangeServer
from exchange_mcp.signin import AuthorizeErrorPage, SignIn, static

log = logging.getLogger("exchange_mcp")


def http_client(settings: Settings) -> httpx.AsyncClient:
    """TLS is always verified, against the operator's CA bundle when there is one (manifesto rule 22)."""
    verify: ssl.SSLContext = ssl.create_default_context(cafile=str(settings.ca_bundle) if settings.ca_bundle else None)
    return httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), verify=verify, follow_redirects=False)


def build(
    settings: Settings, http: httpx.AsyncClient | None = None, sealer: Sealer | None = None
) -> tuple[Starlette, ExchangeServer, Provider]:
    http = http or http_client(settings)
    sealer = sealer or make_sealer(settings, http)
    rt = context.Runtime(settings=settings, http=http, sealer=sealer)
    context.install(rt)

    adfs = None
    renew = None
    if settings.auth_mode == "adfs":
        from exchange_mcp.auth.adfs import Adfs

        adfs = Adfs(settings, http, sealer)
        renew = adfs.renew

    provider = Provider(sealer, public_url=settings.public_url, refresh_ttl=settings.refresh_hours * 3600, renew=renew)
    auth = AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=f"{settings.public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,
    )
    server = ExchangeServer(settings.areas, auth=auth, auth_server_provider=provider)
    signin = SignIn(provider, adfs)

    server.custom_route("/signin", methods=["GET"])(signin.page)
    server.custom_route("/signin", methods=["POST"])(signin.submit)
    server.custom_route("/static/{path:path}", methods=["GET"])(static)
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
    settings = Settings.from_env()
    app, _, _ = build(settings)
    log.info("starting mode=%s keys=%s", settings.auth_mode, settings.key_provider)
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - a container listens on all interfaces
        port=settings.port,
        log_level="warning",
        access_log=False,  # access logs carry query strings
        log_config=None,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
