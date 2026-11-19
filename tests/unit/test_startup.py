"""What a fresh process says and measures before anyone uses it.

- Each uvicorn start and stop line is written once. 0.2.0 wrote every one twice with the same pid: the
  metrics port runs a second uvicorn server, and both log through the one `uvicorn.error` logger.
- `mcp_state_store_up` has a sample before the first request. 0.2.0 set it only on a store operation, so a
  fresh pod exposed HELP and TYPE alone and an alert on the store could not see one never reached.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
import time
from collections import Counter

import httpx
import prometheus_client
from conftest import base_env
from test_logs_json import _env, _ports, lines_of

from exchange_mcp import app as app_module
from exchange_mcp import metrics
from exchange_mcp.state.store import StoreUnavailable

ONCE = ("started_server_process", "waiting_for_application_startup", "application_startup_complete")
SAMPLE = 'mcp_state_store_up{server="exchange-mcp"} 1.0'


def test_a_fresh_server_writes_each_start_line_once_and_measures_the_store_before_any_request():
    port, metrics_port = _ports(2)
    env = _env(
        **base_env(LISTEN="127.0.0.1", METRICS_PORT=str(metrics_port), PUBLIC_URL=f"http://localhost:{port}"),
        PORT=str(port),
    )
    proc = subprocess.Popen(  # noqa: S603 - the server under test
        [sys.executable, "-m", "exchange_mcp.app"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        scraped = ""
        for _ in range(100):  # the first request this process serves is this scrape: no /readyz, no sign-in
            try:
                scraped = httpx.get(f"http://127.0.0.1:{metrics_port}/metrics", timeout=1).text
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError("the metrics port did not answer")
        assert "# TYPE mcp_state_store_up gauge" in scraped  # the scrape itself worked
        assert SAMPLE in scraped, [line for line in scraped.splitlines() if "store_up" in line]
    finally:
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
    records = lines_of(out + err)
    uvicorn_lines = [(r["event"], r["msg"]) for r in records if r["logger"] == "uvicorn.error"]
    counts = Counter(uvicorn_lines)
    for event in ONCE + ("finished_server_process",):
        assert sum(n for (e, _), n in counts.items() if e == event) == 1, (event, uvicorn_lines)
    assert [line for line, n in counts.items() if n > 1] == [], uvicorn_lines


def test_the_metrics_server_keeps_its_warnings():
    """Only its INFO lines go: a failure of the metrics port must still be seen."""
    f = app_module.SideLines()

    def record(level: int) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.error", level, __file__, 1, "x", (), None)

    async def inside_side() -> tuple[bool, bool]:
        app_module._SIDE.set(True)
        return f.filter(record(logging.INFO)), f.filter(record(logging.ERROR))

    assert asyncio.run(inside_side()) == (False, True)
    assert f.filter(record(logging.INFO)) is True  # the public server's own lines pass
    app_module.quiet_side_lines()
    app_module.quiet_side_lines()
    assert sum(isinstance(x, app_module.SideLines) for x in logging.getLogger("uvicorn.error").filters) == 1


class _Store:
    kind = "redis"

    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if a == "hang":
            await asyncio.sleep(60)
        if isinstance(a, BaseException):
            raise a
        metrics.STORE_UP.labels(metrics.SERVER).set(1 if a else 0)
        return a

    async def close(self) -> None:
        return None


def _gauge() -> float | None:
    return prometheus_client.REGISTRY.get_sample_value("mcp_state_store_up", {"server": metrics.SERVER})


async def test_the_lifespan_pings_the_store_before_serving(rt, monkeypatch):
    from exchange_mcp.app import build

    metrics.STORE_UP.clear()
    assert _gauge() is None  # the state 0.2.0 was left in until a store operation
    store = _Store([True])
    app, _, _ = build(rt.settings, rt.http, rt.sealer, store)
    async with app.router.lifespan_context(app):
        assert _gauge() == 1.0 and store.pings == 1
    assert store.pings == 1  # the watcher stopped with the app


async def test_a_store_never_reached_is_reported_down_from_the_start(rt):
    from exchange_mcp.app import build

    metrics.STORE_UP.clear()
    app, _, _ = build(rt.settings, rt.http, rt.sealer, _Store([StoreUnavailable("ping")]))
    async with app.router.lifespan_context(app):
        assert _gauge() == 0.0


async def test_a_hanging_store_is_reported_down_within_the_ping_timeout(monkeypatch):
    monkeypatch.setattr(app_module, "STORE_PING_TIMEOUT", 0.05)
    metrics.STORE_UP.labels(metrics.SERVER).set(1)
    assert await app_module.ping_store(_Store(["hang"])) is False
    assert _gauge() == 0.0


async def test_the_store_is_pinged_again_and_again(monkeypatch):
    store = _Store([True, False, True])
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) > 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    seen = []
    real = app_module.ping_store

    async def ping(s):
        seen.append(await real(s))
        return seen[-1]

    monkeypatch.setattr(app_module, "ping_store", ping)
    try:
        await app_module.watch_store(store, sleep=sleep)
    except asyncio.CancelledError:
        pass
    assert waits == [app_module.STORE_PING_SECONDS] * 4
    assert seen == [True, False, True]  # down and back up again, each seen within one period
    assert app_module.STORE_PING_SECONDS <= 15


async def test_readiness_does_not_follow_the_store(rt):
    from exchange_mcp.app import build

    app, _, _ = build(rt.settings, rt.http, rt.sealer, _Store([False]))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=rt.settings.public_url)
    r = await client.get("/readyz", headers={"Host": "mcp.example.com"})
    assert r.status_code == 200 and r.json()["store"] is False
