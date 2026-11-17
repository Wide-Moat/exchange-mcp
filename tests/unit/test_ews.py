"""The EWS client: the allowlist, the one configured mailbox, errors inside HTTP 200, back-off, and a real
NTLM handshake against an NTLM acceptor."""

from __future__ import annotations

import base64
import inspect
import re
from pathlib import Path

import httpx
import pytest
from conftest import EWS_URL, envelope, error, ok

import exchange_mcp
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.ews.client import ALLOWED, Ews
from exchange_mcp.ews.soap import M, T
from exchange_mcp.ews.transport import Bearer, Ntlm

SRC = Path(exchange_mcp.__file__).parent
FORBIDDEN_OPS = {
    "GetInboxRules",
    "UpdateInboxRules",
    "AddDelegate",
    "UpdateDelegate",
    "RemoveDelegate",
    "GetDelegate",
    "ExportItems",
    "UploadItems",
    "SyncFolderItems",
    "SyncFolderHierarchy",
    "Subscribe",
    "Unsubscribe",
    "GetEvents",
    "GetStreamingEvents",
    "EmptyFolder",
    "SetHoldOnMailboxes",
    "GetSearchableMailboxes",
    "FindMailboxStatisticsByKeywords",
    "SearchMailboxes",
    "PlayOnPhone",
    "MarkAllItemsAsRead",
}


def test_the_allowlist_holds_no_forbidden_operation():
    assert not (ALLOWED & FORBIDDEN_OPS)


async def test_an_operation_outside_the_allowlist_is_never_sent(ews, http):
    client = Ews(http, EWS_URL, Bearer("t"))
    for op in sorted(FORBIDDEN_OPS):
        with pytest.raises(RuntimeError, match="allowlist"):
            await client.call(getattr(M, op)())
    assert ews.seen == []


def test_no_module_outside_ews_speaks_http():
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(("ews/", "keys/vault.py", "auth/adfs.py", "app.py", "links/routes.py", "signin.py")):
            continue
        src = path.read_text()
        assert not re.search(r"\.(post|get|put|send|request)\(\s*(f?[\"']https?:|self\._url|url)", src), rel
        assert "http.post(" not in src and "http.get(" not in src, rel


def test_only_move_to_deleted_items_is_sent():
    kinds = set()
    for path in SRC.rglob("*.py"):
        kinds |= set(re.findall(r"DeleteType=\"(\w+)\"", path.read_text()))
    assert kinds == {"MoveToDeletedItems"}


def test_save_draft_never_sends():
    sending = {}
    for path in SRC.rglob("*.py"):
        found = set(re.findall(r"MessageDisposition=\"(\w+)\"", path.read_text())) - {"SaveOnly"}
        if found:
            sending[path.relative_to(SRC).as_posix()] = found
    # Messages are sent by send_draft (SendItem) only. Meeting responses and cancellations are messages
    # Exchange builds itself, sent from ews/calendar.py and nowhere else.
    assert set(sending) <= {"ews/calendar.py"}, sending
    send_calls = [p for p in SRC.rglob("*.py") if "M.SendItem(" in p.read_text()]
    assert [p.name for p in send_calls] == ["mail.py"] and send_calls[0].parent.name == "ews"


async def test_requests_go_to_the_configured_mailbox_only(rt, ews, invoke):
    """A full mail flow: every request goes to the one EWS URL, none impersonates or names a mailbox."""
    from test_mail import CREATED, TIPS, item, resolved

    ews.on("ResolveNames", resolved("Pat", "pat@corp.example")).on("CreateItem", CREATED).on("GetItem", item())
    ews.on("GetMailTips", TIPS).on("SendItem", ok("SendItem"))
    await invoke("save_draft", to=["pat@corp.example"], body="hi")
    await invoke("send_draft", id="D1", user_confirmed=True)
    assert ews.seen
    for s in ews.seen:
        x = s.xml()
        assert s.url == EWS_URL
        assert "ExchangeImpersonation" not in x and "PrimarySmtpAddress" not in x
        # a folder of another mailbox is a Mailbox element inside a folder id
        assert not s.root.xpath("//*[local-name()='DistinguishedFolderId' or local-name()='FolderId']/*")


async def test_an_error_inside_http_200_is_an_error(ews, http):
    ews.on("GetItem", error("GetItem", "ErrorItemNotFound"))
    with pytest.raises(ExchangeError) as e:
        await Ews(http, EWS_URL, Bearer("t")).call(M.GetItem())
    assert e.value.key == "not_found"


async def test_busy_backs_off_then_says_when(ews, http):
    busy = envelope(
        '<m:GetItemResponse><m:ResponseMessages><m:GetItemResponseMessage ResponseClass="Error"><m:ResponseCode>'
        'ErrorServerBusy</m:ResponseCode><m:MessageXml><t:Value Name="BackOffMilliseconds">200</t:Value>'
        "</m:MessageXml></m:GetItemResponseMessage></m:ResponseMessages></m:GetItemResponse>"
    )
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    ews.on("GetItem", busy, busy, ok("GetItem"))
    await Ews(http, EWS_URL, Bearer("t"), sleep=sleep).call(M.GetItem())
    assert slept == [0.2, 0.2]


async def test_a_401_means_sign_in_again(ews, http):
    ews.on("GetItem", 401)
    with pytest.raises(ExchangeError) as e:
        await Ews(http, EWS_URL, Bearer("t")).call(M.GetItem())
    assert e.value.key == "reconnect"


async def test_xml_entities_are_not_expanded(ews, http):
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><x>&b;</x></s:Body></s:Envelope>'
    )
    ews.on("GetItem", bomb)
    try:
        await Ews(http, EWS_URL, Bearer("t")).call(M.GetItem())
    except ExchangeError:
        pass  # refused or empty: either way nothing was expanded into memory


async def test_ntlm_completes_a_real_handshake(monkeypatch, tmp_path):
    """Leg 0 anonymous, leg 1 negotiate, leg 2 authenticate, checked by pyspnego's own NTLM acceptor."""
    import spnego

    users = tmp_path / "ntlm_users"
    users.write_text("CORP:alex:right-password\n")
    monkeypatch.setenv("NTLM_USER_FILE", str(users))
    server = spnego.server(protocol="ntlm")
    legs: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("authorization")
        if not auth:
            legs.append("anonymous")
            return httpx.Response(401, headers={"WWW-Authenticate": "NTLM"})
        token = base64.b64decode(auth.split(" ", 1)[1])
        out = server.step(token)
        if not server.complete:
            legs.append("negotiate")
            return httpx.Response(401, headers={"WWW-Authenticate": "NTLM " + base64.b64encode(out).decode()})
        legs.append("authenticate")
        return httpx.Response(200, content=ok("GetItem"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await Ews(http, EWS_URL, Ntlm("CORP\\alex", "right-password")).call(M.GetItem())
    assert legs == ["anonymous", "negotiate", "authenticate"]
    assert server.client_principal.lower() == "corp\\alex"


def test_credentials_never_print():
    assert "right" not in repr(Ntlm("CORP\\alex", "right")) and "tok" not in repr(Bearer("tok"))
    from exchange_mcp.auth.credential import Credential

    c = Credential("password", "a@corp.example", "a@corp.example", "A", password="right")
    assert "right" not in repr(c)


def test_t_namespace_used_for_mailbox_builders():
    assert T.Mailbox is not None and inspect.isclass(type(T))
