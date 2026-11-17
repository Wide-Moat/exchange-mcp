"""EWS for people and contacts: the address book, the person's own contacts, rooms, and saving contacts.

Distribution lists are reported as what they are and never expanded: there is no ExpandDL here.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from exchange_mcp.core.errors import ExchangeError, invalid
from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.soap import M, T, find, findall, text

GROUP_KINDS = frozenset({"PublicDL", "PrivateDL", "GroupMailbox"})
CONTACT_KINDS = frozenset({"Contact", "PrivateDL"})
EMAIL_KEYS = ("EmailAddress1", "EmailAddress2", "EmailAddress3")
PHONE_KEYS = ("BusinessPhone", "MobilePhone", "HomePhone", "OtherTelephone")
MAX_ROOM_LISTS = 50


class Partial(list):
    """A list that knows Exchange or this server left some entries out."""

    truncated = False


def partial(items: list, truncated: bool) -> list:
    out = Partial(items)
    out.truncated = truncated
    return out


@dataclass
class Found:
    name: str
    address: str
    source: str  # directory | contacts | rooms
    group: bool = False
    id: str = ""
    title: str = ""
    department: str = ""
    company: str = ""
    phone: str = ""
    room_list: str = ""
    kind: str = "Mailbox"  # the EWS MailboxType
    alias: str = ""


def _ok(response: etree._Element) -> None:
    """Room and people operations answer with ResponseClass on the response itself, not in a
    ResponseMessage the client checks: an Error there is still an error."""
    if response.get("ResponseClass") == "Error":
        code = text(response, "m:ResponseCode", "ErrorUnknown")
        if code == "ErrorAccessDenied":
            raise ExchangeError("forbidden", outcome="Nothing was read.")
        raise ExchangeError("failed", code=code, ref="people", outcome="Nothing was read.")


def _phone(el: etree._Element | None) -> str:
    for key in PHONE_KEYS[:2]:
        v = text(el, f"t:PhoneNumbers/t:Entry[@Key='{key}']")
        if v:
            return v
    return ""


async def resolve_names(
    ews: Ews, query: str, *, directory_only: bool = False, contacts_only: bool = False
) -> list[Found]:
    """ResolveNames over the address book and the person's contacts, or only one of them."""
    try:
        messages = await ews.call(
            M.ResolveNames(
                M.UnresolvedEntry(query),
                ReturnFullContactData="true",
                SearchScope="ActiveDirectory"
                if directory_only
                else "Contacts"
                if contacts_only
                else "ActiveDirectoryContacts",
            )
        )
    except ExchangeError as e:
        if e.key == "failed" and e.params.get("code") == "ErrorNameResolutionNoResults":
            return []
        raise
    out = []
    complete = True
    for m in messages:
        rs = find(m, "m:ResolutionSet")
        if rs is not None and rs.get("IncludesLastItemInRange", "true") != "true":
            complete = False
        for r in findall(m, "m:ResolutionSet/t:Resolution"):
            mb = find(r, "t:Mailbox")
            contact = find(r, "t:Contact")
            kind = text(mb, "t:MailboxType", "Mailbox")
            item = find(mb, "t:ItemId")
            source = "contacts" if kind in CONTACT_KINDS else "directory"
            found = Found(
                name=text(mb, "t:Name") or text(contact, "t:DisplayName"),
                address=text(mb, "t:EmailAddress"),
                source=source,
                group=kind in GROUP_KINDS,
                id=item.get("Id", "") if item is not None and source == "contacts" else "",
                title=text(contact, "t:JobTitle"),
                department=text(contact, "t:Department"),
                company=text(contact, "t:CompanyName"),
                phone=_phone(contact),
                kind=kind,
                alias=text(contact, "t:Alias"),
            )
            if found.address or found.id:
                out.append(found)
    return partial(out, not complete)


def _indexed(uri: str, key: str) -> etree._Element:
    return T.IndexedFieldURI(FieldURI=uri, FieldIndex=key)


async def rooms(ews: Ews) -> list[Found]:
    """Every room of every room list the organisation publishes."""
    messages = await ews.call(M.GetRoomLists())
    _ok(messages[0])
    every_list = [
        (text(a, "t:Name"), text(a, "t:EmailAddress")) for a in findall(find(messages[0], "m:RoomLists"), "t:Address")
    ]
    lists = every_list[:MAX_ROOM_LISTS]
    partial_lists = len(every_list) > MAX_ROOM_LISTS
    out: list[Found] = []
    seen: set[str] = set()
    for list_name, list_address in lists:
        if not list_address:
            continue
        answer = await ews.call(M.GetRooms(M.RoomList(T.EmailAddress(list_address))))
        _ok(answer[0])
        for room in findall(find(answer[0], "m:Rooms"), "t:Room"):
            rid = find(room, "t:Id")
            address = text(rid, "t:EmailAddress")
            if address and address.lower() not in seen:
                seen.add(address.lower())
                out.append(Found(name=text(rid, "t:Name"), address=address, source="rooms", room_list=list_name))
    return partial(out, partial_lists)


