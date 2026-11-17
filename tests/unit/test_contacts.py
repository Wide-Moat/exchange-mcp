"""contacts: what save_contact and delete_contact send to EWS, and every refusal in docs/requirements.md."""

from __future__ import annotations

import pytest
from conftest import ok


def got(kind: str = "Contact") -> bytes:
    return ok("GetItem", f'<m:Items><t:{kind}><t:ItemId Id="C1" ChangeKey="CK1"/></t:{kind}></m:Items>')


CREATED = ok("CreateItem", '<m:Items><t:Contact><t:ItemId Id="C9" ChangeKey="K"/></t:Contact></m:Items>')


@pytest.fixture
def save(server):
    """save_contact through the server; its `name` argument collides with the invoke fixture's own."""
    from conftest import Result
    from mcp.types import CallToolRequestParams

    async def _save(**args):
        r = await server._handle_call_tool(None, CallToolRequestParams(name="save_contact", arguments=args))
        return Result(bool(r.is_error), " ".join(getattr(c, "text", "") for c in r.content), r.structured_content)

    return _save


def children(el) -> list[str]:
    return [c.tag.split("}")[1] for c in el]


async def test_more_slots_than_exchange_has_are_refused(rt, ews, invoke, save):
    r = await save(name="Pat", emails=[f"p{i}@corp.example" for i in range(4)])
    assert r.is_error and "at most 3 e-mail addresses and 4 phone numbers" in r.text and "Nothing was saved" in r.text
    r = await save(name="Pat", phones=[str(i) for i in range(5)])
    assert r.is_error and "at most 3 e-mail addresses and 4 phone numbers" in r.text
    assert ews.ops() == []


async def test_a_non_contact_id_is_refused(rt, ews, invoke, save):
    ews.on("GetItem", got("Message"))
    r = await invoke("delete_contact", id="C1")
    assert r.is_error and "not a personal contact" in r.text
    r = await save(id="C1", name="Pat")
    assert r.is_error and "not a personal contact" in r.text
    assert ews.writes() == []


async def test_a_new_contact_is_written_in_schema_order(rt, ews, invoke, save):
    ews.on("CreateItem", CREATED)
    r = await save(
        name="Pat Doe",
        emails=["pat@corp.example", "pat@home.example"],
        phones=["+1 555 0100", "+1 555 0199"],
        company="Corp",
        job_title="Engineer",
        notes="Met at the fair",
    )
    assert not r.is_error, r.text
    create = ews.last("CreateItem")
    assert 'Id="contacts"' in create.xml()
    contact = create.body.find(".//{*}Contact")
    assert children(contact) == [
        "Body",
        "FileAs",
        "DisplayName",
        "GivenName",
        "CompanyName",
        "EmailAddresses",
        "PhoneNumbers",
        "JobTitle",
        "Surname",
    ]
    keys = [e.get("Key") for e in contact.find("{*}PhoneNumbers")]
    assert keys == ["BusinessPhone", "MobilePhone"]
    assert r.data == {"saved": "created", "id": "C9", "name": "Pat Doe"}


async def test_an_update_reads_the_change_key_and_clears_unused_slots(rt, ews, invoke, save):
    ews.on("GetItem", got()).on("UpdateItem", ok("UpdateItem"))
    r = await save(id="C1", emails=["pat@corp.example"])
    assert not r.is_error, r.text
    assert ews.ops() == ["GetItem", "UpdateItem"]
    update = ews.last("UpdateItem")
    assert update.body.get("ConflictResolution") == "AutoResolve" and 'ChangeKey="CK1"' in update.xml()
    updates = update.body.find(".//{*}Updates")
    assert children(updates) == ["SetItemField", "DeleteItemField", "DeleteItemField"]


async def test_delete_contact_moves_to_deleted_items(rt, ews, invoke, save):
    ews.on("GetItem", got()).on("DeleteItem", ok("DeleteItem"))
    r = await invoke("delete_contact", id="C1")
    assert not r.is_error, r.text
    assert ews.last("DeleteItem").body.get("DeleteType") == "MoveToDeletedItems"
