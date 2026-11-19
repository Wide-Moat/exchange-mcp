"""Many replicas: what one replica did, every other one knows; and a store that does not answer refuses
sign-in, redemption, sending and uploads (fail closed) while reading goes on.

Replicas here are separate app instances with one sealing key and their own clients to one real Valkey
(VALKEY_TEST_URL, from scripts/stores-test.sh in CI; skipped without it, and CI fails a run with a skipped test). The
"without a shared store" cases show the same checks failing when each replica keeps its own memory,
which is what 0.1.x did: a check that cannot fail would pass there too.
"""

from __future__ import annotations

import os
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import PUBLIC, base_env, memory_store, ok
from test_signin import RESOLVED, Flow, _pkce

from exchange_mcp.config import Settings
from exchange_mcp.core.guard import Guard
from exchange_mcp.keys.seal import LocalSealer
from exchange_mcp.state.remote import connect
from exchange_mcp.state.store import Namer


def valkey(sealer, prefix: str):
    url = os.environ.get("VALKEY_TEST_URL")
    if not url:
        pytest.skip("VALKEY_TEST_URL not set: no Valkey to test against")
    env = {"STATE_URL": url, "STATE_PREFIX": prefix}
    if os.environ.get("VALKEY_TEST_PASSWORD"):
        import tempfile

        fd, path = tempfile.mkstemp()
        os.write(fd, os.environ["VALKEY_TEST_PASSWORD"].encode())
        os.close(fd)
        os.chmod(path, 0o400)
        env["STATE_PASSWORD_FILE"] = path
    return connect(Settings.from_env(base_env(**env)), Namer(sealer.derive, prefix))


def dead(sealer):
    """A real client to a port nothing listens on: every operation fails as an unreachable store does."""
    return connect(Settings.from_env(base_env(STATE_URL="redis://127.0.0.1:1/0")), Namer(sealer.derive, "d:"))


def replicas(ews, *, shared: bool, **env):
    sealer = LocalSealer([("k1", os.urandom(32))])
    if shared:
        prefix = f"r{secrets.token_hex(4)}:"
        return Flow(ews, sealer=sealer, store=valkey(sealer, prefix), **env), Flow(
            ews, sealer=sealer, store=valkey(sealer, prefix), **env
        )
    return Flow(ews, sealer=sealer, store=memory_store(sealer), **env), Flow(
        ews, sealer=sealer, store=memory_store(sealer), **env
    )


async def _code(a: Flow) -> tuple[str, str, str]:
    cid = await a.register()
    verifier, challenge = _pkce()
    rid = await a.request_id(cid, challenge)
    r = await a.submit(rid, "alex", "pw-not-in-logs-1")
    assert r.status_code == 302, r.text
    return cid, verifier, parse_qs(urlsplit(r.headers["location"]).query)["code"][0]


async def _redeem(f: Flow, cid: str, verifier: str, code: str) -> httpx.Response:
    return await f.client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://client.example.org/cb",
            "client_id": cid,
            "code_verifier": verifier,
        },
    )