# ---------------------------------------------------------------- contacts


async def contact_change_key(ews: Ews, item: str) -> str:
    """The current ChangeKey of a personal contact; anything that is not a contact is refused."""
    messages = await ews.call(M.GetItem(M.ItemShape(T.BaseShape("IdOnly")), M.ItemIds(T.ItemId(Id=item))))
    found = findall(messages[0], "m:Items/*")
    if not found:
        raise ExchangeError("not_found")
    if etree.QName(found[0]).localname != "Contact":
        raise invalid(
            "That id is not a personal contact. Find contacts with list_people source=contacts. Nothing was done."
        )
    return found[0].find("{*}ItemId").get("ChangeKey", "")


@dataclass
class ContactFields:
    """None leaves a field as it is; an empty list clears every slot of its kind."""

    name: str | None = None
    notes: str | None = None
    company: str | None = None
    emails: list[str] | None = None
    phones: list[str] | None = None
    job_title: str | None = None


def _split(name: str) -> tuple[str, str]:
    parts = name.strip().rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def _contact(f: ContactFields) -> etree._Element:
    """A t:Contact in schema order: Body (ItemType), FileAs, DisplayName, GivenName, CompanyName,
    EmailAddresses, PhoneNumbers, JobTitle, Surname."""
    c = T.Contact()
    if f.notes is not None:
        c.append(T.Body(f.notes, BodyType="Text"))
    given = surname = None
    if f.name is not None:
        given, surname = _split(f.name)
        c.append(T.FileAs(f.name))
        c.append(T.DisplayName(f.name))
        c.append(T.GivenName(given))
    if f.company is not None:
        c.append(T.CompanyName(f.company))
    if f.emails:
        c.append(T.EmailAddresses(*[T.Entry(a, Key=k) for k, a in zip(EMAIL_KEYS, f.emails, strict=False)]))
    if f.phones:
        c.append(T.PhoneNumbers(*[T.Entry(p, Key=k) for k, p in zip(PHONE_KEYS, f.phones, strict=False)]))
    if f.job_title is not None:
        c.append(T.JobTitle(f.job_title))
    if surname:
        c.append(T.Surname(surname))
    return c


async def create_contact(ews: Ews, f: ContactFields) -> str:
    messages = await ews.call(
        M.CreateItem(M.SavedItemFolderId(T.DistinguishedFolderId(Id="contacts")), M.Items(_contact(f)))
    )
    ids = findall(messages[0], "m:Items/*/t:ItemId")
    if not ids:
        raise ExchangeError("uncertain", ref="create-without-id")
    return ids[0].get("Id")


def _set(uri: str, el: etree._Element) -> etree._Element:
    return T.SetItemField(T.FieldURI(FieldURI=uri), T.Contact(el))


def _slots(uri: str, container: str, keys: tuple[str, ...], values: list[str]) -> list[etree._Element]:
    out = []
    for i, key in enumerate(keys):
        if i < len(values):
            out.append(
                T.SetItemField(_indexed(uri, key), T.Contact(getattr(T, container)(T.Entry(values[i], Key=key))))
            )
        else:
            out.append(T.DeleteItemField(_indexed(uri, key)))
    return out


async def update_contact(ews: Ews, item: str, ck: str, f: ContactFields) -> None:
    updates: list[etree._Element] = []
    if f.notes is not None:
        updates.append(_set("item:Body", T.Body(f.notes, BodyType="Text")))
    if f.name is not None:
        given, surname = _split(f.name)
        updates.append(_set("contacts:FileAs", T.FileAs(f.name)))
        updates.append(_set("contacts:DisplayName", T.DisplayName(f.name)))
        updates.append(_set("contacts:GivenName", T.GivenName(given)))
        if surname:
            updates.append(_set("contacts:Surname", T.Surname(surname)))
        else:
            updates.append(T.DeleteItemField(T.FieldURI(FieldURI="contacts:Surname")))
    if f.company is not None:
        updates.append(_set("contacts:CompanyName", T.CompanyName(f.company)))
    if f.emails is not None:
        updates.extend(_slots("contacts:EmailAddress", "EmailAddresses", EMAIL_KEYS, f.emails))
    if f.phones is not None:
        updates.extend(_slots("contacts:PhoneNumber", "PhoneNumbers", PHONE_KEYS, f.phones))
    if f.job_title is not None:
        updates.append(_set("contacts:JobTitle", T.JobTitle(f.job_title)))
    if not updates:
        return
    await ews.call(
        M.UpdateItem(
            M.ItemChanges(T.ItemChange(T.ItemId(Id=item, ChangeKey=ck), T.Updates(*updates))),
            ConflictResolution="AutoResolve",
        )
    )


__all__ = [
    "EMAIL_KEYS",
    "PHONE_KEYS",
    "ContactFields",
    "Found",
    "contact_change_key",
    "create_contact",
    "resolve_names",
    "rooms",
    "update_contact",
]
