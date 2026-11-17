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
        await p._issue(c.client_id, dead, [], None, started=int(now[0]))  # noqa: SLF001


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
    """Live (2016 CU23): SetUserOofSettings without both replies is ErrorInvalidUserOofSettings. Turning the
    reply off sends the saved texts back unchanged, so they survive and Exchange accepts the request."""
    oof = envelope(
        '<m:GetUserOofSettingsResponse><m:ResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
        "</m:ResponseCode></m:ResponseMessage><t:OofSettings><t:OofState>Scheduled</t:OofState>"
        "<t:ExternalAudience>Known</t:ExternalAudience><t:InternalReply><t:Message>Back Monday</t:Message>"
        "</t:InternalReply><t:ExternalReply><t:Message>Away</t:Message></t:ExternalReply></t:OofSettings>"
        "</m:GetUserOofSettingsResponse>"
    )

    def set_like_exchange(seen):
        x = seen.xml()
        code = "NoError" if ("InternalReply" in x and "ExternalReply" in x) else "ErrorInvalidUserOofSettings"
        cls = "Success" if code == "NoError" else "Error"
        return envelope(
            f'<m:SetUserOofSettingsResponse><m:ResponseMessage ResponseClass="{cls}"><m:ResponseCode>{code}'
            "</m:ResponseCode></m:ResponseMessage></m:SetUserOofSettingsResponse>"
        )

    ews.on("GetUserOofSettings", oof).on("SetUserOofSettings", set_like_exchange)
    r = await invoke("save_automatic_reply", state="off")
    assert not r.is_error, r.text
    sent = ews.last("SetUserOofSettings").xml()
    assert "<t:OofState>Disabled</t:OofState>" in sent
    assert "Back Monday" in sent and "Away" in sent  # the saved texts, unchanged


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
    ews.on("GetItem", ok("GetItem"))  # previews are built from the bodies (QA D4)
    # Berlin is UTC+1 in November: 09:00-15:00 local is 08:00-14:00 UTC, so only 09:30Z is inside.
    r = await invoke("list_messages", query="report", since="2026-11-17 09:00", until="2026-11-17 15:00")
    assert not r.is_error, r.text
    assert [m["id"] for m in r.data["items"]] == ["I9"]


# ---------------------------------------------------------------- second review pass
async def test_an_oversized_answer_to_a_write_leaves_the_outcome_open(monkeypatch):
    import httpx
    from conftest import EWS_URL

    from exchange_mcp.ews import client as ec
    from exchange_mcp.ews.soap import M
    from exchange_mcp.ews.transport import Bearer

    monkeypatch.setattr(ec, "MAX_RESPONSE_BYTES", 100)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<" * 500)))
    with pytest.raises(ExchangeError) as e:
        await ec.Ews(http, EWS_URL, Bearer("t")).call(M.SendItem())
    assert e.value.key == "uncertain"


async def test_a_batch_with_a_permanent_error_is_not_retried_as_busy(ews, http):
    from conftest import EWS_URL

    from exchange_mcp.ews.client import Ews
    from exchange_mcp.ews.soap import M
    from exchange_mcp.ews.transport import Bearer

    msg = (
        '<m:GetItemResponseMessage ResponseClass="Error"><m:ResponseCode>{}</m:ResponseCode></m:GetItemResponseMessage>'
    )
    ews.on(
        "GetItem",
        envelope(
            "<m:GetItemResponse><m:ResponseMessages>"
            + msg.format("ErrorServerBusy")
            + msg.format("ErrorAccessDenied")
            + "</m:ResponseMessages></m:GetItemResponse>"
        ),
    )
    with pytest.raises(ExchangeError) as e:
        await Ews(http, EWS_URL, Bearer("t")).call(M.GetItem())
    assert e.value.key != "busy" and ews.ops() == ["GetItem"]


def test_ntlm_legs_keep_the_client_timeouts():
    import httpx

    from exchange_mcp.ews.transport import Ntlm

    req = httpx.Request(
        "POST",
        "https://mail.example.com/EWS/Exchange.asmx",
        content=b"<x/>",
        extensions={"timeout": {"connect": 5, "read": 30, "write": 30, "pool": 5}},
    )
    flow = Ntlm("CORP\\alex", "pw").auth_flow(req)
    first = next(flow)
    assert first.extensions["timeout"]["read"] == 30 and first.content == b""


