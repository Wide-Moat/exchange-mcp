"""EWS for mail: find, read, move, flag, draft, send, folders, automatic replies, MailTips.

Functions here build the request, call EWS and return plain values. They decide nothing about the
person's word or the bucket: the tools do that before calling them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.common import (
    DRAFT_PROPERTY_NAME,
    Page,
    draft_marker,
    draft_marker_uri,
    folder_id,
    item_ids,
    mailbox,
    page_of,
    paged_view,
)
from exchange_mcp.ews.soap import M, T, find, findall, flag, text

LIST_FIELDS = (
    "item:Subject",
    "item:DateTimeReceived",
    "item:DisplayTo",
    "item:HasAttachments",
    "item:Importance",
    "item:Categories",
    "item:Preview",
    "item:Flag",
    "item:ConversationId",
    "item:ParentFolderId",
    "message:From",
    "message:IsRead",
)


def _fields(*uris: str) -> etree._Element:
    return T.AdditionalProperties(*[T.FieldURI(FieldURI=u) for u in uris])


@dataclass
class Listing:
    id: str
    subject: str
    received: str
    sender: dict[str, str]
    to_display: str
    preview: str
    unread: bool
    flag: str
    categories: list[str]
    importance: str
    has_attachments: bool
    conversation: str
    folder: str


def _listing(el: etree._Element) -> Listing:
    return Listing(
        id=el.find("{*}ItemId").get("Id"),
        subject=text(el, "t:Subject"),
        received=text(el, "t:DateTimeReceived"),
        sender=mailbox(find(el, "t:From/t:Mailbox")),
        to_display=text(el, "t:DisplayTo"),
        preview=text(el, "t:Preview"),
        unread=not flag(el, "t:IsRead"),
        flag=text(el, "t:Flag/t:FlagStatus", "NotFlagged"),
        categories=[c.text or "" for c in findall(el, "t:Categories/t:String")],
        importance=text(el, "t:Importance", "Normal"),
        has_attachments=flag(el, "t:HasAttachments"),
        conversation=(find(el, "t:ConversationId").get("Id") if find(el, "t:ConversationId") is not None else ""),
        folder=(find(el, "t:ParentFolderId").get("Id") if find(el, "t:ParentFolderId") is not None else ""),
    )


def _restriction(
    unread_only: bool, since: str | None, until: str | None, has_attachments: bool
) -> etree._Element | None:
    terms = []
    if unread_only:
        terms.append(
            T.IsEqualTo(T.FieldURI(FieldURI="message:IsRead"), T.FieldURIOrConstant(T.Constant(Value="false")))
        )
    if since:
        terms.append(
            T.IsGreaterThanOrEqualTo(
                T.FieldURI(FieldURI="item:DateTimeReceived"), T.FieldURIOrConstant(T.Constant(Value=since))
            )
        )
    if until:
        terms.append(
            T.IsLessThan(T.FieldURI(FieldURI="item:DateTimeReceived"), T.FieldURIOrConstant(T.Constant(Value=until)))
        )
    if has_attachments:
        terms.append(
            T.IsEqualTo(T.FieldURI(FieldURI="item:HasAttachments"), T.FieldURIOrConstant(T.Constant(Value="true")))
        )
    if not terms:
        return None
    return M.Restriction(terms[0] if len(terms) == 1 else T.And(*terms))


async def find_messages(
    ews: Ews,
    folder: str,
    *,
    offset: int,
    limit: int,
    aqs: str | None,
    unread_only: bool,
    since: str | None,
    until: str | None,
    has_attachments: bool,
) -> tuple[list[Listing], Page]:
    body = M.FindItem(
        M.ItemShape(T.BaseShape("IdOnly"), _fields(*LIST_FIELDS)),
        paged_view(offset, limit),
        Traversal="Shallow",
    )
    if aqs:
        # AQS and a Restriction cannot be combined: with a query, every filter travels in the query
        # except unread, which the tool applies to the page (the page may come back shorter).
        body.append(M.SortOrder(T.FieldOrder(T.FieldURI(FieldURI="item:DateTimeReceived"), Order="Descending")))
        body.append(M.ParentFolderIds(folder_id(folder)))
        body.append(M.QueryString(aqs))
    else:
        r = _restriction(unread_only, since, until, has_attachments)
        if r is not None:
            body.append(r)
        body.append(M.SortOrder(T.FieldOrder(T.FieldURI(FieldURI="item:DateTimeReceived"), Order="Descending")))
        body.append(M.ParentFolderIds(folder_id(folder)))
    messages = await ews.call(body)
    page = page_of(messages[0], "t:Items", offset, limit)
    return [_listing(e) for e in page.elements], page


@dataclass
class Attachment:
    id: str
    name: str
    size: int
    content_type: str
    inline: bool
    kind: str  # file | item


@dataclass
class Message:
    id: str
    change_key: str
    subject: str
    received: str
    sent: str
    sender: dict[str, str]
    to: list[dict[str, str]]
    cc: list[dict[str, str]]
    bcc: list[dict[str, str]]
    html: str
    unread: bool
    is_draft: bool
    ours: bool
    flag: str
    categories: list[str]
    importance: str
    conversation: str
    attachments: list[Attachment] = field(default_factory=list)


def _recipients(el: etree._Element, path: str) -> list[dict[str, str]]:
    return [mailbox(m) for m in findall(el, f"{path}/t:Mailbox")]


def _message(el: etree._Element) -> Message:
    iid = el.find("{*}ItemId")
    atts = []
    for a in findall(el, "t:Attachments/*"):
        aid = a.find("{*}AttachmentId")
        atts.append(
            Attachment(
                id=aid.get("Id") if aid is not None else "",
                name=text(a, "t:Name", "attachment"),
                size=int(text(a, "t:Size", "0") or 0),
                content_type=text(a, "t:ContentType", "application/octet-stream"),
                inline=flag(a, "t:IsInline"),
                kind="file" if etree.QName(a).localname == "FileAttachment" else "item",
            )
        )
    ours = (
        any(p.get("PropertyName") == DRAFT_PROPERTY_NAME for p in el.iter("{*}ExtendedFieldURI"))
        and text(el, "t:ExtendedProperty/t:Value") == "1"
    )
    conv = find(el, "t:ConversationId")
    return Message(
        id=iid.get("Id"),
        change_key=iid.get("ChangeKey", ""),
        subject=text(el, "t:Subject"),
        received=text(el, "t:DateTimeReceived"),
        sent=text(el, "t:DateTimeSent"),
        sender=mailbox(find(el, "t:From/t:Mailbox")),
        to=_recipients(el, "t:ToRecipients"),
        cc=_recipients(el, "t:CcRecipients"),
        bcc=_recipients(el, "t:BccRecipients"),
        html=text(el, "t:Body"),
        unread=not flag(el, "t:IsRead"),
        is_draft=flag(el, "t:IsDraft"),
        ours=ours,
        flag=text(el, "t:Flag/t:FlagStatus", "NotFlagged"),
        categories=[c.text or "" for c in findall(el, "t:Categories/t:String")],
        importance=text(el, "t:Importance", "Normal"),
        conversation=conv.get("Id") if conv is not None else "",
        attachments=atts,
    )


async def get_message(ews: Ews, item: str, *, body: bool = True) -> Message:
    uris = [
        "item:Subject",
        "item:DateTimeReceived",
        "item:DateTimeSent",
        "item:IsDraft",
        "item:Attachments",
        "item:Categories",
        "item:Importance",
        "item:Flag",
        "item:ConversationId",
        "message:From",
        "message:ToRecipients",
        "message:CcRecipients",
        "message:BccRecipients",
        "message:IsRead",
    ]
    if body:
        uris.append("item:Body")
    shape = M.ItemShape(T.BaseShape("IdOnly"), T.BodyType("HTML"), _fields(*uris))
    shape[-1].append(draft_marker_uri())
    messages = await ews.call(M.GetItem(shape, M.ItemIds(T.ItemId(Id=item))))
    found = findall(messages[0], "m:Items/*")
    if not found:
        from exchange_mcp.core.errors import ExchangeError

        raise ExchangeError("not_found")
    return _message(found[0])


async def conversation(ews: Ews, conversation_id: str, limit: int) -> list[Listing]:
    messages = await ews.call(
        M.GetConversationItems(
            M.ItemProperties(T.BaseShape("IdOnly"), _fields(*LIST_FIELDS)),
            M.FoldersToIgnore(T.DistinguishedFolderId(Id="deleteditems"), T.DistinguishedFolderId(Id="drafts")),
            M.MaxItemsToReturn(str(limit)),
            M.SortOrder("DateOrderDescending"),
            M.Conversations(T.Conversation(T.ConversationId(Id=conversation_id))),
        )
    )
    out = []
    for item in findall(messages[0], "m:Conversation/t:ConversationNodes/t:ConversationNode/t:Items/*"):
        out.append(_listing(item))
    return out[:limit]


async def attachment_bytes(ews: Ews, attachment_id: str) -> tuple[str, str, bytes]:
    import base64

    messages = await ews.call(M.GetAttachment(M.AttachmentIds(T.AttachmentId(Id=attachment_id))))
    att = find(messages[0], "m:Attachments/t:FileAttachment")
    if att is None:
        from exchange_mcp.core.errors import ExchangeError

        raise ExchangeError("not_found")
    return (
        text(att, "t:Name", "attachment"),
        text(att, "t:ContentType", "application/octet-stream"),
        base64.b64decode(text(att, "t:Content")),
    )


# ---------------------------------------------------------------- changes


async def move(ews: Ews, ids: list[str], to_folder: str) -> list[str]:
    messages = await ews.call(M.MoveItem(M.ToFolderId(folder_id(to_folder)), item_ids(ids)))
    return [e.get("Id") for m in messages for e in findall(m, "m:Items/*/t:ItemId")]


async def set_fields(ews: Ews, ids: list[str], updates: list[etree._Element]) -> None:
    """Set fields on items the person owns. AlwaysOverwrite: flags, read state and categories carry no
    content another writer could lose."""
    changes = [T.ItemChange(T.ItemId(Id=i), T.Updates(*[_copy(u) for u in updates])) for i in ids]
    await ews.call(
        M.UpdateItem(M.ItemChanges(*changes), ConflictResolution="AlwaysOverwrite", MessageDisposition="SaveOnly")
    )


def _copy(el: etree._Element) -> etree._Element:
    from copy import deepcopy

    return deepcopy(el)


def set_read(read: bool) -> etree._Element:
    return T.SetItemField(T.FieldURI(FieldURI="message:IsRead"), T.Message(T.IsRead("true" if read else "false")))


def set_flag(status: str) -> etree._Element:
    return T.SetItemField(T.FieldURI(FieldURI="item:Flag"), T.Message(T.Flag(T.FlagStatus(status))))


def set_categories(names: list[str]) -> etree._Element:
    if not names:
        return T.DeleteItemField(T.FieldURI(FieldURI="item:Categories"))
    return T.SetItemField(
        T.FieldURI(FieldURI="item:Categories"), T.Message(T.Categories(*[T.String(n) for n in names]))
    )


async def categories_of(ews: Ews, ids: list[str]) -> dict[str, list[str]]:
    messages = await ews.call(M.GetItem(M.ItemShape(T.BaseShape("IdOnly"), _fields("item:Categories")), item_ids(ids)))
    out = {}
    for m in messages:
        for it in findall(m, "m:Items/*"):
            out[it.find("{*}ItemId").get("Id")] = [c.text or "" for c in findall(it, "t:Categories/t:String")]
    return out


# ---------------------------------------------------------------- drafts


def _mailboxes(tag: str, addresses: list[str]) -> etree._Element:
    return getattr(T, tag)(*[T.Mailbox(T.EmailAddress(a)) for a in addresses])


async def create_draft(
    ews: Ews,
    *,
    subject: str,
    html: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    importance: str,
) -> tuple[str, str]:
    msg = T.Message(T.Subject(subject), T.Body(html, BodyType="HTML"), T.Importance(importance), draft_marker())
    # Element order follows the EWS schema: Subject, Body, Importance, ExtendedProperty, ..., recipients.
    for tag, addrs in (("ToRecipients", to), ("CcRecipients", cc), ("BccRecipients", bcc)):
        if addrs:
            msg.append(_mailboxes(tag, addrs))
    messages = await ews.call(
        M.CreateItem(
            M.SavedItemFolderId(T.DistinguishedFolderId(Id="drafts")), M.Items(msg), MessageDisposition="SaveOnly"
        )
    )
    iid = findall(messages[0], "m:Items/*/t:ItemId")[0]
    return iid.get("Id"), iid.get("ChangeKey", "")


async def create_response(
    ews: Ews, *, mode: str, reference: str, html: str, to: list[str], cc: list[str], bcc: list[str]
) -> tuple[str, str]:
    """A reply, reply-all or forward created by Exchange, so the thread and quoted history stay (rule 33).
    The new text goes above the history; the marker is set right after."""
    tag = {"reply": "ReplyToItem", "reply_all": "ReplyAllToItem", "forward": "ForwardItem"}[mode]
    ck = await _ck(ews, reference)
    resp = getattr(T, tag)()
    for name, addrs in (("ToRecipients", to), ("CcRecipients", cc), ("BccRecipients", bcc)):
        if addrs:
            resp.append(_mailboxes(name, addrs))
    resp.append(T.ReferenceItemId(Id=reference, ChangeKey=ck))
    resp.append(T.NewBodyContent(html, BodyType="HTML"))
    messages = await ews.call(
        M.CreateItem(
            M.SavedItemFolderId(T.DistinguishedFolderId(Id="drafts")), M.Items(resp), MessageDisposition="SaveOnly"
        )
    )
    ids = findall(messages[0], "m:Items/*/t:ItemId")
    if not ids:
        # Older builds answer without the new item; the draft exists but cannot be marked or sent by us.
        from exchange_mcp.core.errors import ExchangeError

        raise ExchangeError("uncertain", ref="reply-without-id")
    new_id, new_ck = ids[0].get("Id"), ids[0].get("ChangeKey", "")
    new_ck = await mark_draft(ews, new_id, new_ck)
    return new_id, new_ck


async def _ck(ews: Ews, item: str) -> str:
    from exchange_mcp.ews.common import change_key

    return await change_key(ews, item)


async def mark_draft(ews: Ews, item: str, ck: str) -> str:
    messages = await ews.call(
        M.UpdateItem(
            M.ItemChanges(
                T.ItemChange(
                    T.ItemId(Id=item, ChangeKey=ck),
                    T.Updates(T.SetItemField(draft_marker_uri(), T.Message(draft_marker()))),
                )
            ),
            ConflictResolution="AutoResolve",
            MessageDisposition="SaveOnly",
        )
    )
    ids = findall(messages[0], "m:Items/*/t:ItemId")
    return ids[0].get("ChangeKey", ck) if ids else ck


async def update_draft(
    ews: Ews,
    item: str,
    ck: str,
    *,
    subject: str | None,
    html: str | None,
    to: list[str] | None,
    cc: list[str] | None,
    bcc: list[str] | None,
    importance: str | None,
) -> str:
    updates = []
    if subject is not None:
        updates.append(T.SetItemField(T.FieldURI(FieldURI="item:Subject"), T.Message(T.Subject(subject))))
    if html is not None:
        updates.append(T.SetItemField(T.FieldURI(FieldURI="item:Body"), T.Message(T.Body(html, BodyType="HTML"))))
    if importance is not None:
        updates.append(T.SetItemField(T.FieldURI(FieldURI="item:Importance"), T.Message(T.Importance(importance))))
    for uri, tag, addrs in (
        ("message:ToRecipients", "ToRecipients", to),
        ("message:CcRecipients", "CcRecipients", cc),
        ("message:BccRecipients", "BccRecipients", bcc),
    ):
        if addrs is None:
            continue
        if addrs:
            updates.append(T.SetItemField(T.FieldURI(FieldURI=uri), T.Message(_mailboxes(tag, addrs))))
        else:
            updates.append(T.DeleteItemField(T.FieldURI(FieldURI=uri)))
    if not updates:
        return ck
    messages = await ews.call(
        M.UpdateItem(
            M.ItemChanges(T.ItemChange(T.ItemId(Id=item, ChangeKey=ck), T.Updates(*updates))),
            ConflictResolution="AutoResolve",
            MessageDisposition="SaveOnly",
        )
    )
    ids = findall(messages[0], "m:Items/*/t:ItemId")
    return ids[0].get("ChangeKey", ck) if ids else ck


async def add_attachment(
    ews: Ews, item: str, name: str, content_type: str, data: bytes, *, content_id: str | None = None
) -> None:
    import base64

    att = T.FileAttachment(T.Name(name), T.ContentType(content_type))
    if content_id:
        att.append(T.ContentId(content_id))
        att.append(T.IsInline("true"))
    att.append(T.Content(base64.b64encode(data).decode()))
    await ews.call(M.CreateAttachment(M.ParentItemId(Id=item), M.Attachments(att)))


async def send(ews: Ews, item: str, ck: str) -> None:
    """The only send in this server (manifesto rule 24): SendItem of a saved draft."""
    await ews.call(
        M.SendItem(
            M.ItemIds(T.ItemId(Id=item, ChangeKey=ck)),
            M.SavedItemFolderId(T.DistinguishedFolderId(Id="sentitems")),
            SaveItemToFolder="true",
        )
    )


async def mail_tips(ews: Ews, sender: str, recipients: list[str]) -> dict[str, Any]:
    messages = await ews.call(
        M.GetMailTips(
            M.SendingAs(T.EmailAddress(sender)),
            M.Recipients(*[T.Mailbox(T.EmailAddress(r)) for r in recipients]),
            M.MailTipsRequested("OutOfOfficeMessage MaxMessageSize InvalidRecipient MailboxFullStatus"),
        )
    )
    out: dict[str, Any] = {"automatic_replies": [], "invalid": [], "full": []}
    sizes = []
    for mt in (tip for m in messages for tip in m.iter("{*}MailTips")):
        addr = text(mt, "t:RecipientAddress/t:EmailAddress")
        if text(mt, "t:OutOfOffice/t:ReplyBody/t:Message").strip():
            out["automatic_replies"].append(addr)
        if flag(mt, "t:InvalidRecipient"):
            out["invalid"].append(addr)
        if flag(mt, "t:MailboxFull"):
            out["full"].append(addr)
        size = text(mt, "t:MaxMessageSize")
        if size.isdigit():
            sizes.append(int(size))
    if sizes:
        out["max_message_size"] = min(sizes)
    return out


# ---------------------------------------------------------------- folders


async def create_folder(ews: Ews, name: str, parent: str) -> str:
    messages = await ews.call(
        M.CreateFolder(M.ParentFolderId(folder_id(parent)), M.Folders(T.Folder(T.DisplayName(name))))
    )
    return findall(messages[0], "m:Folders/*/t:FolderId")[0].get("Id")


async def rename_folder(ews: Ews, fid: str, name: str) -> None:
    await ews.call(
        M.UpdateFolder(
            M.FolderChanges(
                T.FolderChange(
                    T.FolderId(Id=fid),
                    T.Updates(
                        T.SetFolderField(T.FieldURI(FieldURI="folder:DisplayName"), T.Folder(T.DisplayName(name)))
                    ),
                )
            )
        )
    )


async def move_folder(ews: Ews, fid: str, parent: str) -> None:
    await ews.call(M.MoveFolder(M.ToFolderId(folder_id(parent)), M.FolderIds(T.FolderId(Id=fid))))


async def delete_folder(ews: Ews, fid: str) -> None:
    await ews.call(M.DeleteFolder(M.FolderIds(T.FolderId(Id=fid)), DeleteType="MoveToDeletedItems"))


# ---------------------------------------------------------------- automatic replies


async def oof(ews: Ews, me: str) -> dict[str, str]:
    messages = await ews.call(M.GetUserOofSettingsRequest(T.Mailbox(T.Address(me))))
    r = messages[0]
    return {
        "state": text(r, ".//t:OofState", "Disabled").lower(),
        "external_audience": text(r, ".//t:ExternalAudience", "None").lower(),
        "start": text(r, ".//t:Duration/t:StartTime"),
        "end": text(r, ".//t:Duration/t:EndTime"),
    }


async def set_oof(
    ews: Ews,
    me: str,
    *,
    state: str,
    audience: str,
    internal: str | None,
    external: str | None,
    start: str | None,
    end: str | None,
) -> None:
    settings = T.UserOofSettings(T.OofState(state), T.ExternalAudience(audience))
    if start and end:
        settings.append(T.Duration(T.StartTime(start), T.EndTime(end)))
    if internal is not None:  # absent: Exchange keeps the saved reply texts
        settings.append(T.InternalReply(T.Message(internal)))
    if external is not None:
        settings.append(T.ExternalReply(T.Message(external)))
    await ews.call(M.SetUserOofSettingsRequest(T.Mailbox(T.Address(me)), settings))


# ---------------------------------------------------------------- the folder tree

FOLDER_PAGE = 500


@dataclass
class RawFolder:
    id: str
    parent: str
    name: str
    unread: int
    total: int
    children: int
    mail: bool


async def folder_tree(ews: Ews, root: str) -> list[RawFolder]:
    """Every folder under `root`, page by page until Exchange says the last is in range."""
    out: list[RawFolder] = []
    offset = 0
    while True:
        messages = await ews.call(
            M.FindFolder(
                M.FolderShape(
                    T.BaseShape("IdOnly"),
                    _fields(
                        "folder:DisplayName",
                        "folder:ParentFolderId",
                        "folder:UnreadCount",
                        "folder:TotalCount",
                        "folder:ChildFolderCount",
                        "folder:FolderClass",
                    ),
                ),
                M.IndexedPageFolderView(MaxEntriesReturned=str(FOLDER_PAGE), Offset=str(offset), BasePoint="Beginning"),
                M.ParentFolderIds(folder_id(root)),
                Traversal="Deep",
            )
        )
        rf = find(messages[0], "m:RootFolder")
        page = findall(rf, "t:Folders/*")
        for f in page:
            cls = text(f, "t:FolderClass")
            parent = f.find("{*}ParentFolderId")
            out.append(
                RawFolder(
                    id=f.find("{*}FolderId").get("Id"),
                    parent=parent.get("Id") if parent is not None else "",
                    name=text(f, "t:DisplayName"),
                    unread=int(text(f, "t:UnreadCount", "0") or 0),
                    total=int(text(f, "t:TotalCount", "0") or 0),
                    children=int(text(f, "t:ChildFolderCount", "0") or 0),
                    mail=not cls or cls.startswith("IPF.Note"),
                )
            )
        last = rf is None or rf.get("IncludesLastItemInRange", "true") == "true"
        if last or not page:
            return out
        if len(out) >= 20 * FOLDER_PAGE:
            from exchange_mcp.ews.people import partial

            return partial(out, True)
        offset += len(page)
