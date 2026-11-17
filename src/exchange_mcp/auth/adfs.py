"""`adfs` mode: this server is a confidential OAuth client of AD FS, and Exchange trusts AD FS.

The person signs in on AD FS's own page (password, and whatever second factor AD FS requires); this
server never sees the password. The flow is authorization code with PKCE. The server authenticates to
AD FS with a signed JWT (`private_key_jwt`); the private key is a file, never an environment variable.
AD FS returns an access token for the Exchange Web API (EWS only, by the AD FS rule the administrators
set) and a refresh token. Both travel sealed inside this server's own tokens.

Requires Exchange 2019 CU13 or later, or Subscription Edition, on every client-facing server
(docs/auth-modes.md).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.server.auth.provider import TokenError
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from exchange_mcp.auth.credential import Credential
from exchange_mcp.config import Settings
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.ews.client import Ews
from exchange_mcp.keys.seal import Sealer, b64

STATE_TTL = 15 * 60
ASSERTION_TTL = 120


def _claims(jwt: str) -> dict[str, Any]:
    """The payload of a token AD FS just handed us over TLS, in answer to our own authenticated request.
    Its signature is Exchange's to check; we read who it names."""
    try:
        payload = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class Adfs:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, sealer: Sealer, clock=time.time) -> None:
        assert settings.adfs is not None
        self.s = settings
        self.a = settings.adfs
        self.http = http
        self.sealer = sealer
        self.clock = clock
        self.redirect_uri = f"{settings.public_url}/adfs/callback"
        self.authorize_endpoint = f"{self.a.issuer}/oauth2/authorize/"
        self.token_endpoint = f"{self.a.issuer}/oauth2/token/"
        self._key = self._load_key()

    def _load_key(self) -> rsa.RSAPrivateKey:
        from exchange_mcp.config import ConfigError
        from exchange_mcp.keys.providers import exposed

        path = self.a.signing_key_file
        try:
            raw = path.read_bytes()
        except OSError:
            raise ConfigError("the AD FS signing key file cannot be read") from None
        if exposed(path.stat()):
            raise ConfigError("the AD FS signing key file is readable by others or by a foreign group")
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except ValueError:
            raise ConfigError("the AD FS signing key file is not a PEM private key") from None
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ConfigError("the AD FS signing key must be RSA, 2048 bits or more")
        return key

    def assertion(self) -> str:
        now = int(self.clock())
        header = {"alg": "RS256", "typ": "JWT", "kid": self.a.signing_key_id}
        body = {
            "iss": self.a.client_id,
            "sub": self.a.client_id,
            "aud": self.token_endpoint,
            "jti": secrets.token_urlsafe(16),
            "iat": now,
            "nbf": now,
            "exp": now + ASSERTION_TTL,
        }

        def part(d: dict) -> str:
            return b64(json.dumps(d, separators=(",", ":")).encode())

        signing_input = f"{part(header)}.{part(body)}"
        sig = self._key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{b64(sig)}"

    # ---------------------------------------------------------------- the browser leg
    async def authorize_url(self, request_id: str) -> str:
        verifier = secrets.token_urlsafe(48)
        challenge = b64(hashlib.sha256(verifier.encode()).digest())
        state = await self.sealer.seal("adfs_state", {"r": request_id, "v": verifier}, STATE_TTL)
        query = {
            "response_type": "code",
            "client_id": self.a.client_id,
            "redirect_uri": self.redirect_uri,
            "resource": self.a.resource,
            "scope": "openid offline_access",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
        }
        return f"{self.authorize_endpoint}?{urlencode(query)}"

    def callback_route(self, provider):
        async def callback(request: Request) -> Response:
            from exchange_mcp.signin import T, _page, pick_lang

            lang = pick_lang(request.headers.get("accept-language", ""))
            t = T[lang]
            state = await self.sealer.open("adfs_state", request.query_params.get("state", ""))
            code = request.query_params.get("code", "")
            if state is None or not code:
                return _page(lang, heading=t["h1_again"], error=t["expired"], status=400)
            try:
                tokens = await self._token(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                        "code_verifier": state["v"],
                    }
                )
                cred = await self._credential(tokens)
                redirect = await provider.complete_sign_in(state["r"], cred)
            except (TokenError, ExchangeError, LookupError):
                return _page(lang, heading=t["h1_again"], error=t["expired"], status=400)
            return RedirectResponse(redirect, status_code=302)

        return callback

    # ---------------------------------------------------------------- the back channel
    async def _token(self, form: dict[str, str]) -> dict[str, Any]:
        data = {
            **form,
            "client_id": self.a.client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": self.assertion(),
            "resource": self.a.resource,
        }
        try:
            r = await self.http.post(self.token_endpoint, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise ExchangeError("busy", seconds=30) from None
        if r.status_code != 200:
            raise TokenError(error="invalid_grant", error_description="AD FS refused the grant; sign in again")
        body = r.json()
        if not body.get("access_token"):
            raise TokenError(error="invalid_grant", error_description="AD FS returned no access token")
        return body

    async def _credential(self, tokens: dict[str, Any], previous: Credential | None = None) -> Credential:
        claims = _claims(tokens["access_token"])
        login = (claims.get("upn") or claims.get("unique_name") or (previous.login if previous else "")).lower()
        if not login:
            raise ExchangeError("unsupported", reason="AD FS did not say who signed in (no upn claim).")
        if previous is not None and previous.login != login:
            raise TokenError(error="invalid_grant", error_description="the refreshed token names another person")
        exp = int(self.clock()) + int(tokens.get("expires_in", 3600))
        refresh = tokens.get("refresh_token") or (previous.refresh_token if previous else "")
        if previous is not None:
            return Credential(
                "adfs",
                login,
                previous.address,
                previous.name,
                access_token=tokens["access_token"],
                access_exp=exp,
                refresh_token=refresh,
            )
        address, name = await self._whoami(tokens["access_token"], login, claims)
        return Credential(
            "adfs", login, address, name, access_token=tokens["access_token"], access_exp=exp, refresh_token=refresh
        )

    async def _whoami(self, token: str, login: str, claims: dict[str, Any]) -> tuple[str, str]:
        from exchange_mcp.core.resolve import candidates

        probe = Credential("adfs", login, "", "", access_token=token)
        found = await candidates(
            Ews(self.http, self.s.ews_url, probe.auth()), claims.get("email") or login, scope="ActiveDirectory"
        )
        target = (claims.get("email") or login).lower()
        mine = [p for p in found if p.address.lower() == target] or (found if len(found) == 1 else [])
        if len(mine) != 1:
            raise ExchangeError("unsupported", reason="Signed in, but Exchange did not identify your mailbox.")
        return mine[0].address, mine[0].name or mine[0].address

    async def renew(self, cred: Credential) -> Credential:
        """Refresh the AD FS token inside a credential; AD FS refusing means the person signs in again."""
        if cred.access_exp - self.clock() > 300:
            return cred
        if not cred.refresh_token:
            raise TokenError(error="invalid_grant", error_description="no AD FS refresh token; sign in again")
        tokens = await self._token({"grant_type": "refresh_token", "refresh_token": cred.refresh_token})
        return await self._credential(tokens, previous=cred)
