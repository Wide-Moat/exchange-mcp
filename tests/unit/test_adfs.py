"""adfs mode: PKCE and state on the way out, a signed client assertion on the back channel, the person
named by AD FS, and refresh that cannot switch people."""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import EWS_URL, PUBLIC, FakeEws, base_env, ok
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.server.auth.provider import TokenError

from exchange_mcp.auth.adfs import Adfs
from exchange_mcp.auth.credential import Credential
from exchange_mcp.config import ConfigError, Settings
from exchange_mcp.keys.seal import LocalSealer

ISSUER = "https://sts.example.com/adfs"
TOKEN_URL = f"{ISSUER}/oauth2/token/"


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


@pytest.fixture
def key_file(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "adfs.pem"
    p.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    p.chmod(0o400)
    return p, key.public_key()


def adfs_settings(key_path) -> Settings:
    return Settings.from_env(
        base_env(
            AUTH_MODE="adfs",
            ADFS_ISSUER=ISSUER,
            ADFS_CLIENT_ID="client-1",
            ADFS_KEY_FILE=str(key_path),
            ADFS_KEY_ID="kid-1",
            ADFS_RESOURCE="https://mail.example.com/",
        )
    )


class FakeAdfs:
    def __init__(self, ews: FakeEws, upn: str = "alex@corp.example") -> None:
        self.ews = ews
        self.upn = upn
        self.forms: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.forms.append(form)
            if form.get("refresh_token") == "dead":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": jwt({"upn": self.upn, "aud": "https://mail.example.com/"}),
                    "refresh_token": "rt-2",
                    "expires_in": 3600,
                },
            )
        return self.ews.handler(request)


RESOLVED = ok(
    "ResolveNames",
    '<m:ResolutionSet TotalItemsInView="1" IncludesLastItemInRange="true"><t:Resolution><t:Mailbox>'
    "<t:Name>Alex Example</t:Name><t:EmailAddress>alex@corp.example</t:EmailAddress>"
    "<t:MailboxType>Mailbox</t:MailboxType></t:Mailbox></t:Resolution></m:ResolutionSet>",
)


def test_the_key_file_must_be_closed_and_rsa(tmp_path, key_file):
    path, _ = key_file
    path.chmod(0o404)
    with pytest.raises(ConfigError, match="readable by others"):
        Adfs(adfs_settings(path), httpx.AsyncClient(), LocalSealer([("k", os.urandom(32))]))


def test_the_client_assertion_is_signed_for_the_token_endpoint(key_file):
    path, public = key_file
    a = Adfs(adfs_settings(path), httpx.AsyncClient(), LocalSealer([("k", os.urandom(32))]))
    h, b, s = a.assertion().split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    header, body = json.loads(base64.urlsafe_b64decode(pad(h))), json.loads(base64.urlsafe_b64decode(pad(b)))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    assert body["iss"] == body["sub"] == "client-1" and body["aud"] == TOKEN_URL and body["exp"] - body["iat"] <= 120
    public.verify(base64.urlsafe_b64decode(pad(s)), f"{h}.{b}".encode(), padding.PKCS1v15(), hashes.SHA256())


async def test_the_full_adfs_sign_in(ews, key_file):
    from exchange_mcp.app import build

    path, _ = key_file
    ews.on("ResolveNames", RESOLVED)
    fake = FakeAdfs(ews)
    app, _, provider = build(
        adfs_settings(path),
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        LocalSealer([("k1", os.urandom(32))]),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)
    reg = await client.post(
        "/register",
        json={
            "redirect_uris": ["https://client.example.org/cb"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    cid = reg.json()["client_id"]
    auth = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "https://client.example.org/cb",
            "state": "s",
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
        },
    )
    rid = parse_qs(urlsplit(auth.headers["location"]).query)["request"][0]
    consent = await client.get("/signin", params={"request": rid})
    assert (
        consent.status_code == 200 and "client.example.org" in consent.text and "password" not in consent.text.lower()
    )
    assert fake.forms == []  # nothing goes to AD FS before the person agrees on this page
    to_adfs = await client.post("/signin", data={"request": rid})
    assert to_adfs.status_code == 302
    loc = urlsplit(to_adfs.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == f"{ISSUER}/oauth2/authorize/"
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == [f"{PUBLIC}/adfs/callback"]
    cookie = to_adfs.cookies.get("exchange_mcp_signin")
    assert cookie and "httponly" in to_adfs.headers["set-cookie"].lower()

    stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)
    forged = await stranger.get("/adfs/callback", params={"code": "adfs-code", "state": q["state"][0]})
    assert forged.status_code == 400 and fake.forms == []  # another browser cannot finish this sign-in

    back = await client.get(
        "/adfs/callback", params={"code": "adfs-code", "state": q["state"][0]}, cookies={"exchange_mcp_signin": cookie}
    )
    assert back.status_code == 302 and back.headers["location"].startswith("https://client.example.org/cb?code=")
    form = fake.forms[0]
    assert form["grant_type"] == "authorization_code" and form["code_verifier"]
    assert form["client_assertion_type"].endswith("jwt-bearer") and "client_secret" not in form
    assert ews.last("ResolveNames").headers["authorization"].startswith("Bearer ")


async def test_a_forged_state_is_refused(ews, key_file):
    from exchange_mcp.app import build

    path, _ = key_file
    fake = FakeAdfs(ews)
    app, _, _ = build(
        adfs_settings(path),
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        LocalSealer([("k1", os.urandom(32))]),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)
    r = await client.get("/adfs/callback", params={"code": "c", "state": "k1.AAAA"})
    assert r.status_code == 400 and fake.forms == []


async def test_refresh_cannot_switch_people(ews, key_file):
    path, _ = key_file
    fake = FakeAdfs(ews, upn="someone.else@corp.example")
    a = Adfs(
        adfs_settings(path),
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        LocalSealer([("k", os.urandom(32))]),
        clock=lambda: 10_000,
    )
    old = Credential(
        "adfs", "alex@corp.example", "alex@corp.example", "Alex", access_token="a", access_exp=0, refresh_token="rt-1"
    )
    with pytest.raises(TokenError):
        await a.renew(old)


async def test_a_refused_refresh_means_sign_in_again(ews, key_file):
    path, _ = key_file
    fake = FakeAdfs(ews)
    a = Adfs(
        adfs_settings(path),
        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        LocalSealer([("k", os.urandom(32))]),
        clock=lambda: 10_000,
    )
    dead = Credential(
        "adfs", "alex@corp.example", "alex@corp.example", "Alex", access_token="a", access_exp=0, refresh_token="dead"
    )
    with pytest.raises(TokenError):
        await a.renew(dead)


def test_adfs_ews_url_unchanged(key_file):
    assert adfs_settings(key_file[0]).ews_url == EWS_URL
