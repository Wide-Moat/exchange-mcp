"""The process: one Starlette app with the MCP endpoint, OAuth, the sign-in page and file links on the
public port, and Prometheus metrics on a port of their own. Tokens are sealed (`keys.seal`), so a
restart with the same key keeps everyone signed in. Used sign-in codes, attempt windows, the send bucket
and upload marks live in the shared state store (`state`): Redis or Valkey for any number of replicas,
or this process's memory for exactly one.
"""

from __future__ import annotations

import asyncio
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

from exchange_mcp import metrics
from exchange_mcp.config import ConfigError, Settings
from exchange_mcp.core import context
from exchange_mcp.keys.harden import non_dumpable
from exchange_mcp.keys.providers import make_sealer
from exchange_mcp.keys.seal import Sealer
from exchange_mcp.links import routes as link_routes
from exchange_mcp.links.links import Links
from exchange_mcp.oauth.provider import Provider
from exchange_mcp.server import ExchangeServer
from exchange_mcp.signin import AuthorizeErrorPage, SignIn, static
from exchange_mcp.state.remote import make_store
from exchange_mcp.state.store import Namer, StateStore

log = logging.getLogger("exchange_mcp")


def http_client(settings: Settings, *, connections: int | None = None) -> httpx.AsyncClient:
    """TLS is always verified, against the operator's CA bundle when there is one (manifesto rule 22)."""
    verify: ssl.SSLContext = ssl.create_default_context(cafile=str(settings.ca_bundle) if settings.ca_bundle else None)
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections or 20)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=10), verify=verify, follow_redirects=False, limits=limits
    )


def build(
    settings: Settings,
    http: httpx.AsyncClient | None = None,
    sealer: Sealer | None = None,
    store: StateStore | None = None,
) -> tuple[Starlette, ExchangeServer, Provider]:
    if http is None:
        from exchange_mcp.ews import client as ews_client

        http = http_client(settings)
        ews_client.isolated = lambda: http_client(settings, connections=1)
    sealer = sealer or make_sealer(settings, http)
    if store is None:
        prefix = settings.state.prefix if settings.state else "exchange-mcp:"
        store = make_store(settings, Namer(sealer.derive, prefix))
    rt = context.Runtime(settings=settings, http=http, sealer=sealer, store=store)
    rt.links = Links(settings.public_url, sealer, store)
    context.install(rt)

    adfs = None
    renew = None
    if settings.auth_mode == "adfs":
        from exchange_mcp.auth.adfs import Adfs

        adfs = Adfs(settings, http, sealer)
        renew = adfs.renew

    provider = Provider(
        sealer,
        store,
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
        """Liveness: the process answers."""
        return JSONResponse({"ok": True})

    async def readyz(_: Request) -> Response:
        """Readiness: the key seals and opens. The state store is reported and measured
        (`mcp_state_store_up`) but does not decide: with the store down every pod would leave the Service at
        once, while reading mail needs no store. Sign-in and sending refuse on their own (fail closed)."""
        try:
            key = await sealer.open("ready", await sealer.seal("ready", {}, 60)) is not None
        except Exception:  # noqa: BLE001 - a secrets manager that fails is the answer, not an error
            key = False
        store_ok = await store.ping()
        body = {"ok": key, "key": key, "store": store_ok, "state": store.kind}
        return JSONResponse(body, status_code=200 if key else 503)

    rt.health = {"/healthz": healthz, "/readyz": readyz}  # served again on the metrics port, for probes

    server.custom_route("/healthz", methods=["GET"])(healthz)
    server.custom_route("/readyz", methods=["GET"])(readyz)
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
                await store.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(AuthorizeErrorPage)
    if settings.trusted_proxies:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        # X-Forwarded-For and -Proto are believed from these peers only; from anyone else they are ignored.
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=list(settings.trusted_proxies))
    app.add_middleware(metrics.HttpMetrics)  # outermost: counts every answer, the error pages included
    return app, server, provider


class _SideServer:
    """Built on first use, so importing this module does not import uvicorn."""

    @staticmethod
    def make(config):
        import uvicorn

        class Quiet(uvicorn.Server):
            """The metrics port's server: no signal handlers of its own. The public server owns the signals
            and stops this one when it stops."""

            @contextlib.contextmanager
            def capture_signals(self):
                yield

        return Quiet(config)


async def serve(app: Starlette, settings: Settings) -> None:
    import uvicorn

    from exchange_mcp.observability import level_name

    # A container listens on all interfaces; a laptop run binds loopback (scripts/run-local.sh).
    host = os.environ.get("EXCHANGE_MCP_LISTEN", "0.0.0.0")  # noqa: S104
    # access logs carry query strings; proxy headers are handled by the app, from trusted proxies only
    common = {"log_level": level_name().lower(), "access_log": False, "log_config": None, "proxy_headers": False}
    public = uvicorn.Server(uvicorn.Config(app, host=host, port=settings.port, **common))
    side_app = metrics.metrics_app(context.runtime().health)
    side = _SideServer.make(uvicorn.Config(side_app, host=host, port=settings.metrics_port, **common))
    task = asyncio.create_task(side.serve())
    try:
        await public.serve()
    finally:
        side.should_exit = True
        await task


def main() -> None:
    from exchange_mcp.observability import configure

    configure()
    # before any setting or key is read (keys/harden.py)
    if not non_dumpable():
        log.warning("this system cannot hide the process memory from its own user; run it on Linux in production")
    try:
        settings = Settings.from_env()
        app, _, _ = build(settings)
    except ConfigError as e:
        log.error("start refused: %s", e, extra={"event": "config_refused"})
        raise SystemExit(2) from None
    if not settings.trusted_proxies:
        log.warning(
            "no trusted proxies: sign-in attempts are limited per login only; set EXCHANGE_MCP_TRUSTED_PROXIES "
            "to the ingress controllers' networks to limit them per client network too",
            extra={"event": "network_limit_off"},
        )
    if settings.state is None:
        log.warning(
            "shared state is kept in this process: run exactly one replica, or set EXCHANGE_MCP_STATE_URL or "
            "EXCHANGE_MCP_STATE_SENTINELS",
            extra={"event": "state_in_memory", "store": "memory"},
        )
    log.info(
        "starting mode=%s keys=%s state=%s metrics port %s",
        settings.auth_mode,
        settings.key_provider,
        settings.state.mode if settings.state else "memory",
        settings.metrics_port,
        extra={"event": "starting", "port": settings.port},
    )
    asyncio.run(serve(app, settings))


if __name__ == "__main__":
    main()
