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


class SignInFailed(Exception):
    """Login or password not accepted. Deliberately carries nothing."""


def name_part(login: str) -> str:
    return login.split("\\", 1)[1] if "\\" in login else login.split("@", 1)[0]


async def check(http: httpx.AsyncClient, ews_url: str, login: str, password: str) -> Credential:
    from exchange_mcp.core.resolve import candidates

    probe = Credential("password", login, "", "", password=password)
    ews = Ews(http, ews_url, probe.auth())
    me = name_part(login)
    try:
        found = await candidates(ews, me, scope="ActiveDirectory")
    except ExchangeError as e:
        if e.key == "reconnect":
            raise SignInFailed from None
        raise
    login_l, me_l = login.lower(), me.lower()
    mine = [
        p for p in found if me_l in (p.alias.lower(), p.address.split("@")[0].lower()) or p.address.lower() == login_l
    ]
    if len(mine) != 1:
        # Authenticated, but the directory does not say which mailbox is this login's. Refuse rather than
        # guess: a wrong guess would show someone else's name and address as "you".
        raise ExchangeError(
            "unsupported",
            reason="Signed in, but Exchange did not identify your mailbox by your login. Ask the operator to check "
            "the login format setting.",
        )
    return Credential("password", login, mine[0].address, mine[0].name or mine[0].address, password=password)
