"""Defects a code review found, each pinned by a test that failed before its fix."""

from __future__ import annotations

import json
import logging
import os

import pytest
from conftest import base_env, envelope, ok

from exchange_mcp.auth.limiter import MAX_KEYS, Limited, Limiter
from exchange_mcp.config import ConfigError, Settings
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.core.guard import MAX_PEOPLE_TRACKED, Guard
from exchange_mcp.core.time import parse
from exchange_mcp.keys.providers import read_key_file
from exchange_mcp.keys.seal import LocalSealer
from exchange_mcp.observability import ContentFilter, JsonFormatter


# ---------------------------------------------------------------- memory stays bounded
def test_the_limiter_refuses_rather_than_grows(monkeypatch):
    import exchange_mcp.auth.limiter as lm

    monkeypatch.setattr(lm, "MAX_KEYS", 4)
    lim = Limiter(5, 20, clock=lambda: 0.0)
    lim.take(login="a", network="n1")
    lim.take(login="b", network="n2")
    with pytest.raises(Limited):
        lim.take(login="c", network="n3")
    assert len(lim._seen) == 4  # noqa: SLF001
    assert MAX_KEYS > 1000


def test_the_guard_refuses_rather_than_grows(monkeypatch):
    import exchange_mcp.core.guard as gm

    monkeypatch.setattr(gm, "MAX_PEOPLE_TRACKED", 2)
    g = Guard(clock=lambda: 0.0)
    g.check("p1", kind="mail", user_confirmed=True)
    g.check("p2", kind="mail", user_confirmed=True)
    with pytest.raises(ExchangeError) as e:
        g.check("p3", kind="mail", user_confirmed=True)
    assert e.value.key == "busy" and MAX_PEOPLE_TRACKED > 1000


# ---------------------------------------------------------------- logs
def test_an_exception_carrying_content_is_dropped_before_formatting():
    try:
        raise RuntimeError("token for pat@corp.example")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert rec.exc_text is None
    assert not ContentFilter().filter(rec)


def test_log_lines_are_json_whatever_the_message_holds():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, 'quote " and\nnewline \\ slash', (), None)
    line = JsonFormatter().format(rec)
    assert "\n" not in line and json.loads(line)["msg"] == 'quote " and\nnewline \\ slash'


# ---------------------------------------------------------------- configuration and time
@pytest.mark.parametrize("url", ["http://localhost:8000?x=1", "http://127.0.0.1:8000#f"])
def test_a_local_public_url_has_no_query_or_fragment(url):
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(PUBLIC_URL=url))


def test_times_that_do_not_exist_or_are_ambiguous_are_refused():
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    cases = [
        ("2026-03-29 02:30", "skip"),  # clocks jump 02:00 -> 03:00
        ("2026-10-25 02:30", "twice"),  # clocks go back 03:00 -> 02:00
        ("2026-11-17 15:00+24:00", "offset"),
        ("2026-11-17 15:00+03:60", "offset"),
    ]
    for value, word in cases:
        with pytest.raises(ExchangeError) as e:
            parse(value, berlin)
        assert word in e.value.text, value
    assert parse("2026-10-25 02:30+01:00", berlin).utcoffset().total_seconds() == 3600


# ---------------------------------------------------------------- keys
def test_a_key_id_twice_is_refused(tmp_path):
    from conftest import key_line

    f = tmp_path / "key"
    f.write_text(key_line("k1") + "\n" + key_line("k1"))
    f.chmod(0o400)
    with pytest.raises(ConfigError, match="twice"):
        read_key_file(f)
    with pytest.raises(ValueError):
        LocalSealer([("k1", os.urandom(32)), ("k1", os.urandom(32))])


def test_an_oversized_key_file_is_not_read(tmp_path):
    f = tmp_path / "key"
    f.write_bytes(b"k" * (70 * 1024))
    f.chmod(0o400)
    with pytest.raises(ConfigError, match="larger than"):
        read_key_file(f)


