"""people: colleagues in the address book, the person's own contacts, and rooms."""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any, Literal

from pydantic import Field

from exchange_mcp.core.content import name
from exchange_mcp.core.context import call
from exchange_mcp.core.errors import invalid
from exchange_mcp.core.project import clamp_limit, compact, page_result
from exchange_mcp.ews import people as ep
from exchange_mcp.registry import tool


def _row(f: ep.Found) -> dict[str, Any]:
    return compact(
        {
            "name": name(f.name),
            "address": f.address,
            "source": f.source,
            "kind": "distribution list" if f.group else None,
            "id": f.id,
            "title": name(f.title),
            "department": name(f.department),
            "company": name(f.company),
            "phone": name(f.phone),
            "room_list": name(f.room_list),
        }
    )


async def _with_details(c, rows: list) -> list:
    """The page's personal contacts with their own name, title and phone (QA D12, see ep.contact_details).
    A failed read is an error like any other (manifesto 12), not a list passed off as complete."""
    details = await ep.contact_details(c.ews, [f.id for f in rows if f.id])
    out = []
    for f in rows:
        d = details.get(f.id)
        out.append(replace(f, **{k: v for k, v in d.items() if v}) if d else f)
    return out


@tool(area="people", title="Find people and rooms", read_only=True, idempotent=True, returns_list=True)
async def list_people(
    query: Annotated[
        str | None, Field(description="Part of a name or an address. Optional only with source=rooms.", max_length=256)
    ] = None,
    source: Annotated[
        Literal["all", "directory", "contacts", "rooms"],
        Field(description="all: address book and own contacts; directory; contacts (own, with ids); rooms."),
    ] = "all",
    limit: Annotated[int | None, Field(description="How many, 1 to 25; 10 by default.")] = None,
    cursor: Annotated[str | None, Field(description="The cursor from the previous page.")] = None,
) -> dict[str, Any]:
    """Finds colleagues in the address book, people in the person's own contacts, and meeting rooms, with
    name, address, title, department and phone; each result says its source. Call it to find someone's
    address, to check who a name means before inviting or writing, or to find a contact's id for
    save_contact. Distribution lists are shown as such and never expanded into members. It reads only."""
    async with call("list_people") as c:
        n, extra = clamp_limit(limit)
        q = (query or "").strip()
        if not q and source != "rooms":
            raise invalid("Give a query: part of a name or an address. Only source=rooms lists without one.")
        scope = {"q": q, "s": source}
        start = (await c.cursor_in(cursor, scope) or {}).get("o", 0)
        if source == "contacts":
            # Live (2016 CU23): a Contains restriction on contact fields matches only a whole address;
            # ResolveNames over Contacts finds part of a name or an address, as Outlook does.
            found = await ep.resolve_names(c.ews, q, contacts_only=True)
            # One contact with three addresses resolves three times: one row per contact (QA D12).
            seen: set[str] = set()
            every = []
            for f in found:
                if f.source == "contacts" and (f.id or f.address) not in seen:
                    seen.add(f.id or f.address)
                    every.append(f)
            cut = getattr(found, "truncated", False)
        elif source == "rooms":
            every = await ep.rooms(c.ews)
            cut = getattr(every, "truncated", False)
            if q:
                every = [f for f in every if q.lower() in f.name.lower() or q.lower() in f.address.lower()]
        else:
            every = await ep.resolve_names(c.ews, q, directory_only=source == "directory")
            cut = getattr(every, "truncated", False)
        shown = every[start : start + n]
        if source == "contacts":
            shown = await _with_details(c, shown)
        rows = [_row(f) for f in shown]
        nxt = await c.cursor_out({"o": start + n} if start + n < len(every) else None, scope)
        out = page_result(rows, nxt, **extra)
        if cut:  # on every page: later pages cannot bring back what Exchange left out
            out["truncated"] = "Exchange returned only part of the matches; make the query more specific."
        return out
