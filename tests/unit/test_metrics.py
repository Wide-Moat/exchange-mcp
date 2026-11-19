"""Prometheus metrics: the shared names, counted where they happen, on their own port only, and no
person in any label."""

from __future__ import annotations

import os

import httpx
from conftest import ok
from test_signin import RESOLVED, Flow, _pkce

from exchange_mcp import metrics
from exchange_mcp.keys.seal import LocalSealer

SHARED = (
    "mcp_http_requests_in_flight",
    "mcp_http_requests_total",
    "mcp_http_request_duration_seconds",
    "mcp_tool_calls_total",
    "mcp_tool_duration_seconds",
    "mcp_upstream_requests_total",
    "mcp_upstream_duration_seconds",
    "mcp_signin_total",
    "mcp_auth_refusals_total",
    "mcp_guard_refusals_total",
    "mcp_link_transfers_total",
    "mcp_state_store_up",
    "mcp_state_store_errors_total",
    "mcp_state_store_duration_seconds",
    "mcp_log_records_dropped_total",
)


async def _scrape() -> str:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=metrics.metrics_app()), base_url="http://m")
    r = await client.get("/metrics")
    assert r.status_code == 200
    return r.text


async def test_every_shared_metric_is_exposed_with_the_server_label(ews, invoke, rt):
    from test_mail import item

    ews.on("GetItem", item()).on("SendItem", ok("SendItem"))
    await invoke("send_draft", id="D1", user_confirmed=True)
    await invoke("send_draft", id="D1", user_confirmed=False)
    text = await _scrape()
    for name in SHARED:
        assert f"# TYPE {name.removesuffix('_total')}" in text, name
    assert "process_cpu_seconds_total" in text or "python_info" in text  # default collectors on
    assert "_created" not in text
    for line in text.splitlines():
        if line.startswith("mcp_") and "{" in line:
            assert 'server="exchange-mcp"' in line, line


async def test_tool_calls_are_counted_by_outcome(ews, invoke, rt):
    from test_mail import item

    def count(result: str) -> float:
        return metrics.value("mcp_tool_calls_total", tool="send_draft", result=result)

    before = {r: count(r) for r in ("ok", "error", "crash")}
    ews.on("GetItem", item()).on("SendItem", ok("SendItem"))
    await invoke("send_draft", id="D1", user_confirmed=True)
    await invoke("send_draft", id="D1", user_confirmed=False)
    ews.on("GetItem", lambda s: (_ for _ in ()).throw(RuntimeError("crash")))
    await invoke("send_draft", id="D1", user_confirmed=True)
    assert {r: count(r) - before[r] for r in before} == {"ok": 1, "error": 1, "crash": 1}
    before_unknown = metrics.value("mcp_tool_calls_total", tool="unknown", result="error")
    await invoke("forward_everything", to="x")
    assert metrics.value("mcp_tool_calls_total", tool="unknown", result="error") == before_unknown + 1
    assert metrics.value("mcp_guard_refusals_total", reason="confirm") >= 1


async def test_sign_in_outcomes_and_http_requests_are_counted(ews):
    ews.on("ResolveNames", RESOLVED)
    f = Flow(ews, sealer=LocalSealer([("k1", os.urandom(32))]), ATTEMPTS_PER_LOGIN="1")
    ok_before = metrics.value("mcp_signin_total", result="ok")
    limited_before = metrics.value("mcp_signin_total", result="limited")
    http_before = metrics.value("mcp_http_requests_total", route_group="signin", method="POST", status_class="3xx")
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    assert (await f.submit(rid, "alex", "pw-not-in-logs-1")).status_code == 302
    assert (await f.submit(rid, "alex", "pw-not-in-logs-1")).status_code == 429
    assert metrics.value("mcp_signin_total", result="ok") == ok_before + 1
    assert metrics.value("mcp_signin_total", result="limited") == limited_before + 1
    assert (
        metrics.value("mcp_http_requests_total", route_group="signin", method="POST", status_class="3xx")
        == http_before + 1
    )
    for group in metrics.ROUTE_GROUPS:  # nothing left in flight once the answers are out
        assert metrics.value("mcp_http_requests_in_flight", route_group=group) == 0
    assert metrics.value("mcp_upstream_requests_total", status_class="2xx") >= 1


async def test_a_refused_bearer_is_counted(ews):
    f = Flow(ews, sealer=LocalSealer([("k1", os.urandom(32))]))
    before = metrics.value("mcp_auth_refusals_total", reason="invalid_token")
    r = await f.client.post("/mcp", headers={"Authorization": "Bearer k1.forged"}, json={})
    assert r.status_code == 401
    assert metrics.value("mcp_auth_refusals_total", reason="invalid_token") == before + 1


async def test_the_public_port_has_no_metrics(ews):
    f = Flow(ews, sealer=LocalSealer([("k1", os.urandom(32))]))
    assert (await f.client.get("/metrics")).status_code == 404


async def test_no_label_carries_a_person(ews, invoke, rt):
    from test_mail import item

    ews.on("GetItem", item()).on("SendItem", ok("SendItem"))
    await invoke("send_draft", id="D1", user_confirmed=True)
    text = await _scrape()
    for clear in ("alex", "corp.example", "pat@", "D1", "10.1.2.3"):
        assert clear not in text, clear


def test_route_groups():
    assert metrics.route_group("/mcp") == "mcp"
    assert metrics.route_group("/.well-known/oauth-authorization-server") == "oauth"
    assert metrics.route_group("/token") == "oauth"
    assert metrics.route_group("/signin") == "signin"
    assert metrics.route_group("/adfs/callback") == "signin"
    assert metrics.route_group("/files/d/x") == "links"
    assert metrics.route_group("/readyz") == "health"
    assert metrics.route_group("/wp-admin") == "other"


async def test_a_pod_stays_far_below_the_series_limit_after_every_tool(ews, invoke, server, rt):
    """Labels are bounded (tool names, route groups, fixed outcomes): every tool called, ok or refused,
    leaves a pod with a few hundred series at most, never one per person or argument."""
    for name in server.specs:
        await invoke(name)  # no arguments: refused by validation, still counted
        await invoke(name, id="X1", user_confirmed=False)
    samples = [line for line in (await _scrape()).splitlines() if line and not line.startswith("#")]
    assert 100 < len(samples) < 5000, len(samples)


def test_log_level_applies_to_every_logger(monkeypatch):
    import logging

    from exchange_mcp.observability import configure

    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    configure()
    assert logging.getLogger().level == logging.WARNING
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure()
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING  # content below WARNING stays off
    monkeypatch.setenv("LOG_LEVEL", "nonsense")
    configure()
    assert logging.getLogger().level == logging.INFO
    monkeypatch.delenv("LOG_LEVEL")
    configure()
