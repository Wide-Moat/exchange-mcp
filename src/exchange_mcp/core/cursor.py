"""Opaque cursors: the EWS paging position, bound to one tool, one person and the arguments that chose the
list, sealed so it cannot be read, replayed in another list or edited to point elsewhere."""

from __future__ import annotations

from typing import Any

from exchange_mcp.core.errors import invalid
from exchange_mcp.keys.seal import Sealer

CURSOR_TTL = 24 * 3600


class Cursors:
    def __init__(self, sealer: Sealer) -> None:
        self._sealer = sealer

    async def make(self, tool: str, person: str, state: dict[str, Any], scope: dict[str, Any] | None = None) -> str:
        return await self._sealer.seal("cursor", {"t": tool, "p": person, "q": scope or {}, "s": state}, CURSOR_TTL)

    async def read(self, tool: str, person: str, cursor: str, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        body = await self._sealer.open("cursor", cursor)
        if body is None:
            raise invalid("This cursor is damaged or expired: pass it exactly as it came, or start the list again.")
        if body.get("t") != tool or body.get("p") != person or body.get("q") != (scope or {}):
            raise invalid("This cursor does not belong to this list. Start the list again without a cursor.")
        state = body.get("s")
        if not isinstance(state, dict):
            raise invalid("This cursor is damaged: start the list again.")
        return state
