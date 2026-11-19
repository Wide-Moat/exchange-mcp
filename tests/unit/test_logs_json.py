"""Every line every process of the image writes is one JSON object (the gate behind "logs only JSON").

These start real processes, as the container does: the server (serving requests, then stopped), a start
the configuration refuses, the init container's key staging, an uncaught exception and a Python warning.
Each captured line must parse, carry the fixed fields, and the count of lines must be above zero, so a
process that printed nothing cannot pass.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

import httpx
from conftest import base_env

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
FIELDS = {"ts", "level", "logger", "event", "msg"}


def lines_of(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        record = json.loads(line)  # raises on any line that is not JSON
        assert isinstance(record, dict) and FIELDS <= set(record), line
        assert TS.match(record["ts"]), record["ts"]
        assert re.fullmatch(r"[a-z0-9_]{1,64}", record["event"]), record["event"]
        out.append(record)
    return out


def _ports(n: int) -> list[int]:
    """n distinct free ports: held open together while chosen, so two calls cannot return one port."""
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EXCHANGE_MCP_")}
    env.update(extra)
    return env


def test_the_running_server_writes_only_json():
    port, metrics_port = _ports(2)
    env = _env(
        **base_env(LISTEN="127.0.0.1", METRICS_PORT=str(metrics_port), PUBLIC_URL=f"http://localhost:{port}"),
        PORT=str(port),
    )
    proc = subprocess.Popen(  # noqa: S603 - the server under test
        [sys.executable, "-m", "exchange_mcp.app"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError("the server did not start")
        host = {"Host": f"localhost:{port}"}
        assert httpx.get(f"{base}/readyz", headers=host).status_code == 200
        assert httpx.get(f"{base}/signin?request=forged", headers=host).status_code == 410
        assert httpx.get(f"{base}/metrics", headers=host).status_code == 404  # never on the public port
        scraped = httpx.get(f"http://127.0.0.1:{metrics_port}/metrics").text
        assert 'mcp_http_requests_in_flight{route_group="health",server="exchange-mcp"}' in scraped
        assert 'mcp_state_store_up{server="exchange-mcp"} 1.0' in scraped
        assert (
            'mcp_http_requests_total{method="GET",route_group="signin",server="exchange-mcp",status_class="4xx"}'
            in scraped
        )
    finally:
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
    records = lines_of(out + err)
    assert len(records) >= 2, out + err
    events = [r["event"] for r in records]
    assert "state_in_memory" in events and "starting" in events
    assert proc.returncode in (0, -signal.SIGTERM)  # uvicorn re-raises the signal after a clean shutdown


def test_a_refused_start_writes_only_json():
    env = _env(**base_env(PUBLIC_URL="ftp://nowhere"))
    run = subprocess.run([sys.executable, "-m", "exchange_mcp.app"], env=env, capture_output=True, text=True)  # noqa: S603
    records = lines_of(run.stdout + run.stderr)
    assert run.returncode == 2 and len(records) >= 1
    assert records[-1]["event"] == "config_refused" and "PUBLIC_URL" in records[-1]["msg"]


def test_a_key_in_the_environment_is_refused_in_json():
    env = _env(**base_env(SECRET="abcdefabcdefabcdefabcdefabcdefabcdef"))
    run = subprocess.run([sys.executable, "-m", "exchange_mcp.app"], env=env, capture_output=True, text=True)  # noqa: S603
    records = lines_of(run.stdout + run.stderr)
    assert run.returncode == 2 and "never read from the environment" in records[-1]["msg"]
    assert "abcdef" not in run.stdout + run.stderr


def test_key_staging_writes_only_json(tmp_path):
    src, dst = tmp_path / "secret", tmp_path / "run"
    src.mkdir()
    dst.mkdir()
    (src / "key").write_text("k1:" + "A" * 43 + "\n")
    ok = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "exchange_mcp.keys.stage", str(src), str(dst)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    missing = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "exchange_mcp.keys.stage", str(tmp_path / "absent"), str(dst)],
        capture_output=True,
        text=True,
        env=_env(),
    )
    good, bad = lines_of(ok.stdout + ok.stderr), lines_of(missing.stdout + missing.stderr)
    assert ok.returncode == 0 and [r["event"] for r in good] == ["keys_staged"]
    assert missing.returncode == 1 and [r["event"] for r in bad] == ["stage_failed"]
    assert "AAAA" not in ok.stdout + ok.stderr


def test_an_uncaught_exception_is_json_with_its_message_withheld():
    code = (
        "from exchange_mcp.observability import configure\n"
        "configure()\n"
        "def deep():\n"
        "    raise ValueError('quarterly-numbers-detail')\n"
        "deep()\n"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=_env())  # noqa: S603
    records = lines_of(run.stdout + run.stderr)
    assert run.returncode == 1 and len(records) == 1
    assert records[0]["event"] == "uncaught" and records[0]["level"] == "CRITICAL"
    assert "in deep" in records[0]["exc"] and "ValueError" in records[0]["exc"]
    assert "quarterly-numbers-detail" not in run.stdout + run.stderr


def test_a_warning_is_json():
    code = "import warnings\nfrom exchange_mcp.observability import configure\nconfigure()\nwarnings.warn('old way')\n"
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=_env())  # noqa: S603
    records = lines_of(run.stdout + run.stderr)
    assert len(records) == 1 and records[0]["logger"] == "py.warnings"