# ---------------------------------------------------------------- OAuth
async def test_revocation_is_not_offered_because_nothing_could_be_revoked(ews):
    import httpx
    from conftest import PUBLIC

    from exchange_mcp.app import build

    app, _, _ = build(
        Settings.from_env(base_env()),
        httpx.AsyncClient(transport=httpx.MockTransport(ews.handler)),
        LocalSealer([("k1", os.urandom(32))]),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)
    meta = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert "revocation_endpoint" not in meta
    assert (await client.post("/revoke", data={"token": "x"})).status_code in (404, 405)


async def test_a_live_code_is_never_forgotten(monkeypatch):
    from mcp.server.auth.provider import TokenError
    from test_oauth import client, code_for, make

    import exchange_mcp.oauth.provider as op

    monkeypatch.setattr(op, "USED_CODES_MAX", 1)
    p, _ = make()
    c = await client(p)
    first = await code_for(p, c)
    await p.exchange_authorization_code(c, await p.load_authorization_code(c, first))
    second = await code_for(p, c)
    with pytest.raises(TokenError):
        await p.exchange_authorization_code(c, await p.load_authorization_code(c, second))
    assert await p.load_authorization_code(c, first) is None  # still remembered as used


async def test_an_expired_adfs_credential_gets_no_token():
    from mcp.server.auth.provider import TokenError
    from test_oauth import client, make

    from exchange_mcp.auth.credential import Credential

    p, now = make()
    c = await client(p)
    dead = Credential("adfs", "a@corp.example", "a@corp.example", "A", access_token="t", access_exp=int(now[0]) - 1)
    with pytest.raises(TokenError):
        await p._issue(c.client_id, dead, [], None)  # noqa: SLF001


async def test_the_first_sign_in_page_allows_the_client_callback(ews):
    from test_signin import Flow, _pkce

    f = Flow(ews)
    cid = await f.register()
    rid = await f.request_id(cid, _pkce()[1])
    r = await f.client.get("/signin", params={"request": rid})
    assert "form-action 'self' https://client.example.org" in r.headers["content-security-policy"]


# ---------------------------------------------------------------- mail
def draft_with(bcc: int, to: int = 1) -> bytes:
    from test_mail import MARK

    box = "".join(f"<t:Mailbox><t:EmailAddress>b{i}@corp.example</t:EmailAddress></t:Mailbox>" for i in range(bcc))
    tos = "".join(f"<t:Mailbox><t:EmailAddress>t{i}@corp.example</t:EmailAddress></t:Mailbox>" for i in range(to))
    return ok(
        "GetItem",
        f'<m:Items><t:Message><t:ItemId Id="D1" ChangeKey="CK"/><t:IsDraft>true</t:IsDraft>{MARK}'
        f"<t:ToRecipients>{tos}</t:ToRecipients><t:BccRecipients>{box}</t:BccRecipients></t:Message></m:Items>",
    )


async def test_blind_copies_count_against_the_recipient_cap(rt, ews, invoke):
    ews.on("GetItem", draft_with(bcc=15, to=10)).on("SendItem", ok("SendItem"))
    r = await invoke("send_draft", id="D1", user_confirmed=True)
    assert r.is_error and "At most 20 recipients" in r.text
    assert ews.writes() == []


async def test_mail_tips_are_read_for_every_recipient(rt, ews):
    from exchange_mcp.ews import mail as em
    from exchange_mcp.ews.client import Ews
    from exchange_mcp.ews.transport import Bearer

    def tip(addr: str, oof: bool) -> str:
        reply = "<t:ReplyBody><t:Message>Away</t:Message></t:ReplyBody>" if oof else ""
        return (
            '<m:MailTipsResponseMessageType ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
            f"<m:MailTips><t:RecipientAddress><t:EmailAddress>{addr}</t:EmailAddress></t:RecipientAddress>"
            f"<t:OutOfOffice>{reply}</t:OutOfOffice><t:MaxMessageSize>{'1000' if oof else '2000'}</t:MaxMessageSize>"
            "</m:MailTips></m:MailTipsResponseMessageType>"
        )

    ews.on(
        "GetMailTips",
        envelope(
            "<m:GetMailTipsResponse><m:ResponseMessages>"
            + tip("a@corp.example", False)
            + tip("b@corp.example", True)
            + "</m:ResponseMessages></m:GetMailTipsResponse>"
        ),
    )
    tips = await em.mail_tips(
        Ews(rt.http, rt.settings.ews_url, Bearer("t")), "me@corp.example", ["a@corp.example", "b@corp.example"]
    )
    assert tips["automatic_replies"] == ["b@corp.example"] and tips["max_message_size"] == 1000


