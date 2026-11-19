"""Sign-in attempt limits, per login and per client network, over a sliding 15 minutes.

The windows live in the shared state store (`state.store`), so every replica counts against the same
limit: N failed attempts spread over any number of pods are limited at the configured total. Neither the
login nor the network reaches the store in clear. A store that does not answer raises
`StoreUnavailable`, and the sign-in page fails closed with "try again shortly".
"""

from __future__ import annotations

import ipaddress

from exchange_mcp.state.store import StateStore, StoreFull

WINDOW = 15 * 60


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
    def __init__(self, store: StateStore, per_login: int, per_network: int) -> None:
        self._store = store
        self._per_login = per_login
        self._per_network = per_network

    async def take(self, *, login: str, network: str | None) -> None:
        """Count one attempt; raise Limited when either limit is already reached (nothing is counted then).
        `network` None: the client's network is not known, and only the login is counted."""
        limits = [(f"login\n{login}", self._per_login)]
        if network is not None:
            limits.append((f"network\n{network}", self._per_network))
        try:
            taken = await self._store.take_window("signin", limits, WINDOW)
        except StoreFull:
            raise Limited from None  # a flood of distinct logins or networks: refuse rather than grow
        if not taken:
            raise Limited
