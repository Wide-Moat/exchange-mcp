"""The shared state store: one interface, two implementations.

`MemoryStore` keeps the state in this process (one replica, tests). `RedisStore` (`state.remote`) keeps it
in Redis or Valkey, standalone or behind Sentinel, so any number of replicas share it. Both pass the same
contract suite (tests/unit/test_state_contract.py).

The store holds four things, each under a namespace:

- `once(ns, ident, ttl)`: True the first time within `ttl`. Used authorization codes, upload marks.
- `release(ns, ident)`: forgets a mark set by `once`. An upload finished.
- `take_window(ns, limits, window)`: a sliding window over several keys at once, the sign-in attempts per
  login and per network. Counts one attempt in every key, unless one is already at its limit; then
  nothing is counted.
- `take_token(ns, ident, capacity, refill)`: a token bucket, the send allowance per person.

No identifier is kept in clear: every key is `<prefix><ns>:<HMAC-SHA256(sub-key, ns, ident)>`, where the
sub-key is derived from the sealing key and used for nothing else (`Namer`). A login, an address or an
address of a client network never reaches the store.

Failures are explicit. `StoreUnavailable` means the store could not answer: callers fail closed (sign-in
and sending refuse with "try again shortly"). `StoreFull` means the in-process store reached its bound:
it refuses rather than grows, and never forgets a live entry.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from exchange_mcp import metrics

KEY_LABEL = b"exchange-mcp state key v1"

ONCE_MAX = 20_000  # used codes and upload marks held at once by one process
WINDOW_MAX = 100_000  # sign-in windows (logins plus networks)
BUCKET_MAX = 50_000  # people with a partly spent send bucket


class StoreUnavailable(Exception):
    """The store did not answer. Carries the operation name only."""

    def __init__(self, op: str) -> None:
        super().__init__(op)
        self.op = op


class StoreFull(Exception):
    """The in-process store is at its bound and refuses to grow."""


class Namer:
    """Turns (namespace, identifier) into store keys that name no one.

    The HMAC keys are derived from the sealing keys, once, through `derive` (`Sealer.derive(KEY_LABEL)`:
    one per key that opens, the sealing key first). Domain separation: the label is used for nothing
    else, and the namespace is inside the MAC, so one identifier in two namespaces gives two unrelated
    keys.

    During a rotation replicas hold different key rings ([old], then [old, new] or [new, old], then
    [new]). A mark that must hold across replicas (a redeemed code) is therefore set and checked under
    every key of the ring (`every`): any two rings of one rotation share a key, so a code redeemed on
    one replica is seen by every other. Windows and buckets use the sealing key's name (`__call__`);
    while the sealing key itself changes between rollouts they are counted twice over, for minutes."""

    def __init__(self, derive: Callable[[bytes], Awaitable[list[bytes]]], prefix: str) -> None:
        self._derive = derive
        self._keys: list[bytes] | None = None
        self.prefix = prefix

    async def every(self, ns: str, ident: str) -> list[str]:
        if self._keys is None:
            try:
                keys = await self._derive(KEY_LABEL)
            except Exception:  # noqa: BLE001 - a secrets manager that fails leaves nothing to name with
                raise StoreUnavailable("derive") from None
            if not keys:
                raise StoreUnavailable("derive")
            self._keys = keys
        message = f"{ns}\n{ident}".encode()
        return [f"{self.prefix}{ns}:{hmac.new(k, message, hashlib.sha256).hexdigest()[:40]}" for k in self._keys]

    async def __call__(self, ns: str, ident: str) -> str:
        return (await self.every(ns, ident))[0]


class StateStore(Protocol):
    kind: str  # "memory" or "redis"

    async def once(self, ns: str, ident: str, ttl: float) -> bool: ...

    async def release(self, ns: str, ident: str) -> None: ...

    async def take_window(self, ns: str, limits: Sequence[tuple[str, int]], window: float) -> bool: ...

    async def take_token(self, ns: str, ident: str, capacity: float, refill_per_second: float) -> tuple[bool, float]:
        """(taken, tokens): the tokens left after taking one, or the tokens there are when none was taken."""
        ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class Measured:
    """Times every operation and keeps `mcp_state_store_up` and the error counter current."""

    async def _measure(self, op: str, run: Callable[[], Awaitable]):
        started = time.perf_counter()
        try:
            result = await run()
        except StoreUnavailable:
            metrics.STORE_ERRORS.labels(metrics.SERVER, op).inc()
            metrics.STORE_UP.labels(metrics.SERVER).set(0)
            raise
        finally:
            metrics.STORE_DURATION.labels(metrics.SERVER, op).observe(time.perf_counter() - started)
        metrics.STORE_UP.labels(metrics.SERVER).set(1)
        return result


class MemoryStore(Measured):
    """The state in this process. Correct for exactly one replica; the chart refuses more without a store."""

    kind = "memory"

    def __init__(self, namer: Namer, clock: Callable[[], float] = time.monotonic) -> None:
        self._name = namer
        self._clock = clock
        self._once: dict[str, float] = {}
        self._windows: dict[str, deque[float]] = {}
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, at)

    async def once(self, ns: str, ident: str, ttl: float) -> bool:
        keys = await self._name.every(ns, ident)
        return await self._measure("once", lambda: self._once_now(keys, ttl))

    async def _once_now(self, keys: list[str], ttl: float) -> bool:
        now = self._clock()
        if any(self._once.get(k, 0) > now for k in keys):
            return False
        new = [k for k in keys if k not in self._once]
        if len(self._once) + len(new) > ONCE_MAX:
            for k in [k for k, e in self._once.items() if e <= now]:
                del self._once[k]
            if len(self._once) + len(new) > ONCE_MAX:
                # Forgetting a live mark would let a code be redeemed twice: refuse instead.
                raise StoreFull
        for k in keys:
            self._once[k] = now + ttl
        return True

    async def release(self, ns: str, ident: str) -> None:
        keys = await self._name.every(ns, ident)

        async def run() -> None:
            for k in keys:
                self._once.pop(k, None)

        await self._measure("release", run)

    async def take_window(self, ns: str, limits: Sequence[tuple[str, int]], window: float) -> bool:
        keyed = [(await self._name(ns, ident), limit) for ident, limit in limits]
        return await self._measure("take_window", lambda: self._window_now(keyed, window))

    async def _window_now(self, keyed: list[tuple[str, int]], window: float) -> bool:
        now = self._clock()
        for key, limit in keyed:
            q = self._windows.get(key)
            if q is not None:
                while q and q[0] <= now - window:
                    q.popleft()
                if len(q) >= limit:
                    return False
        new = sum(1 for key, _ in keyed if key not in self._windows)
        if len(self._windows) + new > WINDOW_MAX:
            for k in [k for k, q in self._windows.items() if not q or q[-1] <= now - window]:
                del self._windows[k]
            new = sum(1 for key, _ in keyed if key not in self._windows)
            if len(self._windows) + new > WINDOW_MAX:
                raise StoreFull  # a flood of distinct logins or networks: refuse rather than grow
        for key, _ in keyed:
            self._windows.setdefault(key, deque()).append(now)
        return True

    async def take_token(self, ns: str, ident: str, capacity: float, refill_per_second: float) -> tuple[bool, float]:
        key = await self._name(ns, ident)
        return await self._measure("take_token", lambda: self._token_now(key, capacity, refill_per_second))

    async def _token_now(self, key: str, capacity: float, rate: float) -> tuple[bool, float]:
        now = self._clock()
        if key not in self._buckets and len(self._buckets) >= BUCKET_MAX:
            # A full bucket is the same as no bucket: forgetting it changes nothing. A partly spent one is
            # kept, and a newcomer waits rather than the table growing without bound.
            for k in [k for k, (t, at) in self._buckets.items() if t + (now - at) * rate >= capacity]:
                del self._buckets[k]
            if len(self._buckets) >= BUCKET_MAX:
                raise StoreFull
        tokens, at = self._buckets.get(key, (capacity, now))
        tokens = min(capacity, tokens + max(0.0, now - at) * rate)
        taken = tokens >= 1
        if taken:
            tokens -= 1
        self._buckets[key] = (tokens, now)
        return taken, tokens

    async def ping(self) -> bool:
        async def run() -> bool:
            return True

        return await self._measure("ping", run)

    async def close(self) -> None:
        return None


def wait_seconds(tokens: float, refill_per_second: float) -> int:
    """Seconds until one token is back."""
    return max(1, math.ceil((1 - tokens) / refill_per_second))
