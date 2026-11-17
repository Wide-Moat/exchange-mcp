"""No content in logs: a full flow with a password, addresses, subjects and tokens leaves none of them."""

from __future__ import annotations

import logging

from conftest import ok

from exchange_mcp.observability import DROPPED, ContentFilter, configure

SECRETS = ("pw-not-in-logs-1", "pat@corp.example", "Quarterly numbers", "alex@corp.example", "Hi there")


async def test_a_full_flow_logs_no_content(rt, ews, invoke, caplog):
    from test_mail import CREATED, TIPS, item, resolved

    caplog.set_level(logging.DEBUG)
    ews.on("ResolveNames", resolved("Pat", "pat@corp.example")).on("CreateItem", CREATED).on("GetItem", item())
    ews.on("GetMailTips", TIPS).on("SendItem", ok("SendItem")).on("FindFolder", 500)
    await invoke("save_draft", to=["pat@corp.example"], subject="Quarterly numbers", body="Hi there")
    await invoke("send_draft", id="D1", user_confirmed=True)
    await invoke("send_draft", id="D1", user_confirmed="yes")
    await invoke("list_messages", folder="Projects")  # fails upstream
    await invoke("save_draft", to=["nobody"], body="x")  # fails resolution
    text = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    for s in SECRETS:
        assert s not in text, s


def test_the_filter_drops_what_looks_like_content():
    f = ContentFilter()
    before = DROPPED["count"]
    for msg in (
        "sent to pat@corp.example",
        "Authorization: NTLM TlRMTVNTUAADAAAAGAAYAH",
        "Bearer eyJhbGciOiJSUzI1NiJ9.eyJ1cG4iOiJ4In0.sig",
        "token k1.QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB",
    ):
        assert not f.filter(logging.LogRecord("x", logging.INFO, __file__, 1, msg, (), None))
    assert DROPPED["count"] == before + 4
    assert f.filter(logging.LogRecord("x", logging.INFO, __file__, 1, "tool send_draft refused: confirm", (), None))


def test_configure_silences_url_loggers():
    configure()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert any(isinstance(fl, ContentFilter) for h in logging.getLogger().handlers for fl in h.filters)
