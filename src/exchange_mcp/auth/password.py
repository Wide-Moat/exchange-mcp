"""`password` mode: check a login and password against EWS once, and learn who the person is.

The check is one read (`ResolveNames` for the person's own name), sent with NTLM: a wrong password,
an unknown login and a disabled account all come back as 401 and look the same to the person.
Which of the candidates is the person is decided by the alias or the address matching the login;
docs/live-findings.md records what a real Exchange returned.
"""

from __future__ import annotations

import httpx

from exchange_mcp.auth.credential import Credential
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.soap import M, T, find, findall, text


class SignInFailed(Exception):
    """Login or password not accepted. Deliberately carries nothing."""


def name_part(login: str) -> str:
    return login.split("\\", 1)[1] if "\\" in login else login.split("@", 1)[0]


async def check(http: httpx.AsyncClient, ews_url: str, login: str, password: str) -> Credential:
    probe = Credential("password", login, "", "", password=password)
    ews = Ews(http, ews_url, probe.auth())
    me = name_part(login)
    try:
        messages = await ews.call(
            M.ResolveNames(
                M.UnresolvedEntry(me),
                ReturnFullContactData="true",
                SearchScope="ActiveDirectory",
                ContactDataShape="Default",
            )
        )
    except ExchangeError as e:
        if e.key == "reconnect":
            raise SignInFailed from None
        if e.key in ("failed",) and e.params.get("code") == "ErrorNameResolutionNoResults":
            raise SignInFailed from None
        raise
    candidates = []
    for m in messages:
        for res in findall(m, "m:ResolutionSet/t:Resolution"):
            address = text(res, "t:Mailbox/t:EmailAddress")
            alias = text(res, "t:Contact/t:Alias")
            name = text(res, "t:Mailbox/t:Name") or text(res, "t:Contact/t:DisplayName")
            if address:
                candidates.append((address, alias, name))
    login_l = login.lower()
    mine = [
        c
        for c in candidates
        if c[1].lower() == me.lower() or c[0].lower() == login_l or c[0].split("@")[0].lower() == me.lower()
    ]
    if len(mine) != 1:
        # Authenticated, but the directory does not say which mailbox is this login's. Refuse rather than
        # guess: a wrong guess would show someone else's name and address as "you".
        raise ExchangeError(
            "unsupported",
            reason="Signed in, but Exchange did not identify your mailbox by your login. Ask the operator to check "
            "the login format setting.",
        )
    address, _, name = mine[0]
    return Credential("password", login, address, name or address, password=password)


__all__ = ["SignInFailed", "check", "find", "name_part", "T"]
