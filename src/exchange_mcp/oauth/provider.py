"""OAuth 2.1 authorization server for MCP clients, in front of the Exchange sign-in. It stores nothing.

Every artefact of the flow is a sealed blob (`keys.seal`) carrying what the next step needs:

| Artefact | Carries | Lives |
|---|---|---|
| `client_id` | the client's registration | as long as the client keeps it |
| sign-in request | the client and its authorization parameters | 15 minutes, in the sign-in URL |
| authorization code | the parameters and the person's credential | 5 minutes, once per replica |
| access token | the credential | 1 hour (never past the AD FS token it carries) |
| refresh token | the credential | `password`: 12 hours by default, 24 at most; `adfs`: 30 days |

A token sealed under a key this server no longer holds does not open: the client gets 401 and the
person signs in again. Any number of replicas serve the same tokens when they share the key.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from exchange_mcp.auth.credential import Credential, person_key
from exchange_mcp.keys.seal import Sealer

ACCESS_TTL = 3600
CODE_TTL = 300
SIGN_IN_TTL = 15 * 60
CLIENT_TTL = 10 * 365 * 24 * 3600
USED_CODES_MAX = 10_000


def redirect_allowed(uri: str) -> bool:
    u = urlsplit(uri)
    if u.fragment or u.username or u.password or not u.hostname:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


class Provider:
    def __init__(
        self,
        sealer: Sealer,
        *,
        public_url: str,
        refresh_ttl: int,
        renew: Callable[[Credential], Awaitable[Credential]] | None = None,
        client_hosts: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sealer = sealer
        self.public_url = public_url.rstrip("/")
        self._refresh_ttl = refresh_ttl
        self._renew = renew  # adfs: refresh the AD FS token inside the credential
        self._client_hosts = client_hosts
        self._clock = clock
        self._used_codes: dict[str, float] = {}

    # ---------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        body = await self.sealer.open("client", client_id)
        if body is None:
            return None
        return OAuthClientInformationFull.model_validate({**body["info"], "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """The registration becomes the client id: the SDK answers with this same object."""
        for uri in client_info.redirect_uris or []:
            host = (urlsplit(str(uri)).hostname or "").lower()
            if self._client_hosts and host not in self._client_hosts:
                from mcp.server.auth.provider import RegistrationError

                raise RegistrationError(
                    error="invalid_redirect_uri", error_description="this server accepts only its operator's clients"
                )
            if not redirect_allowed(str(uri)):
                from mcp.server.auth.provider import RegistrationError

                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="redirect URIs are https, or http on a loopback address",
                )
        info = client_info.model_dump(mode="json", exclude={"client_id"}, exclude_none=True)
        client_info.client_id = await self.sealer.seal("client", {"info": info}, CLIENT_TTL)

    # ---------------------------------------------------------------- authorize -> sign-in page
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request = await self.sealer.seal(
            "signin",
            {
                "client_id": client.client_id,
                "state": params.state,
                "scopes": params.scopes or [],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
            SIGN_IN_TTL,
        )
        return f"{self.public_url}/signin?request={request}"

    async def sign_in_request(self, request_id: str) -> dict | None:
        return await self.sealer.open("signin", request_id)

    async def complete_sign_in(self, request_id: str, credential: Credential) -> str:
        """The person proved themselves: hand the client a one-time code that carries their credential."""
        p = await self.sign_in_request(request_id)
        if p is None:
            raise LookupError("sign-in request unknown or expired")
        p.pop("exp", None)
        signed_in = int(self._clock())
        code = await self.sealer.seal("code", {**p, "c": credential.to_payload(), "s0": signed_in}, CODE_TTL)
        return construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])

    # ---------------------------------------------------------------- codes
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        p = await self.sealer.open("code", authorization_code)
        if p is None or p["client_id"] != client.client_id or authorization_code in self._used_codes:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=p["scopes"],
            expires_at=p["exp"],
            client_id=client.client_id,
            code_challenge=p["code_challenge"],
            redirect_uri=AnyUrl(p["redirect_uri"]),
            redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
            resource=p["resource"],
            subject=None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        p = await self.sealer.open("code", authorization_code.code)
        if p is None or authorization_code.code in self._used_codes:
            raise TokenError(error="invalid_grant", error_description="authorization code unknown or used")
        now = self._clock()
        for k in [k for k, exp in self._used_codes.items() if exp < now]:
            del self._used_codes[k]
        if len(self._used_codes) >= USED_CODES_MAX:
            # Forgetting a live code would make it redeemable again: refuse until codes expire instead.
            raise TokenError(error="invalid_request", error_description="too many sign-ins at once; try again")
        self._used_codes[authorization_code.code] = authorization_code.expires_at
        cred = Credential.from_payload(p["c"])
        if cred is None:
            raise TokenError(error="invalid_grant", error_description="authorization code damaged")
        if cred.mode == "adfs" and self._renew is not None:
            cred = await self._renew(cred)  # a code can wait five minutes; the AD FS token inside may be near its end
        return await self._issue(
            client.client_id,
            cred,
            authorization_code.scopes,
            authorization_code.resource,
            started=int(p.get("s0", now)),  # the sign-in, not the redemption: a code can wait five minutes
        )

    # ---------------------------------------------------------------- tokens
    async def _issue(
        self, client_id: str, cred: Credential, scopes: list[str], resource: str | None, *, started: int
    ) -> OAuthToken:
        """`started` is the sign-in time and never moves: refreshing renews the access token, not the session.
        Every refresh token ends when the session ends, the sign-in time plus the refresh lifetime."""
        session_left = int(started + self._refresh_ttl - self._clock())
        if session_left <= 0:
            raise TokenError(error="invalid_grant", error_description="the session has ended; sign in again")
        body = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "c": cred.to_payload(),
            "s0": started,
        }
        access_ttl = ACCESS_TTL
        if cred.mode == "adfs":
            left = int(cred.access_exp - self._clock())
            if left <= 0:
                raise TokenError(error="invalid_grant", error_description="the AD FS sign-in has ended; sign in again")
            access_ttl = min(ACCESS_TTL, left)  # never past the AD FS token it carries
        access_ttl = min(access_ttl, session_left)
        return OAuthToken(
            access_token=await self.sealer.seal("access", body, access_ttl),
            token_type="Bearer",
            expires_in=access_ttl,
            refresh_token=await self.sealer.seal("refresh", body, session_left),
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        p = await self.sealer.open("refresh", refresh_token)
        if p is None or p["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=p["scopes"],
            expires_at=p["exp"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        p = await self.sealer.open("refresh", refresh_token.token)
        cred = Credential.from_payload(p["c"]) if p else None
        if p is None or cred is None or p["client_id"] != client.client_id:
            raise TokenError(error="invalid_grant", error_description="refresh token unknown or expired")
        if cred.mode == "adfs" and self._renew is not None:
            cred = await self._renew(cred)  # raises TokenError when AD FS refuses: the person signs in again
        granted = [s for s in (scopes or p["scopes"]) if s in p["scopes"]]
        started = p.get("s0")
        if not isinstance(started, int):  # a token from before the session limit: its own expiry bounds it
            started = int(p["exp"] - self._refresh_ttl)
        return await self._issue(client.client_id, cred, granted, p["resource"], started=started)

    async def credential_of(self, access_token: str) -> Credential | None:
        p = await self.sealer.open("access", access_token)
        return Credential.from_payload(p["c"]) if p else None

    async def load_access_token(self, token: str) -> AccessToken | None:
        p = await self.sealer.open("access", token)
        cred = Credential.from_payload(p["c"]) if p else None
        if p is None or cred is None:
            return None
        return AccessToken(
            token=token,
            client_id=p["client_id"],
            scopes=p["scopes"],
            expires_at=p["exp"],
            resource=p["resource"],
            subject=person_key(cred.login),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Not offered: the revocation endpoint is off (app.py), because nothing is stored to revoke and
        answering success would be a lie. Tokens end at their expiry, all of them with the key, and in
        password mode with the person's password (docs/auth-modes.md)."""
        raise NotImplementedError
