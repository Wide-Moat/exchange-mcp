"""What a token carries about the person, and how a request to EWS is authenticated from it.

The credential is a plain object only inside one request. It is sealed into tokens by the OAuth
provider and never logged: `repr` hides the secret parts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import httpx

from exchange_mcp.ews.transport import Bearer, Ntlm


@dataclass(frozen=True)
class Credential:
    mode: str  # "password" | "adfs"
    login: str  # normalised; the person's identity
    address: str  # the primary SMTP address, resolved at sign-in
    name: str  # display name, resolved at sign-in
    password: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)  # adfs: the EWS token
    access_exp: int = 0
    refresh_token: str = field(default="", repr=False)  # adfs: the AD FS refresh token

    def auth(self) -> httpx.Auth:
        return Bearer(self.access_token) if self.mode == "adfs" else Ntlm(self.login, self.password)

    def to_payload(self) -> dict[str, Any]:
        d: dict[str, Any] = {"m": self.mode, "l": self.login, "a": self.address, "n": self.name}
        if self.mode == "password":
            d["pw"] = self.password
        else:
            d.update(at=self.access_token, ax=self.access_exp, rt=self.refresh_token)
        return d

    @classmethod
    def from_payload(cls, d: dict[str, Any]) -> Credential | None:
        try:
            if d["m"] == "password":
                return cls("password", d["l"], d["a"], d["n"], password=d["pw"])
            if d["m"] == "adfs":
                return cls(
                    "adfs", d["l"], d["a"], d["n"], access_token=d["at"], access_exp=int(d["ax"]), refresh_token=d["rt"]
                )
        except (KeyError, TypeError, ValueError):
            return None
        return None


def person_key(login: str) -> str:
    """A stable, opaque name for one person: keys the bucket and binds cursors and links."""
    return hashlib.sha256(f"exchange-mcp person\n{login.lower()}".encode()).hexdigest()[:32]
