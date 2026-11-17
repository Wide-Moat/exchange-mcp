"""account: who is signed in, how, and what this connector can do. Always on."""

from __future__ import annotations

from typing import Any

from exchange_mcp.core.context import call
from exchange_mcp.ews.soap import M, T, text
from exchange_mcp.registry import AREAS, tool

MODE_SENTENCE = {
    "adfs": "Signed in through AD FS; this connector holds a token that reaches Exchange Web Services only.",
    "password": (
        "Signed in with the directory password, which this connector keeps encrypted inside the connection; "
        "no second factor applies. The organisation can switch to AD FS sign-in when Exchange supports it."
    ),
}


@tool(area="account", title="Connection", read_only=True, idempotent=True)
async def get_connection() -> dict[str, Any]:
    """Who is signed in to Exchange on this connector, how they signed in, which areas are on, and whether
    an automatic reply is running. Call it first when another tool fails for rights or sign-in, or when the
    person asks what the assistant can do with their mailbox. It reads only and changes nothing."""
    async with call("get_connection") as c:
        on = sorted(c.rt.settings.areas)
        oof_state = "unknown"
        if "mail" in c.rt.settings.areas:
            messages = await c.ews.call(M.GetUserOofSettingsRequest(T.Mailbox(T.Address(c.me))))
            oof_state = text(messages[0] if messages else None, ".//t:OofState", "unknown")
        return {
            "name": c.cred.name,
            "address": c.cred.address,
            "sign_in": c.cred.mode,
            "sign_in_note": MODE_SENTENCE[c.cred.mode],
            "time_zone": c.rt.settings.timezone,
            "areas_on": on,
            "areas_off": sorted(set(AREAS) - set(on)),
            "automatic_reply": oof_state.lower(),
        }