async def test_turning_the_automatic_reply_off_keeps_its_texts(rt, ews, invoke):
    oof = envelope(
        '<m:GetUserOofSettingsResponse><m:ResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
        "</m:ResponseCode></m:ResponseMessage><t:OofSettings><t:OofState>Enabled</t:OofState>"
        "<t:ExternalAudience>Known</t:ExternalAudience></t:OofSettings></m:GetUserOofSettingsResponse>"
    )
    done = envelope(
        '<m:SetUserOofSettingsResponse><m:ResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
        "</m:ResponseCode></m:ResponseMessage></m:SetUserOofSettingsResponse>"
    )
    ews.on("GetUserOofSettings", oof).on("SetUserOofSettings", done)
    r = await invoke("save_automatic_reply", state="off")
    assert not r.is_error, r.text
    sent = ews.last("SetUserOofSettings").xml()
    assert "InternalReply" not in sent and "ExternalReply" not in sent


async def test_more_than_twenty_five_categories_are_refused(rt, ews, invoke):
    r = await invoke("update_messages", ids=["I1"], action="categorize", categories=[f"c{i}" for i in range(26)])
    assert r.is_error and "At most 25 categories" in r.text
    assert ews.writes() == []


async def test_a_move_returns_the_new_ids(rt, ews, invoke):
    from test_mail import resolved  # noqa: F401

    ews.on("MoveItem", ok("MoveItem", '<m:Items><t:Message><t:ItemId Id="NEW1" ChangeKey="c"/></t:Message></m:Items>'))
    r = await invoke("update_messages", ids=["OLD1"], action="move", folder="inbox")
    assert not r.is_error and r.data["ids"] == ["NEW1"]


async def test_many_attachments_are_capped_and_said(rt, ews, invoke):
    atts = "".join(
        f'<t:FileAttachment><t:AttachmentId Id="A{i}"/><t:Name>f{i}.txt</t:Name><t:Size>1</t:Size>'
        "<t:IsInline>false</t:IsInline></t:FileAttachment>"
        for i in range(30)
    )
    ews.on(
        "GetItem",
        ok(
            "GetItem",
            f'<m:Items><t:Message><t:ItemId Id="M1" ChangeKey="C"/><t:Attachments>{atts}'
            "</t:Attachments><t:IsRead>true</t:IsRead></t:Message></m:Items>",
        ),
    )
    r = await invoke("get_message", id="M1", include_body=False)
    assert len(r.data["attachments"]) == 25 and r.data["attachments_truncated"] and r.data["attachments_total"] == 30


async def test_a_query_keeps_exact_time_bounds(rt, ews, invoke):
    rows = "".join(
        f'<t:Message><t:ItemId Id="I{h}" ChangeKey="c"/><t:Subject>s</t:Subject>'
        f"<t:DateTimeReceived>2026-11-17T{h:02d}:30:00Z</t:DateTimeReceived><t:IsRead>false</t:IsRead></t:Message>"
        for h in (6, 9, 14)
    )
    ews.on(
        "FindItem",
        ok(
            "FindItem",
            f'<m:RootFolder TotalItemsInView="3" IncludesLastItemInRange="true">'
            f"<t:Items>{rows}</t:Items></m:RootFolder>",
        ),
    )
    # Berlin is UTC+1 in November: 09:00-15:00 local is 08:00-14:00 UTC, so only 09:30Z is inside.
    r = await invoke("list_messages", query="report", since="2026-11-17 09:00", until="2026-11-17 15:00")
    assert not r.is_error, r.text
    assert [m["id"] for m in r.data["items"]] == ["I9"]