def test_offsets_past_fourteen_hours_are_refused():
    from zoneinfo import ZoneInfo

    for bad in ("2026-11-17 15:00+14:30", "2026-11-17 15:00-14:59"):
        with pytest.raises(ExchangeError):
            parse(bad, ZoneInfo("UTC"))
    assert parse("2026-11-17 15:00+14:00", ZoneInfo("UTC")).utcoffset().total_seconds() == 14 * 3600


def test_a_time_zone_without_a_windows_name_is_refused():
    with pytest.raises(ConfigError, match="Windows zone"):
        Settings.from_env(base_env(TIMEZONE="Antarctica/Troll"))
    assert Settings.from_env(base_env(TIMEZONE="America/Toronto")).timezone == "America/Toronto"


async def test_a_blank_phone_is_refused_rather_than_shifting_slots(rt, ews):
    from exchange_mcp.tools.contacts import save_contact

    with pytest.raises(ExchangeError):
        await save_contact(name="Pat", emails=[], phones=["", "555-0100"])
    assert ews.ops() == []


async def test_a_meeting_across_a_clock_change_lasts_its_minutes(rt, ews, invoke):
    from test_calendar import CREATED

    ews.on("CreateItem", CREATED)
    # Berlin, 28 March 2027: clocks jump 02:00 -> 03:00. 01:30 + 120 minutes is 04:30 local, 02:30Z.
    r = await invoke("save_event", subject="Long", start="2027-03-28 01:30", duration=120)
    assert not r.is_error, r.text
    xml = ews.last("CreateItem").xml()
    assert "2027-03-28T00:30:00Z" in xml and "2027-03-28T02:30:00Z" in xml


async def test_suggestions_are_only_times_everyone_is_free(rt, ews, invoke):
    ews.on(
        "GetUserAvailability",
        envelope(
            "<m:GetUserAvailabilityResponse><m:FreeBusyResponseArray/><m:SuggestionsResponse>"
            '<m:ResponseMessage ResponseClass="Error"><m:ResponseCode>ErrorInvalidSuggestionTimeRange</m:ResponseCode>'
            "</m:ResponseMessage></m:SuggestionsResponse></m:GetUserAvailabilityResponse>"
        ),
    )
    ews.on(
        "ResolveNames",
        ok(
            "ResolveNames",
            '<m:ResolutionSet TotalItemsInView="1" IncludesLastItemInRange="true">'
            "<t:Resolution><t:Mailbox><t:Name>Pat</t:Name><t:EmailAddress>pat@corp.example"
            "</t:EmailAddress><t:MailboxType>Mailbox</t:MailboxType></t:Mailbox></t:Resolution>"
            "</m:ResolutionSet>",
        ),
    )
    r = await invoke(
        "find_meeting_times", people=["pat@corp.example"], start="2026-11-18 09:00", end="2026-11-18 18:00", duration=30
    )
    assert r.is_error and "refused" in r.text and "ErrorInvalidSuggestionTimeRange" not in r.text
    assert "Excellent" in ews.last("GetUserAvailability").xml()


async def test_a_blob_that_would_expand_without_bound_does_not_open():
    import zlib

    from exchange_mcp.keys import seal as sm

    s = LocalSealer([("k1", os.urandom(32))])
    nonce = os.urandom(12)
    bomb = zlib.compress(b'{"k":"client","exp":9999999999,"x":"' + b"a" * 200_000 + b'"}', 9)
    blob = "k1." + sm.b64(nonce + s._aead["k1"].encrypt(nonce, bomb, sm._aad("client", "k1")))  # noqa: SLF001
    assert len(blob) < sm.MAX_BLOB and await s.open("client", blob) is None


