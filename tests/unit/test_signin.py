"""The sign-in page in password mode: login forms, attempt limits, identical refusals, headers."""

from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import EWS_URL, PUBLIC, FakeEws, base_env, ok

from exchange_mcp.app import build
from exchange_mcp.auth.limiter import Limited, Limiter
from exchange_mcp.auth.login import BadLogin, normalise
from exchange_mcp.config import Settings
from exchange_mcp.keys.seal import LocalSealer

RESOLVED = ok(
    "ResolveNames",
    '<m:ResolutionSet TotalItemsInView="1" IncludesLastItemInRange="true"><t:Resolution>'
    "<t:Mailbox><t:Name>Alex Example</t:Name><t:EmailAddress>alex.example@corp.example</t:EmailAddress>"
    "<t:RoutingType>SMTP</t:RoutingType><t:MailboxType>Mailbox</t:MailboxType></t:Mailbox>"
    "<t:Contact><t:DisplayName>Alex Example</t:DisplayName><t:Alias>alex</t:Alias></t:Contact>"
    "</t:Resolution></m:ResolutionSet>",
)


@pytest.mark.parametrize("typed", ["alex", "CORP\\alex", "corp\\Alex", "alex@corp.example", " Alex@CORP.example "])
def test_every_login_form_is_one_person(typed):
    assert normalise(typed, "upn", "corp.example") == "alex@corp.example"
    assert normalise(typed, "down_level", "CORP", frozenset({"corp.example"})) == "CORP\\alex"


@pytest.mark.parametrize("typed", ["alex@corp.unrelated.example", "alex@corp.example"])
def test_down_level_takes_only_the_configured_upn_suffix(typed):
    with pytest.raises(BadLogin):
        normalise(typed, "down_level", "CORP")  # no LOGIN_ALSO: no @ form is accepted
    with pytest.raises(BadLogin):
        normalise("alex@corp.unrelated.example", "down_level", "CORP", frozenset({"corp.example"}))


@pytest.mark.parametrize("typed", ["OTHER\\alex", "alex@other.example", "", "a b", "alex@", "\\alex", "x" * 300])
def test_a_login_of_another_domain_is_refused(typed):
    with pytest.raises(BadLogin):
        normalise(typed, "upn", "corp.example")


def test_password_attempts_are_limited():
    t = [0.0]
    lim = Limiter(per_login=5, per_network=20, clock=lambda: t[0])
    for _ in range(5):
        lim.take(login="a@corp.example", network="10.0.0.1")
    with pytest.raises(Limited):
        lim.take(login="a@corp.example", network="10.0.0.2")
    for i in range(15):  # the network has 5 attempts counted; 15 more fill it
        lim.take(login=f"u{i}@corp.example", network="10.0.0.1")
    with pytest.raises(Limited):
        lim.take(login="fresh@corp.example", network="10.0.0.1")
    t[0] += 15 * 60 + 1
    lim.take(login="a@corp.example", network="10.0.0.1")  # the window slid


