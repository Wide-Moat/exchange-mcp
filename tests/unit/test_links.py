"""Sealed links: bound to a person, short-lived, closed to tampering, bounded in size."""

from __future__ import annotations

import os

import httpx
from conftest import PUBLIC, base_env, ok

from exchange_mcp.app import build
from exchange_mcp.config import Settings
from exchange_mcp.keys.seal import LocalSealer
from exchange_mcp.links.links import MAX_BODY_BYTES, safe_name


def app_for(ews, sealer):
    settings = Settings.from_env(base_env())
    app, _, _ = build(settings, httpx.AsyncClient(transport=httpx.MockTransport(ews.handler)), sealer)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC)


async def test_an_oversized_body_upload_is_refused(ews, cred):
    sealer = LocalSealer([("k1", os.urandom(32))])
    client = app_for(ews, sealer)
    from exchange_mcp.auth.credential import person_key
    from exchange_mcp.core import context

    link = await context.runtime().links.upload(cred, person_key(cred.login), "D1", "body")
    r = await client.put(link["url"].replace(PUBLIC, ""), content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413
    assert ews.ops() == []


async def test_a_tampered_or_foreign_link_is_refused(ews, cred):
    sealer = LocalSealer([("k1", os.urandom(32))])
    client = app_for(ews, sealer)
    from exchange_mcp.auth.credential import person_key
    from exchange_mcp.core import context

    links = context.runtime().links
    good = await links.download(cred, person_key(cred.login), "A1", "a.pdf", 3)
    path = good["url"].replace(PUBLIC, "")
    assert (await client.get(path[:-3] + "AAA")).status_code == 403
    other = await links.download(cred, "someone-else", "A1", "a.pdf", 3)  # person does not match credential
    assert (await client.get(other["url"].replace(PUBLIC, ""))).status_code == 403
    upload = await links.upload(cred, person_key(cred.login), "D1", "attach", "a.pdf")
    assert (await client.get(upload["url"].replace("/files/u/", "/files/d/").replace(PUBLIC, ""))).status_code == 403
    assert ews.ops() == []


async def test_a_download_streams_the_attachment(ews, cred):
    import base64

    sealer = LocalSealer([("k1", os.urandom(32))])
    client = app_for(ews, sealer)
    from exchange_mcp.auth.credential import person_key
    from exchange_mcp.core import context

    ews.on(
        "GetAttachment",
        ok(
            "GetAttachment",
            "<m:Attachments><t:FileAttachment><t:Name>r.pdf</t:Name><t:ContentType>application/pdf"
            f"</t:ContentType><t:Content>{base64.b64encode(b'%PDF-1').decode()}</t:Content></t:FileAttachment></m:Attachments>",
        ),
    )
    link = await context.runtime().links.download(cred, person_key(cred.login), "A1", "../r.pdf", 6)
    r = await client.get(link["url"].replace(PUBLIC, ""))
    assert r.status_code == 200 and r.content == b"%PDF-1"
    assert 'filename="r.pdf"' in r.headers["content-disposition"]


def test_names_from_mail_never_become_paths_or_options():
    assert safe_name("../../.bashrc") == "file"
    assert safe_name("-rf") == "file"
    assert safe_name("a/b/report's.pdf") == "reports.pdf"
    assert safe_name("$(rm -rf).pdf") == "(rm -rf).pdf"
