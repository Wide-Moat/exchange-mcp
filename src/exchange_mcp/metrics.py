"""Prometheus metrics, served on a port of their own (`EXCHANGE_MCP_METRICS_PORT`, 9090) and never on the
public one.

The names are shared with the other MCP servers of this family: every metric starts with `mcp_` and
carries the constant label `server`, so one dashboard and one autoscaling rule fit all of them. A label
never carries a person, a login, an address or a tool argument: route groups, tool names from the
registry, status classes and fixed reasons only.
"""

from __future__ import annotations

import time

import prometheus_client
from prometheus_client import Counter, Gauge, Histogram
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from exchange_mcp import __version__

SERVER = "exchange-mcp"
ROUTE_GROUPS = ("mcp", "oauth", "signin", "links", "health", "other")
METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"})

prometheus_client.disable_created_metrics()

HTTP_IN_FLIGHT = Gauge("mcp_http_requests_in_flight", "HTTP requests being served now", ["server", "route_group"])
HTTP_REQUESTS = Counter(
    "mcp_http_requests", "HTTP requests served", ["server", "route_group", "method", "status_class"]
)
HTTP_DURATION = Histogram(
    "mcp_http_request_duration_seconds", "Time to serve an HTTP request", ["server", "route_group"]
)
TOOL_CALLS = Counter("mcp_tool_calls", "Tool calls by outcome: ok, refused, error", ["server", "tool", "result"])
TOOL_DURATION = Histogram(
    "mcp_tool_duration_seconds",
    "Time to run a tool call",
    ["server", "tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
UPSTREAM_REQUESTS = Counter("mcp_upstream_requests", "Requests to Exchange and AD FS", ["server", "status_class"])
UPSTREAM_DURATION = Histogram("mcp_upstream_duration_seconds", "Time of a request to Exchange or AD FS", ["server"])
SIGNIN = Counter("mcp_signin", "Sign-in attempts by outcome: ok, wrong, limited, error", ["server", "result"])
AUTH_REFUSALS = Counter("mcp_auth_refusals", "OAuth artefacts refused, by reason", ["server", "reason"])
GUARD_REFUSALS = Counter("mcp_guard_refusals", "Actions others see that the guard refused", ["server", "reason"])
LINK_TRANSFERS = Counter("mcp_link_transfers", "File link downloads and uploads", ["server", "op", "result"])
STORE_UP = Gauge("mcp_state_store_up", "1 when the last shared-state operation succeeded", ["server"])
STORE_ERRORS = Counter("mcp_state_store_errors", "Shared-state operations that failed", ["server", "op"])
STORE_DURATION = Histogram(
    "mcp_state_store_duration_seconds",
    "Time of a shared-state operation",
    ["server", "op"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
BUILD_INFO = Gauge("mcp_build_info", "1, labelled with the running version", ["server", "version"])
LOG_DROPPED = Counter("mcp_log_records_dropped", "Log records dropped because they looked like content", ["server"])

BUILD_INFO.labels(SERVER, __version__).set(1)
for _group in ROUTE_GROUPS:
    HTTP_IN_FLIGHT.labels(SERVER, _group).set(0)
LOG_DROPPED.labels(SERVER)


def status_class(status: int | None) -> str:
    return f"{status // 100}xx" if status and 100 <= status < 600 else "error"


def route_group(path: str) -> str:
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    if path in ("/healthz", "/readyz"):
        return "health"
    if path.startswith(("/signin", "/adfs/", "/static/")):
        return "signin"
    if path.startswith("/files/"):
        return "links"
    if path.startswith(("/authorize", "/token", "/register", "/revoke", "/.well-known/")):
        return "oauth"
    return "other"


class HttpMetrics:
    """ASGI middleware: in flight, count and duration of every HTTP request, by route group."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        group = route_group(scope.get("path", ""))
        method = scope.get("method", "")
        method = method if method in METHODS else "other"
        status: list[int] = []

        async def capture(message) -> None:
            if message["type"] == "http.response.start" and not status:
                status.append(message["status"])
            await send(message)

        gauge = HTTP_IN_FLIGHT.labels(SERVER, group)
        gauge.inc()
        started = time.perf_counter()
        try:
            await self.app(scope, receive, capture)
        finally:
            gauge.dec()
            HTTP_DURATION.labels(SERVER, group).observe(time.perf_counter() - started)
            HTTP_REQUESTS.labels(SERVER, group, method, status_class(status[0] if status else None)).inc()


def upstream_status(status: int | None, seconds: float) -> None:
    UPSTREAM_DURATION.labels(SERVER).observe(seconds)
    UPSTREAM_REQUESTS.labels(SERVER, status_class(status)).inc()


def signin(result: str) -> None:
    SIGNIN.labels(SERVER, result).inc()


def auth_refused(reason: str) -> None:
    AUTH_REFUSALS.labels(SERVER, reason).inc()


def guard_refused(reason: str) -> None:
    GUARD_REFUSALS.labels(SERVER, reason).inc()


def link(op: str, result: str) -> None:
    LINK_TRANSFERS.labels(SERVER, op, result).inc()


def value(name: str, **labels: str) -> float:
    """The current value of one series, for tests and the readiness answer; 0 when it does not exist."""
    got = prometheus_client.REGISTRY.get_sample_value(name, {"server": SERVER, **labels})
    return got or 0.0


async def _metrics(_: Request) -> Response:
    return Response(prometheus_client.generate_latest(), media_type=prometheus_client.CONTENT_TYPE_LATEST)


def metrics_app(health: dict | None = None) -> Starlette:
    """The app on the metrics port: `/metrics`, and the same `/healthz` and `/readyz` as the public port, so
    probes never need the public one."""
    routes = [Route("/metrics", _metrics, methods=["GET"])]
    routes += [Route(path, handler, methods=["GET"]) for path, handler in (health or {}).items()]
    return Starlette(routes=routes)
