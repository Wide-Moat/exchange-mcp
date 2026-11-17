"""Logins as people type them, turned into the one form this Exchange accepts (docs/auth-modes.md).

`name`, `DOMAIN\\name` and `name@domain` all become the configured form, so a person has one identity
whichever way they typed it. A login of another domain is refused here, before any attempt reaches the
directory, so it cannot count against someone else's lockout.
"""

from __future__ import annotations

import re

from exchange_mcp.config import LoginFormat

MAX_LOGIN = 256
_NAME = re.compile(r"^[^\s\\/@\"\[\]:;|=,+*?<>]{1,104}$")  # sAMAccountName / UPN prefix characters


class BadLogin(ValueError):
    """Not a login of this organisation. The message is generic on purpose."""


def normalise(typed: str, fmt: LoginFormat, domain: str) -> str:
    typed = typed.strip()
    if not typed or len(typed) > MAX_LOGIN:
        raise BadLogin
    domain_l = domain.lower()
    netbios = domain_l.split(".")[0]
    if "\\" in typed:
        given, _, name = typed.partition("\\")
        if given.lower() not in (domain_l, netbios):
            raise BadLogin
    elif "@" in typed:
        name, _, given = typed.rpartition("@")
        if given.lower() != domain_l and not (fmt == "down_level" and given.lower().split(".")[0] == domain_l):
            raise BadLogin
    else:
        name = typed
    if not _NAME.match(name):
        raise BadLogin
    name = name.lower()
    return f"{name}@{domain_l}" if fmt == "upn" else f"{domain.upper()}\\{name}"