async def test_a_code_redeemed_on_one_replica_is_refused_on_another(ews):
    ews.on("ResolveNames", RESOLVED)
    a, b = replicas(ews, shared=True)
    cid, verifier, code = await _code(a)
    assert (await _redeem(a, cid, verifier, code)).status_code == 200
    again = await _redeem(b, cid, verifier, code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


async def test_without_a_shared_store_another_replica_accepts_a_used_code(ews):
    ews.on("ResolveNames", RESOLVED)
    a, b = replicas(ews, shared=False)
    cid, verifier, code = await _code(a)
    assert (await _redeem(a, cid, verifier, code)).status_code == 200
    assert (await _redeem(b, cid, verifier, code)).status_code == 200  # the defect the store removes


async def test_sign_in_attempts_are_counted_across_replicas(ews):
    ews.on("ResolveNames", 401)
    a, b = replicas(ews, shared=True, ATTEMPTS_PER_LOGIN="3")
    cid = await a.register()
    rid = await a.request_id(cid, _pkce()[1])
    for f in (a, b, a):
        assert (await f.submit(rid, "alex", "wrong")).status_code == 400
    assert (await b.submit(rid, "alex", "wrong")).status_code == 429
    assert ews.attempts() == 3


async def test_without_a_shared_store_each_replica_counts_its_own_attempts(ews):
    ews.on("ResolveNames", 401)
    a, b = replicas(ews, shared=False, ATTEMPTS_PER_LOGIN="3")
    cid = await a.register()
    rid = await a.request_id(cid, _pkce()[1])
    for f in (a, b, a):
        await f.submit(rid, "alex", "wrong")
    assert (await b.submit(rid, "alex", "wrong")).status_code == 400  # a fourth try reached the directory
    assert ews.attempts() == 4


async def test_the_send_bucket_is_shared_across_replicas():
    from exchange_mcp.core.errors import ExchangeError

    sealer = LocalSealer([("k1", os.urandom(32))])
    prefix = f"g{secrets.token_hex(4)}:"
    one, two = Guard(valkey(sealer, prefix)), Guard(valkey(sealer, prefix))
    for i in range(10):
        receipt = await (one, two)[i % 2].check("person", kind="mail", user_confirmed=True)
        assert receipt["sends_left"] == 9 - i
    with pytest.raises(ExchangeError) as e:
        await one.check("person", kind="mail", user_confirmed=True)
    assert e.value.key == "rate"


# ---------------------------------------------------------------- the store does not answer


async def test_sign_in_fails_closed_when_the_store_is_down(ews):
    ews.on("ResolveNames", RESOLVED)
    sealer = LocalSealer([("k1", os.urandom(32))])
    f = Flow(ews, sealer=sealer, store=dead(sealer))
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    r = await f.submit(rid, "alex", "pw-not-in-logs-1")
    assert r.status_code == 503 and "try again in a minute" in r.text
    assert ews.attempts() == 0  # not one password reached the directory uncounted


async def test_redeeming_a_code_fails_closed_when_the_store_is_down(ews):
    ews.on("ResolveNames", RESOLVED)
    sealer = LocalSealer([("k1", os.urandom(32))])
    good = Flow(ews, sealer=sealer, store=memory_store(sealer))
    cid, verifier, code = await _code(good)
    down = Flow(ews, sealer=sealer, store=dead(sealer))
    r = await _redeem(down, cid, verifier, code)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    assert "try again shortly" in r.json()["error_description"]
    assert (await _redeem(good, cid, verifier, code)).status_code == 200  # the code was not spent


@pytest.fixture
def down(rt):
    from exchange_mcp.links.links import Links

    rt.store = dead(rt.sealer)
    rt.guard = Guard(rt.store)
    rt.links = Links(PUBLIC, rt.sealer, rt.store)
    return rt


async def test_sending_fails_closed_when_the_store_is_down(down, ews, invoke):
    from test_mail import item

    ews.on("GetItem", item()).on("SendItem", ok("SendItem"))
    r = await invoke("send_draft", id="D1", user_confirmed=True)
    assert r.is_error and "cannot count this action" in r.text and "Nothing was sent." in r.text
    assert ews.writes() == []


async def test_reading_goes_on_when_the_store_is_down(down, ews, invoke):
    from test_mail import item

    ews.on("GetItem", item())
    r = await invoke("get_message", id="D1")
    assert not r.is_error, r.text


async def test_an_upload_fails_closed_when_the_store_is_down(down, ews, cred):
    from exchange_mcp.app import build
    from exchange_mcp.auth.credential import person_key

    app, _, _ = build(down.settings, down.http, down.sealer, down.store)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)
    from exchange_mcp.core import context

    link = await context.runtime().links.upload(cred, person_key(cred.login), "D1", "attach", "a.pdf")
    r = await client.put(link["url"].replace(PUBLIC, ""), content=b"%PDF")
    assert r.status_code == 503 and "Nothing was saved" in r.text
    assert ews.ops() == []