def _pkce() -> tuple[str, str]:
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class Flow:
    def __init__(self, ews: FakeEws, **env: str) -> None:
        import os

        self.ews = ews
        settings = Settings.from_env(base_env(**env))
        outbound = httpx.AsyncClient(transport=httpx.MockTransport(ews.handler))
        self.app, self.server, self.provider = build(settings, outbound, LocalSealer([("k1", os.urandom(32))]))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("10.1.2.3", 5555)), base_url=PUBLIC
        )

    async def register(self) -> str:
        r = await self.client.post(
            "/register",
            json={
                "redirect_uris": ["https://client.example.org/cb"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert r.status_code == 201, r.text
        return r.json()["client_id"]

    async def request_id(self, cid: str, challenge: str) -> str:
        r = await self.client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": "https://client.example.org/cb",
                "state": "st",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert r.status_code == 302, r.text
        return parse_qs(urlsplit(r.headers["location"]).query)["request"][0]

    async def submit(self, rid: str, login: str, password: str, **headers: str) -> httpx.Response:
        """As a browser does: open the page (its form carries a token, its cookie the same), then post."""
        page = await self.client.get("/signin", params={"request": rid})
        token = re.search(r'name="form" value="([^"]+)"', page.text).group(1)
        data = {"request": rid, "login": login, "password": password, "form": token}
        return await self.client.post("/signin", data=data, headers=headers)


async def test_a_full_sign_in_issues_tokens_that_carry_the_person(ews):
    ews.on("ResolveNames", RESOLVED)
    f = Flow(ews)
    cid = await f.register()
    verifier, challenge = _pkce()
    rid = await f.request_id(cid, challenge)
    page = await f.client.get("/signin", params={"request": rid})
    assert page.status_code == 200 and 'name="password"' in page.text
    r = await f.submit(rid, "CORP\\alex", "pw-not-in-logs-1")
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["st"]
    tok = await f.client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": "https://client.example.org/cb",
            "client_id": cid,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    cred = await f.provider.credential_of(tok.json()["access_token"])
    assert cred.login == "alex@corp.example" and cred.address == "alex.example@corp.example"
    assert tok.json()["expires_in"] == 3600
    sent = ews.last("ResolveNames")
    assert sent.url == EWS_URL and "alex" in sent.xml()


async def test_unknown_and_wrong_look_the_same(ews):
    ews.on("ResolveNames", 401)
    f = Flow(ews)
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    wrong = await f.submit(rid, "alex", "wrong")
    other_domain = await f.submit(rid, "OTHER\\alex", "wrong")
    empty = await f.submit(rid, "alex", "")

    def message(r: httpx.Response) -> str:
        return re.search(r'role="alert">([^<]*)<', r.text).group(1)

    assert wrong.status_code == other_domain.status_code == empty.status_code == 400
    assert message(wrong) == message(other_domain) == message(empty)
    assert ews.attempts() == 1  # the other domain and the empty password never reached Exchange
    assert ews.ops() == []  # the wrong password failed the NTLM handshake: no operation ran


async def test_the_page_limits_attempts_before_exchange(ews):
    ews.on("ResolveNames", 401)
    f = Flow(ews, ATTEMPTS_PER_LOGIN="2")
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    for _ in range(2):
        assert (await f.submit(rid, "alex", "wrong")).status_code == 400
    third = await f.submit(rid, "alex@corp.example", "wrong")
    assert third.status_code == 429
    assert ews.attempts() == 2


async def test_the_sign_in_page_is_not_cached_or_framed(ews):
    f = Flow(ews)
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    r = await f.client.get("/signin", params={"request": rid})
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "https://" not in r.text.split("<form")[0].replace(PUBLIC, "")  # nothing loaded from elsewhere


async def test_an_expired_sign_in_link_is_refused(ews):
    f = Flow(ews)
    r = await f.client.get("/signin", params={"request": "k1.AAAA"})
    assert r.status_code == 410


def test_every_language_says_everything():
    from exchange_mcp.signin import T

    assert set(T) == {"en", "ru", "es", "de", "fr"}
    for lang, words in T.items():
        assert set(words) == set(T["en"]), lang
        assert all(v.strip() for v in words.values()), lang


@pytest.mark.parametrize(
    "header,lang",
    [
        ("de-DE,de;q=0.9,en;q=0.8", "de"),
        ("fr-CH, fr;q=0.9", "fr"),
        ("es-ES", "es"),
        ("ru,en;q=0.5", "ru"),
        ("en;q=0.2,ru;q=0.9", "ru"),
        ("zh-CN,zh;q=0.9", "en"),
        ("", "en"),
        ("de;q=0", "en"),
    ],
)
def test_the_page_speaks_the_browsers_language(header, lang):
    from exchange_mcp.signin import pick_lang

    assert pick_lang(header) == lang


async def test_the_sign_in_page_is_served_in_that_language(ews):
    f = Flow(ews)
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    r = await f.client.get("/signin", params={"request": rid}, headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert '<html lang="de">' in r.text and "Kennwort" in r.text and r.headers["content-language"] == "de"


async def test_the_page_says_which_application_asks(ews):
    f = Flow(ews)
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    r = await f.client.get("/signin", params={"request": rid})
    assert "client.example.org" in r.text and "send only what you confirm" in r.text


async def test_a_form_posted_from_another_site_is_refused(ews):
    """The page's token and its cookie must match, and the browser must say the post came from this site."""
    ews.on("ResolveNames", RESOLVED)
    f = Flow(ews)
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    cross = await f.submit(rid, "alex", "pw-not-in-logs-1", origin="https://evil.example.org")
    assert cross.status_code == 403
    blind = await f.client.post("/signin", data={"request": rid, "login": "alex", "password": "x", "form": "guess"})
    assert blind.status_code == 403
    assert ews.attempts() == 0  # neither reached Exchange


def test_origins_compare_as_browsers_serialise_them():
    from exchange_mcp.signin import _origin_of

    assert _origin_of("https://MCP.example.com:443") == _origin_of("https://mcp.example.com")
    assert _origin_of("https://mcp.example.com:8443") != _origin_of("https://mcp.example.com")
    assert _origin_of("http://mcp.example.com") != _origin_of("https://mcp.example.com")


async def test_a_non_ascii_form_token_is_refused_not_a_crash(ews):
    f = Flow(ews)
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    await f.client.get("/signin", params={"request": rid})  # the page sets its cookie
    r = await f.client.post("/signin", data={"request": rid, "login": "a", "password": "b", "form": "токен"})
    assert r.status_code == 403
