"""The OAuth provider: every artefact sealed, bound to its client, used once, and dead past its time."""

from __future__ import annotations

import os

import pytest
from conftest import memory_store
from mcp.server.auth.provider import AuthorizationParams, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from exchange_mcp.auth.credential import Credential
from exchange_mcp.keys.seal import LocalSealer
from exchange_mcp.oauth.provider import Provider

CRED = Credential("password", "alex@corp.example", "alex@corp.example", "Alex", password="Sealed-Password-Value-42")


def make(clock=None, refresh_ttl=12 * 3600):
    now = clock or [1000.0]
    sealer = LocalSealer([("k1", os.urandom(32))], clock=lambda: now[0])
    provider = Provider(
        sealer,
        memory_store(sealer),
        public_url="https://mcp.example.com",
        refresh_ttl=refresh_ttl,
        clock=lambda: now[0],
    )
    return provider, now


async def client(p: Provider, uri: str = "https://client.example.org/cb") -> OAuthClientInformationFull:
    info = OAuthClientInformationFull(
        client_id="placeholder",
        redirect_uris=[AnyUrl(uri)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    await p.register_client(info)
    return info


async def code_for(p: Provider, c: OAuthClientInformationFull) -> str:
    url = await p.authorize(
        c,
        AuthorizationParams(
            state="s",
            scopes=[],
            code_challenge="x" * 43,
            redirect_uri=AnyUrl("https://client.example.org/cb"),
            redirect_uri_provided_explicitly=True,
            resource=None,
        ),
    )
    rid = url.split("request=")[1]
    redirect = await p.complete_sign_in(rid, CRED)
    return redirect.split("code=")[1].split("&")[0]


async def test_an_altered_token_is_refused():
    p, _ = make()
    c = await client(p)
    code = await code_for(p, c)
    tok = await p.exchange_authorization_code(c, await p.load_authorization_code(c, code))
    assert await p.load_access_token(tok.access_token) is not None
    bad = tok.access_token[:-3] + ("AAA" if not tok.access_token.endswith("AAA") else "BBB")
    assert await p.load_access_token(bad) is None
    assert await p.load_access_token(tok.refresh_token) is None  # a refresh token is not an access token
    assert await p.load_refresh_token(c, tok.access_token) is None


async def test_a_token_of_a_retired_key_is_refused():
    p, _ = make()
    c = await client(p)
    tok = await p.exchange_authorization_code(c, await p.load_authorization_code(c, await code_for(p, c)))
    other, _ = make()
    assert await other.load_access_token(tok.access_token) is None
    assert await other.get_client(c.client_id) is None


async def test_tokens_expire():
    p, now = make(refresh_ttl=12 * 3600)
    c = await client(p)
    tok = await p.exchange_authorization_code(c, await p.load_authorization_code(c, await code_for(p, c)))
    now[0] += 3601
    assert await p.load_access_token(tok.access_token) is None
    assert await p.load_refresh_token(c, tok.refresh_token) is not None
    now[0] += 12 * 3600
    assert await p.load_refresh_token(c, tok.refresh_token) is None


async def test_a_code_belongs_to_its_client():
    p, _ = make()
    a, b = await client(p), await client(p)
    code = await code_for(p, a)
    assert await p.load_authorization_code(b, code) is None
    tok = await p.exchange_authorization_code(a, await p.load_authorization_code(a, code))
    assert await p.load_refresh_token(b, tok.refresh_token) is None


async def test_a_code_is_used_once():
    p, _ = make()
    c = await client(p)
    code = await code_for(p, c)
    loaded = await p.load_authorization_code(c, code)
    await p.exchange_authorization_code(c, loaded)
    with pytest.raises(TokenError) as e:
        await p.exchange_authorization_code(c, await p.load_authorization_code(c, code))
    assert e.value.error == "invalid_grant"


@pytest.mark.parametrize(
    "uri", ["http://client.example.org/cb", "https://client.example.org/cb#x", "https://u:p@client.example.org/"]
)
async def test_redirect_uris_are_exact(uri):
    p, _ = make()
    with pytest.raises(RegistrationError):
        await client(p, uri)
    await client(p, "http://127.0.0.1:33418/callback")  # loopback for desktop clients


async def test_the_access_token_carries_the_credential_sealed():
    p, _ = make()
    c = await client(p)
    tok = await p.exchange_authorization_code(c, await p.load_authorization_code(c, await code_for(p, c)))
    assert "Sealed-Password-Value-42" not in tok.access_token and "alex@corp.example" not in tok.access_token
    assert (await p.credential_of(tok.access_token)).password == "Sealed-Password-Value-42"


async def test_an_operator_can_limit_which_clients_register():
    import os

    sealer = LocalSealer([("k1", os.urandom(32))])
    p = Provider(
        sealer,
        memory_store(sealer),
        public_url="https://mcp.example.com",
        refresh_ttl=3600,
        client_hosts=frozenset({"chat.corp.example"}),
    )
    with pytest.raises(RegistrationError):
        await client(p, "https://evil.example.org/cb")
    await client(p, "https://chat.corp.example/oauth/callback")


async def test_refreshing_does_not_extend_the_session():
    """A refresh renews the access token; the session still ends at sign-in time plus the refresh lifetime."""
    p, now = make(refresh_ttl=12 * 3600)
    c = await client(p)
    tok = await p.exchange_authorization_code(c, await p.load_authorization_code(c, await code_for(p, c)))
    for _ in range(11):  # an hour before each expiry, eleven times
        now[0] += 3600
        rt = await p.load_refresh_token(c, tok.refresh_token)
        tok = await p.exchange_refresh_token(c, rt, [])
    assert tok.expires_in <= 3600
    now[0] += 3600 + 1  # twelve hours after sign-in
    assert await p.load_refresh_token(c, tok.refresh_token) is None


async def test_the_session_starts_at_sign_in_not_at_redemption():
    p, now = make(refresh_ttl=12 * 3600)
    c = await client(p)
    code = await code_for(p, c)
    now[0] += 290  # the client redeems the code almost five minutes later
    tok = await p.exchange_authorization_code(c, await p.load_authorization_code(c, code))
    now[0] += 12 * 3600 - 290 + 1  # twelve hours after the sign-in itself
    assert await p.load_refresh_token(c, tok.refresh_token) is None


async def test_a_payload_too_large_to_open_is_not_sealed():
    import os

    from exchange_mcp.keys.seal import TooLarge

    s = LocalSealer([("k1", os.urandom(32))])
    with pytest.raises(TooLarge):
        await s.seal("client", {"x": "a" * 70_000}, 60)
