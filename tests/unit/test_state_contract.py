"""One contract for every state store: the in-process one, Valkey standalone with a password, Valkey
behind Sentinel, Redis standalone and Redis behind a single Sentinel host, the last three without a
password.

The remote stores are real servers, never a fake: `scripts/stores-test.sh up` starts them and prints the
variables naming them; CI does the same before the suite. Without those variables the remote cases are
skipped, and CI fails a run in which any test was skipped.

    eval "$(scripts/stores-test.sh up)" && pytest tests/unit/test_state_contract.py

"Two replicas" below are two store objects: for a remote store, two clients with connections of their
own to the same server; for the in-process store, the one object two callers in one process share.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time

import pytest
from conftest import base_env

from exchange_mcp import metrics
from exchange_mcp.config import Settings
from exchange_mcp.keys.seal import LocalSealer
from exchange_mcp.state.remote import RedisStore, connect
from exchange_mcp.state.store import MemoryStore, Namer, StoreUnavailable

SEALER = LocalSealer([("k1", b"s" * 32)])
BACKENDS = ["memory", "valkey", "valkey-sentinel", "redis", "redis-sentinel"]


def _password_file(tmp_path) -> str:
    path = tmp_path / "state-password"
    path.write_text(os.environ["VALKEY_TEST_PASSWORD"] + "\n")
    path.chmod(0o400)
    return str(path)


def settings_for(backend: str, tmp_path, prefix: str) -> Settings:
    product, _, mode = backend.partition("-")
    name = f"{product.upper()}_TEST_{'SENTINELS' if mode else 'URL'}"
    where = os.environ.get(name)
    if not where:
        pytest.skip(f"{name} not set: no {backend} to test against (scripts/stores-test.sh up)")
    if mode:
        # The platform's shape: Sentinel, no password anywhere; one host is enough.
        return Settings.from_env(base_env(STATE_SENTINELS=where, STATE_SENTINEL_MASTER="mymaster", STATE_PREFIX=prefix))
    extra = {"STATE_URL": where, "STATE_PREFIX": prefix}
    if product == "valkey" and os.environ.get("VALKEY_TEST_PASSWORD"):
        extra["STATE_PASSWORD_FILE"] = _password_file(tmp_path)
    return Settings.from_env(base_env(**extra))


@pytest.fixture(params=BACKENDS)
async def pair(request, tmp_path):
    """Two replicas' stores over one backend, under a prefix of this test's own."""
    prefix = f"t{secrets.token_hex(4)}:"
    if request.param == "memory":
        one = MemoryStore(Namer(SEALER.derive, prefix))
        yield one, one
        return
    settings = settings_for(request.param, tmp_path, prefix)
    a = connect(settings, Namer(SEALER.derive, prefix))
    b = connect(settings, Namer(SEALER.derive, prefix))
    try:
        yield a, b
    finally:
        await a.close()
        await b.close()


async def test_a_mark_is_set_once_across_replicas(pair):
    a, b = pair
    assert await a.once("code", "c1", 30) is True
    assert await b.once("code", "c1", 30) is False
    assert await a.once("code", "c1", 30) is False
    assert await b.once("code", "c2", 30) is True  # another identifier
    assert await b.once("upload", "c1", 30) is True  # another namespace


async def test_a_mark_ends_with_its_lifetime_or_its_release(pair):
    a, b = pair
    assert await a.once("upload", "u1", 0.3) is True
    assert await b.once("upload", "u1", 0.3) is False
    await asyncio.sleep(0.5)
    assert await b.once("upload", "u1", 30) is True
    await a.release("upload", "u1")
    assert await b.once("upload", "u1", 30) is True


async def test_concurrent_marks_admit_exactly_one(pair):
    a, b = pair
    got = await asyncio.gather(*(s.once("code", "race", 30) for s in [a, b] * 10))
    assert got.count(True) == 1


async def test_the_window_is_shared_and_counts_nothing_when_refused(pair):
    a, b = pair
    limits = [("login\nalex", 3), ("network\n10.0.0.1", 10)]
    assert [await s.take_window("signin", limits, 60) for s in (a, b, a)] == [True, True, True]
    assert await b.take_window("signin", limits, 60) is False  # the login is at 3, whichever replica asks
    # The refused attempt was not counted against the network: seven more fit under its limit of 10.
    for i in range(7):
        assert await (a, b)[i % 2].take_window("signin", [(f"login\nu{i}", 3), ("network\n10.0.0.1", 10)], 60)
    assert await a.take_window("signin", [("login\nfresh", 3), ("network\n10.0.0.1", 10)], 60) is False


async def test_the_window_slides(pair):
    a, b = pair
    limits = [("login\nslide", 2)]
    assert await a.take_window("signin", limits, 0.5) and await b.take_window("signin", limits, 0.5)
    assert await a.take_window("signin", limits, 0.5) is False
    await asyncio.sleep(0.7)
    assert await b.take_window("signin", limits, 0.5) is True


async def test_concurrent_attempts_never_pass_the_limit(pair):
    a, b = pair
    got = await asyncio.gather(*(s.take_window("signin", [("login\nrace", 5)], 60) for s in [a, b] * 15))
    assert got.count(True) == 5


async def test_the_bucket_is_shared_and_refills(pair):
    a, b = pair
    taken = [await s.take_token("send", "person", 3, 2.0) for s in (a, b, a)]
    assert [t for t, _ in taken] == [True, True, True]
    assert [round(left) for _, left in taken] == [2, 1, 0]
    ok, left = await b.take_token("send", "person", 3, 2.0)
    assert ok is False and left < 1
    assert (await a.take_token("send", "another", 3, 2.0))[0] is True  # one bucket per person
    await asyncio.sleep(0.6)  # 2 tokens a second: at least one is back
    assert (await b.take_token("send", "person", 3, 2.0))[0] is True


async def test_concurrent_sends_never_pass_the_capacity(pair):
    a, b = pair
    got = await asyncio.gather(*(s.take_token("send", "race", 4, 0.001) for s in [a, b] * 10))
    assert [t for t, _ in got].count(True) == 4


async def test_no_identifier_reaches_the_store_in_clear(pair):
    a, _ = pair
    login, network, person = "alex@corp.example", "10.9.8.7", "person-key-abc"
    await a.once("code", "sealed-code-value", 30)
    await a.take_window("signin", [(f"login\n{login}", 5), (f"network\n{network}", 20)], 60)
    await a.take_token("send", person, 10, 1.0)
    if isinstance(a, MemoryStore):
        keys = [*a._once, *a._windows, *a._buckets]  # noqa: SLF001
    else:
        keys = [k.decode() async for k in a._r.scan_iter(match=f"{a._name.prefix}*")]  # noqa: SLF001
    assert len(keys) == 4, keys  # the code, two windows, one bucket, and nothing else
    if not isinstance(a, MemoryStore):
        for k in keys:  # nothing lives forever: a store that fills up is never filled by us
            assert 0 < await a._r.pttl(k) <= 3_600_000, k  # noqa: SLF001
    for k in keys:
        assert k.startswith(a._name.prefix)  # noqa: SLF001 - every key namespaced
        for clear in (login, "alex", network, person, "sealed-code-value"):
            assert clear not in k


async def test_the_store_answers_a_ping(pair):
    a, b = pair
    assert await a.ping() and await b.ping()
    assert metrics.value("mcp_state_store_up") == 1


async def test_a_store_that_does_not_answer_fails_fast_and_says_so():
    """Nothing listens on the port: every operation raises StoreUnavailable within the one-second timeouts,
    the ping says False, and the metrics show the store down."""
    settings = Settings.from_env(base_env(STATE_URL="redis://127.0.0.1:1/0"))
    store = connect(settings, Namer(SEALER.derive, "x:"))
    assert isinstance(store, RedisStore)
    before = metrics.value("mcp_state_store_errors_total", op="once")
    started = time.monotonic()
    for op in (
        store.once("code", "c", 30),
        store.take_window("signin", [("login\na", 5)], 60),
        store.take_token("send", "p", 10, 1.0),
        store.release("upload", "u"),
    ):
        with pytest.raises(StoreUnavailable):
            await op
    assert await store.ping() is False
    assert time.monotonic() - started < 10
    assert metrics.value("mcp_state_store_up") == 0
    assert metrics.value("mcp_state_store_errors_total", op="once") == before + 1
    await store.close()


async def test_a_full_store_refuses_and_the_server_fails_closed(tmp_path):
    """A store out of memory under `noeviction` refuses writes. That must read as "unavailable" (sign-in and
    sending refuse), never as "nothing counted yet" (fail open)."""
    prefix = f"t{secrets.token_hex(4)}:"
    store = connect(settings_for("redis", tmp_path, prefix), Namer(SEALER.derive, prefix))
    await store._r.config_set("maxmemory", "1")  # noqa: SLF001 - every write is now over the limit
    try:
        for op in (
            store.once("code", "c", 30),
            store.take_window("signin", [("login\na", 5)], 60),
            store.take_token("send", "p", 10, 1.0),
        ):
            with pytest.raises(StoreUnavailable):
                await op
    finally:
        await store._r.config_set("maxmemory", "0")  # noqa: SLF001
        await store.close()
