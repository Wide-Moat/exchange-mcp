"""The state in Redis or Valkey: standalone (`EXCHANGE_MCP_STATE_URL`) or behind Sentinel
(`EXCHANGE_MCP_STATE_SENTINELS`), with or without a password.

Every operation is one atomic command or one Lua script, and the scripts read the store's own clock
(`TIME`), so replicas whose clocks differ still count one window and one bucket. Connect and socket
timeouts are one second: a store that does not answer is reported as `StoreUnavailable` quickly, and
the caller fails closed. Behind Sentinel, redis-py asks the sentinels for the master on every new
connection, so after a failover the next operation reaches the new master.

Cluster mode is not supported: a window's two keys must live on one node.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import Sentinel
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from exchange_mcp.config import ConfigError, Settings
from exchange_mcp.state.store import Measured, MemoryStore, Namer, StateStore, StoreUnavailable

TIMEOUT = 1.0
MAX_PASSWORD_FILE = 4096

# Both scripts start with a shebang: a store over `maxmemory` refuses such a script before it runs. Without
# one, Redis stops refusing writes once a script has written anything, so the window's first trim would
# let its count through under OOM and the limit would fail open (measured, Redis 7.2 and Valkey 8.1).
# A shebang needs Redis 7 or Valkey 7 at least.
#
# KEYS: the window keys. ARGV[1]: window in ms; ARGV[2]: a unique member; ARGV[3..]: the limit of each key.
# Counts nothing when any key is at its limit, as the in-process store does.
WINDOW = """#!lua
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local win = tonumber(ARGV[1])
for i, k in ipairs(KEYS) do
  redis.call('ZREMRANGEBYSCORE', k, '-inf', now - win)
  if redis.call('ZCARD', k) >= tonumber(ARGV[i + 2]) then return 0 end
end
for i, k in ipairs(KEYS) do
  redis.call('ZADD', k, now, ARGV[2])
  redis.call('PEXPIRE', k, win)
end
return 1
"""

# KEYS[1]: the bucket. ARGV[1]: capacity; ARGV[2]: refill per millisecond. Returns {taken, tokens}.
BUCKET = """#!lua
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local v = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = cap
if v[1] then
  tokens = math.min(cap, tonumber(v[1]) + math.max(0, now - tonumber(v[2])) * rate)
end
local taken = 0
if tokens >= 1 then
  tokens = tokens - 1
  taken = 1
end
redis.call('HSET', KEYS[1], 'tokens', string.format('%.6f', tokens), 'at', now)
redis.call('PEXPIRE', KEYS[1], math.ceil((cap - tokens) / rate) + 1000)
return {taken, string.format('%.6f', tokens)}
"""

_FAILURES = (RedisError, OSError, asyncio.TimeoutError)


class RedisStore(Measured):
    kind = "redis"

    def __init__(self, client: aioredis.Redis, namer: Namer, *, closers: Sequence[Any] = ()) -> None:
        self._r = client
        self._name = namer
        self._closers = closers
        self._window = client.register_script(WINDOW)
        self._bucket = client.register_script(BUCKET)

    async def _call(self, op: str, run):
        async def guarded():
            try:
                return await run()
            except _FAILURES:
                raise StoreUnavailable(op) from None

        return await self._measure(op, guarded)

    async def once(self, ns: str, ident: str, ttl: float) -> bool:
        key = await self._name(ns, ident)
        ms = max(1, int(ttl * 1000))
        return bool(await self._call("once", lambda: self._r.set(key, b"1", nx=True, px=ms)))

    async def release(self, ns: str, ident: str) -> None:
        key = await self._name(ns, ident)
        await self._call("release", lambda: self._r.delete(key))

    async def take_window(self, ns: str, limits: Sequence[tuple[str, int]], window: float) -> bool:
        keys = [await self._name(ns, ident) for ident, _ in limits]
        args = [int(window * 1000), secrets.token_hex(8), *(int(limit) for _, limit in limits)]
        return int(await self._call("take_window", lambda: self._window(keys=keys, args=args))) == 1

    async def take_token(self, ns: str, ident: str, capacity: float, refill_per_second: float) -> tuple[bool, float]:
        key = await self._name(ns, ident)
        args = [repr(float(capacity)), repr(refill_per_second / 1000)]
        taken, tokens = await self._call("take_token", lambda: self._bucket(keys=[key], args=args))
        return int(taken) == 1, float(tokens)

    async def ping(self) -> bool:
        try:
            return bool(await self._call("ping", self._r.ping))
        except StoreUnavailable:
            return False

    async def close(self) -> None:
        for c in (self._r, *self._closers):
            try:
                await c.aclose()
            except _FAILURES:
                pass


def read_password(path: Path, name: str) -> str:
    """A password file: a regular file, owned by this server's user, readable by it alone, small.
    The chart's init container stages it so; a file anyone else may read is refused."""
    try:
        st = path.stat()
    except OSError:
        raise ConfigError(f"{name} cannot be read") from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"{name} is not a regular file")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"{name} is readable by others; it must be 0400 or 0600")
    if st.st_uid != os.geteuid():
        raise ConfigError(f"{name} is not owned by this server's user")
    with path.open("rb") as f:
        raw = f.read(MAX_PASSWORD_FILE + 1)
    if len(raw) > MAX_PASSWORD_FILE:
        raise ConfigError(f"{name} is larger than {MAX_PASSWORD_FILE} bytes; it is not a password file")
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise ConfigError(f"{name} is not text") from None
    if not value:
        raise ConfigError(f"{name} is empty; leave the setting out when there is no password")
    return value


def connect(settings: Settings, namer: Namer) -> RedisStore:
    """A client for the configured store. Nothing connects here: the first operation does."""
    s = settings.state
    assert s is not None
    common: dict[str, Any] = {
        "socket_timeout": TIMEOUT,
        "socket_connect_timeout": TIMEOUT,
        "retry": Retry(NoBackoff(), 1),
        "health_check_interval": 15,
    }
    if s.username:
        common["username"] = s.username
    if s.password_file:
        common["password"] = read_password(s.password_file, "EXCHANGE_MCP_STATE_PASSWORD_FILE")
    tls: dict[str, Any] = {}
    if s.tls:
        tls = {"ssl": True, "ssl_cert_reqs": "required"}
        if settings.ca_bundle:
            tls["ssl_ca_certs"] = str(settings.ca_bundle)
    if s.url:
        client = aioredis.Redis(host=s.host, port=s.port, db=s.db, **common, **tls)
        return RedisStore(client, namer)
    sentinel_kwargs: dict[str, Any] = {
        "socket_timeout": TIMEOUT,
        "socket_connect_timeout": TIMEOUT,
        **tls,
    }
    if s.sentinel_password_file:
        sentinel_kwargs["password"] = read_password(
            s.sentinel_password_file, "EXCHANGE_MCP_STATE_SENTINEL_PASSWORD_FILE"
        )
    sentinel = Sentinel(list(s.sentinels), sentinel_kwargs=sentinel_kwargs, db=s.db, **common, **tls)
    client = sentinel.master_for(s.sentinel_master)
    return RedisStore(client, namer, closers=sentinel.sentinels)


def make_store(settings: Settings, namer: Namer) -> StateStore:
    if settings.state is None:
        return MemoryStore(namer)
    return connect(settings, namer)