async def test_readiness_follows_the_key_not_the_store(ews):
    """A store outage must not take every pod out of the Service: reading needs no store, and sign-in and
    sending refuse on their own. Readiness reports the store and stays ready."""
    sealer = LocalSealer([("k1", os.urandom(32))])
    up = Flow(ews, sealer=sealer, store=memory_store(sealer))
    r = await up.client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ok": True, "key": True, "store": True, "state": "memory"}
    gone = Flow(ews, sealer=sealer, store=dead(sealer))
    r = await gone.client.get("/readyz")
    assert r.status_code == 200 and r.json()["store"] is False and r.json()["key"] is True
    assert (await gone.client.get("/healthz")).status_code == 200


async def test_the_metrics_show_the_store_down(ews):
    from exchange_mcp import metrics

    sealer = LocalSealer([("k1", os.urandom(32))])
    gone = Flow(ews, sealer=sealer, store=dead(sealer))
    await gone.client.get("/readyz")
    assert metrics.value("mcp_state_store_up") == 0
    assert metrics.value("mcp_state_store_errors_total", op="ping") >= 1


async def test_readiness_refuses_when_the_key_does_not_work(ews, tmp_path):
    """A secrets manager that does not answer: nothing can be sealed, so the pod takes no traffic."""
    from exchange_mcp.keys.seal import VaultSealer
    from exchange_mcp.keys.vault import Transit

    token = tmp_path / "token"
    token.write_text("t")
    transit = Transit(httpx.AsyncClient(timeout=1), "https://127.0.0.1:1", "k", token)
    f = Flow(ews, sealer=VaultSealer(transit), store=memory_store(LocalSealer([("k1", os.urandom(32))])))
    r = await f.client.get("/readyz")
    assert r.status_code == 503 and r.json()["key"] is False
    assert (await f.client.get("/healthz")).status_code == 200


async def test_the_metrics_port_answers_probes_and_scrapes(ews):
    from exchange_mcp import __version__, metrics
    from exchange_mcp.core import context

    sealer = LocalSealer([("k1", os.urandom(32))])
    Flow(ews, sealer=sealer, store=memory_store(sealer))
    side = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=metrics.metrics_app(context.runtime().health)), base_url="http://m"
    )
    r = await side.get("/metrics")
    assert r.status_code == 200
    assert f'mcp_build_info{{server="exchange-mcp",version="{__version__}"}} 1.0' in r.text
    assert (await side.get("/healthz")).status_code == 200
    assert (await side.get("/readyz")).json()["key"] is True
    assert (await side.get("/mcp")).status_code == 404  # nothing of the public app here


async def test_the_network_limit_uses_the_client_behind_trusted_proxies_only(ews):
    """Behind an ingress every request comes from the controller. Its X-Forwarded-For is believed when the
    operator named it as a trusted proxy; then each client network has its own limit."""
    ews.on("ResolveNames", 401)
    sealer = LocalSealer([("k1", os.urandom(32))])
    f = Flow(ews, sealer=sealer, store=memory_store(sealer), TRUSTED_PROXIES="10.1.2.0/24", ATTEMPTS_PER_NETWORK="2")
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    office = {"X-Forwarded-For": "203.0.113.5"}
    assert (await f.submit(rid, "u1", "wrong", **office)).status_code == 400
    assert (await f.submit(rid, "u2", "wrong", **office)).status_code == 400
    assert (await f.submit(rid, "u3", "wrong", **office)).status_code == 429  # this network is at its limit
    assert (await f.submit(rid, "u4", "wrong", **{"X-Forwarded-For": "198.51.100.7"})).status_code == 400


async def test_without_trusted_proxies_there_is_no_shared_network_bucket(ews):
    """Unset, the peer may be a proxy in front of everybody: a per-network limit would lock out the whole
    organisation after a few failures, so only the per-login limit applies."""
    ews.on("ResolveNames", 401)
    sealer = LocalSealer([("k1", os.urandom(32))])
    f = Flow(ews, sealer=sealer, store=memory_store(sealer), ATTEMPTS_PER_NETWORK="2")
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    for login in ("u1", "u2", "u3", "u4"):
        assert (await f.submit(rid, login, "wrong")).status_code == 400
