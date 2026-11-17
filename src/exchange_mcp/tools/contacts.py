"""contacts: the person's own contacts, created, changed and deleted. They are found with list_people."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from exchange_mcp.core.context import call
from exchange_mcp.core.errors import invalid
from exchange_mcp.core.project import compact
from exchange_mcp.core.resolve import ADDRESS
from exchange_mcp.ews import people as ep
from exchange_mcp.ews.common import move_to_deleted
from exchange_mcp.registry import tool

ID = Annotated[str, Field(description="A contact id from list_people source=contacts.", min_length=1, max_length=512)]
SLOTS = (
    "A contact holds at most 3 e-mail addresses and 4 phone numbers in Exchange; this has {e} and {p}. "
    "Nothing was saved."
)


@tool(area="contacts", title="Save a contact")
async def save_contact(
    id: Annotated[
        str | None, Field(description="A contact id to change it; absent creates a contact.", max_length=512)
    ] = None,
    name: Annotated[str | None, Field(description="Full name; required for a new contact.", max_length=256)] = None,
    emails: Annotated[
        list[str] | None, Field(description="Up to 3 addresses; given on a change, they replace all.")
    ] = None,
    phones: Annotated[
        list[str] | None,
        Field(description="Up to 4 numbers, in order business, mobile, home, other; given, they replace all."),
    ] = None,
    company: str | None = None,
    job_title: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Creates a contact in the person's own Contacts folder, or changes one found with list_people
    source=contacts. Call it when the person wants to keep someone's address or phone number. Fields left
    out stay as they are; nothing is sent to anyone. Exchange keeps at most 3 addresses and 4 phones."""
    async with call("save_contact") as c:
        if len(emails or []) > len(ep.EMAIL_KEYS) or len(phones or []) > len(ep.PHONE_KEYS):
            raise invalid(SLOTS.format(e=len(emails or []), p=len(phones or [])))
        for a in emails or []:
            if not ADDRESS.match(a.strip()):
                raise invalid("Each of emails is one e-mail address. Nothing was saved.")
        if any(not (x or "").strip() for x in [*(emails or []), *(phones or [])]):
            raise invalid("An address or a phone number cannot be empty; leave it out instead. Nothing was saved.")
        if name is not None and not name.strip():
            raise invalid("name cannot be empty. Nothing was saved.")
        fields = ep.ContactFields(
            name=name.strip() if name else None,
            notes=notes,
            company=company,
            emails=[a.strip() for a in emails] if emails is not None else None,
            phones=[p.strip() for p in phones] if phones is not None else None,
            job_title=job_title,
        )
        if id is None:
            if not name:
                raise invalid("A new contact needs name. Nothing was saved.")
            cid = await ep.create_contact(c.ews, fields)
            return compact({"saved": "created", "id": cid, "name": fields.name})
        ck = await ep.contact_change_key(c.ews, id)
        await ep.update_contact(c.ews, id, ck, fields)
        return compact({"saved": "updated", "id": id})


@tool(area="contacts", title="Delete a contact")
async def delete_contact(id: ID) -> dict[str, Any]:
    """Moves one of the person's own contacts to Deleted Items. Call it when the person asks to remove a
    contact found with list_people source=contacts. It refuses ids that are not contacts; the contact can be
    moved back from Deleted Items until that folder is emptied."""
    async with call("delete_contact") as c:
        await ep.contact_change_key(c.ews, id)
        await move_to_deleted(c.ews, [id])
        return {"deleted": id, "where": "Deleted Items"}
