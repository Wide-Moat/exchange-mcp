"""Sign-in attempt limits, per login and per client network, over a sliding 15 minutes.

Kept in this replica's memory: with N replicas behind a balancer that does not pin clients, a login gets
at most N times the limit. Set the limit so that N times it stays below the directory's lockout
threshold (docs/auth-modes.md).
"""

from __future__ import annotations

import hashlib
import ipaddress
import time
from collections import deque
from collections.abc import Callable

WINDOW = 15 * 60
MAX_KEYS = 100_000


def network_of(address: str | None) -> str:
    """IPv4 per address, IPv6 per /64: one client cannot spread attempts across its own prefix."""
    if not address:
        return "unknown"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class Limited(Exception):
    pass


class Limiter:
    def __init__(self, per_login: int, per_network: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._limits = {"l": per_login, "n": per_network}
        self._clock = clock
        self._seen: dict[str, deque[float]] = {}

    def _key(self, kind: str, value: str) -> str:
        return kind + hashlib.sha256(value.encode()).hexdigest()[:24]  # no login kept in clear

    def take(self, *, login: str, network: str) -> None:
        """Count one attempt; raise Limited when either limit is already reached (nothing is counted then)."""
        now = self._clock()
        keys = {"l": self._key("l", login), "n": self._key("n", network)}
        for kind, key in keys.items():
            q = self._seen.get(key)
            if q is not None:
                while q and q[0] <= now - WINDOW:
                    q.popleft()
                if len(q) >= self._limits[kind]:
                    raise Limited
        new = sum(1 for key in keys.values() if key not in self._seen)
        if len(self._seen) + new > MAX_KEYS:
            for k in [k for k, q in self._seen.items() if not q or q[-1] <= now - WINDOW]:
                del self._seen[k]
            new = sum(1 for key in keys.values() if key not in self._seen)
            if len(self._seen) + new > MAX_KEYS:
                raise Limited  # a flood of distinct logins or networks: refuse rather than grow without bound
        for key in keys.values():
            self._seen.setdefault(key, deque()).append(now)
