"""EWS request pieces every area uses: folder ids, paging, item ids, the draft marker, deletes and moves.

Only `exchange_mcp.ews` builds requests; tools call these functions and get plain Python back.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.soap import M, T, find, findall, text

WELL_KNOWN = {
    "inbox": "inbox",
    "sent": "sentitems",
    "drafts": "drafts",
    "deleted": "deleteditems",
    "junk": "junkemail",
    "outbox": "outbox",
    "archive": "archiveinbox",  # what a person means by "my archive": its inbox, not its empty root
    "root": "msgfolderroot",
    "calendar": "calendar",
    "contacts": "contacts",
    "tasks": "tasks",
}

# Marks a draft as made by this server (manifesto rule 24). A named property in our own property set.
DRAFT_PROPERTY_SET = "6e1f8a43-3c0b-4f5e-9d61-2b7a0c5e9f12"
DRAFT_PROPERTY_NAME = "ExchangeMcpDraft"
# Marks a draft that was saved with the person's signature ("1"), so an edit that replaces the body puts
# it back once and never twice (core/signature.py). A property survives Exchange's body conversion,
# which rewrites the HTML and drops the attributes a marker inside the body would need.
SIGNATURE_PROPERTY_NAME = "ExchangeMcpSignature"


def _children(el):
    return [] if el is None else list(el)


def draft_marker_uri() -> etree._Element:
    return T.ExtendedFieldURI(PropertySetId=DRAFT_PROPERTY_SET, PropertyName=DRAFT_PROPERTY_NAME, PropertyType="String")


def draft_marker() -> etree._Element:
    return T.ExtendedProperty(draft_marker_uri(), T.Value("1"))


def signature_marker_uri() -> etree._Element:
    return T.ExtendedFieldURI(
        PropertySetId=DRAFT_PROPERTY_SET, PropertyName=SIGNATURE_PROPERTY_NAME, PropertyType="String"
    )


def signature_marker(signed: bool) -> etree._Element:
    return T.ExtendedProperty(signature_marker_uri(), T.Value("1" if signed else "0"))


def folder_id(value: str) -> etree._Element:
    """A well-known name or an EWS folder id."""
    if value in WELL_KNOWN.values():
        return T.DistinguishedFolderId(Id=value)
    return T.FolderId(Id=value)


def item_ids(ids: list[str]) -> etree._Element:
    return M.ItemIds(*[T.ItemId(Id=i) for i in ids])


@dataclass
class Page:
    elements: list[etree._Element]
    total: int
    next_offset: int | None


def paged_view(offset: int, limit: int) -> etree._Element:
    return M.IndexedPageItemView(MaxEntriesReturned=str(limit), Offset=str(offset), BasePoint="Beginning")


def page_of(message: etree._Element, container_path: str, offset: int, limit: int) -> Page:
    root = find(message, "m:RootFolder")
    total = int(root.get("TotalItemsInView", "0")) if root is not None else 0
    last = (root.get("IncludesLastItemInRange", "true") == "true") if root is not None else True
    els = _children(find(root, container_path))
    nxt = None if last or not els else offset + len(els)
    return Page(els, total, nxt)


async def change_key(ews: Ews, item: str) -> str:
    """The current ChangeKey of an item: the model never sees or passes one."""
    messages = await ews.call(M.GetItem(M.ItemShape(T.BaseShape("IdOnly")), M.ItemIds(T.ItemId(Id=item))))
    found = findall(messages[0], "m:Items/*/t:ItemId")
    if not found:
        from exchange_mcp.core.errors import ExchangeError

        raise ExchangeError("not_found")
    return found[0].get("ChangeKey", "")


async def move_to_deleted(ews: Ews, ids: list[str]) -> None:
    """Deletion here is always a move to Deleted Items (manifesto rule 23)."""
    await ews.call(
        M.DeleteItem(
            item_ids(ids),
            DeleteType="MoveToDeletedItems",
            # Live (2016 CU23): a task refuses SpecifiedOccurrenceOnly (ErrorCannotDeleteTaskOccurrence).
            # The attribute matters for tasks only; a task is deleted whole, as Outlook does.
            AffectedTaskOccurrences="AllOccurrences",
            SendMeetingCancellations="SendToNone",
        )
    )


def mailbox(el: etree._Element | None) -> dict[str, str]:
    """A t:Mailbox as {name, address}."""
    if el is None:
        return {}
    return {"name": text(el, "t:Name"), "address": text(el, "t:EmailAddress")}


__all__ = [
    "Page",
    "WELL_KNOWN",
    "change_key",
    "draft_marker",
    "draft_marker_uri",
    "folder_id",
    "item_ids",
    "mailbox",
    "move_to_deleted",
    "page_of",
    "paged_view",
    "signature_marker",
    "signature_marker_uri",
]
