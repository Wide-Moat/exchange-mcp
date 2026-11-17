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


def normalise(typed: str, fmt: LoginFormat, domain: str, also: frozenset[str] = frozenset()) -> str:
    """`also` names the organisation's other spelling, exactly: the UPN suffix in down_level mode, the
    NetBIOS name in upn mode (the first label of the UPN suffix is accepted there as well)."""
    typed = typed.strip()
    if not typed or len(typed) > MAX_LOGIN:
        raise BadLogin
    domain_l = domain.lower()
    if "\\" in typed:
        given, _, name = typed.partition("\\")
        accepted = {domain_l} if fmt == "down_level" else {domain_l.split(".")[0], *also}
        if given.lower() not in accepted:
            raise BadLogin
    elif "@" in typed:
        name, _, given = typed.rpartition("@")
        accepted = {domain_l} if fmt == "upn" else set(also)
        if given.lower() not in accepted:
            raise BadLogin
    else:
        name = typed
    if not _NAME.match(name):
        raise BadLogin
    name = name.lower()
    return f"{name}@{domain_l}" if fmt == "upn" else f"{domain.upper()}\\{name}"