async def test_sign_in_never_takes_a_namesake_for_the_person(rt, ews):
    """Authenticated as 'al', the directory also has al@other.example (a namesake) and the person's own
    mailbox under the alias 'al'. Only the alias identifies the mailbox; a local part never does."""
    from conftest import PASSWORD

    from exchange_mcp.auth.password import check

    def entry(name, addr, alias):
        a = f"<t:Contact><t:Alias>{alias}</t:Alias></t:Contact>" if alias else ""
        return (
            f"<t:Resolution><t:Mailbox><t:Name>{name}</t:Name><t:EmailAddress>{addr}</t:EmailAddress>"
            f"<t:MailboxType>Mailbox</t:MailboxType></t:Mailbox>{a}</t:Resolution>"
        )

    namesake_only = ok(
        "ResolveNames",
        '<m:ResolutionSet TotalItemsInView="1" IncludesLastItemInRange="true">'
        + entry("Other Alex", "alex@other.example", "other")
        + "</m:ResolutionSet>",
    )
    ews.on("ResolveNames", namesake_only)
    with pytest.raises(ExchangeError):
        await check(rt.http, rt.settings.ews_url, "CORP\\alex", PASSWORD)
    both = ok(
        "ResolveNames",
        '<m:ResolutionSet TotalItemsInView="2" IncludesLastItemInRange="true">'
        + entry("Alex Mine", "alex.real@corp.example", "alex")
        + entry("Alex Other", "alex@corp.example", "")
        + "</m:ResolutionSet>",
    )
    ews.on("ResolveNames", both)
    cred = await check(rt.http, rt.settings.ews_url, "CORP\\alex", PASSWORD)
    assert cred.address == "alex.real@corp.example"


def test_an_ntlm_challenge_is_found_among_others_in_one_header():
    import base64

    import httpx

    from exchange_mcp.ews.transport import _challenge

    tok = base64.b64encode(b"challenge-bytes").decode()
    r = httpx.Response(401, headers={"WWW-Authenticate": f"Negotiate, NTLM {tok}"})
    assert _challenge(r) == b"challenge-bytes"


async def test_more_than_twenty_five_attachments_are_refused(rt, ews, invoke):
    r = await invoke("save_draft", to=["pat@corp.example"], body="x", attach=[f"f{i}.txt" for i in range(26)])
    assert r.is_error and "At most 25 attachments" in r.text
    assert ews.writes() == []


async def test_a_secondary_address_of_a_list_is_refused(rt, ews, invoke):
    from test_mail import resolved

    ews.on("ResolveNames", resolved("All Staff", "all@corp.example", "PublicDL"))
    r = await invoke("save_draft", to=["everyone@corp.example"], body="x")
    assert r.is_error and "distribution list" in r.text
    assert ews.writes() == []


async def test_suggestions_ask_exchange_for_at_least_thirty_minutes(rt, ews):
    from datetime import UTC, datetime

    from exchange_mcp.ews.calendar import availability
    from exchange_mcp.ews.client import Ews
    from exchange_mcp.ews.transport import Bearer

    ews.on(
        "GetUserAvailability",
        envelope("<m:GetUserAvailabilityResponse><m:FreeBusyResponseArray/></m:GetUserAvailabilityResponse>"),
    )
    s, e = datetime(2026, 11, 18, 9, tzinfo=UTC), datetime(2026, 11, 18, 18, tzinfo=UTC)
    await availability(
        Ews(rt.http, rt.settings.ews_url, Bearer("t")),
        [("a@corp.example", "Required")],
        s,
        e,
        duration=15,
        working_hours_only=True,
    )
    assert "<t:MeetingDurationInMinutes>30</t:MeetingDurationInMinutes>" in ews.last("GetUserAvailability").xml()


async def test_deleting_a_folder_asks_for_the_right_reason(rt, ews, invoke):
    r = await invoke("delete_mail_folder", folder="Projects")
    assert r.is_error and "everything in it" in r.text and "Other people see" not in r.text
    assert ews.ops() == []


async def test_an_automatic_reply_that_has_already_ended_is_refused_with_its_reason(rt, ews, invoke):
    r = await invoke(
        "save_automatic_reply",
        state="scheduled",
        internal_text="Away",
        start="2020-01-01 09:00",
        end="2020-01-02 09:00",
        user_confirmed=True,
    )
    assert r.is_error and "already past" in r.text
    assert ews.ops() == []


def test_a_candidate_carries_its_title():
    from exchange_mcp.core.resolve import Person

    assert (
        Person("Ann Lee", "ann@corp.example", "Mailbox", title="Buyer").short() == "Ann Lee <ann@corp.example>, Buyer"
    )
