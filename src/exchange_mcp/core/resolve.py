"""Names as people say them, resolved by Exchange: people and rooms through the address book and the
person's contacts, folders by name or path. Ambiguity is an error that lists the candidates, never the
first match (manifesto rule 7). Distribution lists are refused as recipients (rule 26).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from exchange_mcp.core.content import name as clean_name
from exchange_mcp.core.errors import ExchangeError, invalid
from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.common import WELL_KNOWN
from exchange_mcp.ews.soap import M, T, findall, text

ADDRESS = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
GROUPS = frozenset({"PublicDL", "PrivateDL", "GroupMailbox"})
MAX_CANDIDATES = 5


@dataclass(frozen=True)
class Person:
    name: str
    address: str
    kind: str  # Mailbox, Contact, User, OneOff, PublicDL, ...
    title: str = ""
    department: str = ""
    phone: str = ""

    def short(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


async def candidates(ews: Ews, query: str, *, scope: str = "ActiveDirectoryContacts") -> list[Person]:
    try:
        messages = await ews.call(
            M.ResolveNames(M.UnresolvedEntry(query), ReturnFullContactData="true", SearchScope=scope)
        )
    except ExchangeError as e:
        if e.key == "failed" and e.params.get("code") == "ErrorNameResolutionNoResults":
            return []
        raise
    out = []
    for m in messages:
        for r in findall(m, "m:ResolutionSet/t:Resolution"):
            out.append(
                Person(
                    name=clean_name(text(r, "t:Mailbox/t:Name") or text(r, "t:Contact/t:DisplayName")) or "",
                    address=text(r, "t:Mailbox/t:EmailAddress"),
                    kind=text(r, "t:Mailbox/t:MailboxType", "Mailbox"),
                    title=clean_name(text(r, "t:Contact/t:JobTitle")) or "",
                    department=clean_name(text(r, "t:Contact/t:Department")) or "",
                    phone=text(r, "t:Contact/t:PhoneNumbers/t:Entry[@Key='BusinessPhone']"),
                )
            )
    return [p for p in out if p.address]


async def recipient(ews: Ews, query: str) -> Person:
    """One person to write to or invite: an address, or a name that matches exactly one entry."""
    q = query.strip()
    if not q or len(q) > 256:
        raise invalid("A recipient is a name or an e-mail address.")
    found = await candidates(ews, q)
    if ADDRESS.match(q):
        exact = [p for p in found if p.address.lower() == q.lower()]
        if exact:
            return _no_group(exact[0], q)
        return Person(name="", address=q, kind="OneOff")  # outside the address book: sent as typed
    if not found:
        raise ExchangeError("unresolved", kind="person", query=q)
    exact = [p for p in found if p.name.lower() == q.lower()]
    pool = exact if len(exact) == 1 else found
    if len(pool) > 1:
        raise ExchangeError(
            "ambiguous",
            kind="person",
            query=q,
            candidates="; ".join(p.short() for p in pool[:MAX_CANDIDATES]),
        )
    return _no_group(pool[0], q)


def _no_group(p: Person, q: str) -> Person:
    if p.kind in GROUPS:
        raise invalid(
            f"{p.short()} is a distribution list. Mass mailing is out of scope here: write to people one by one, "
            "at most 20 per message. Nothing was done."
        )
    return p


async def recipients(ews: Ews, queries: list[str]) -> list[Person]:
    out: list[Person] = []
    seen: set[str] = set()
    for q in queries:
        p = await recipient(ews, q)
        if p.address.lower() not in seen:
            seen.add(p.address.lower())
            out.append(p)
    return out


# ---------------------------------------------------------------- folders


@dataclass(frozen=True)
class Folder:
    id: str
    name: str
    path: str
    unread: int
    total: int
    children: int
    well_known: str = ""


async def folder_tree(ews: Ews, root: str = "msgfolderroot") -> list[Folder]:
    messages = await ews.call(
        M.FindFolder(
            M.FolderShape(
                T.BaseShape("IdOnly"),
                T.AdditionalProperties(
                    T.FieldURI(FieldURI="folder:DisplayName"),
                    T.FieldURI(FieldURI="folder:ParentFolderId"),
                    T.FieldURI(FieldURI="folder:UnreadCount"),
                    T.FieldURI(FieldURI="folder:TotalCount"),
                    T.FieldURI(FieldURI="folder:ChildFolderCount"),
                    T.FieldURI(FieldURI="folder:FolderClass"),
                ),
            ),
            M.IndexedPageFolderView(MaxEntriesReturned="1000", Offset="0", BasePoint="Beginning"),
            M.ParentFolderIds(T.DistinguishedFolderId(Id=root)),
            Traversal="Deep",
        )
    )
    raw = []
    for f in findall(messages[0], "m:RootFolder/t:Folders/*"):
        cls = text(f, "t:FolderClass")
        if cls and not cls.startswith("IPF.Note"):
            continue  # mail folders only: calendars, contacts and tasks have their own tools
        raw.append(
            (
                f.find("{*}FolderId").get("Id"),
                (f.find("{*}ParentFolderId").get("Id") if f.find("{*}ParentFolderId") is not None else ""),
                clean_name(text(f, "t:DisplayName")) or "",
                int(text(f, "t:UnreadCount", "0") or 0),
                int(text(f, "t:TotalCount", "0") or 0),
                int(text(f, "t:ChildFolderCount", "0") or 0),
            )
        )
    by_id = {r[0]: r for r in raw}

    def path(fid: str, depth: int = 0) -> str:
        r = by_id.get(fid)
        if r is None or depth > 32:
            return ""
        parent = path(r[1], depth + 1)
        return f"{parent}/{r[2]}" if parent else r[2]

    return [Folder(r[0], r[2], path(r[0]), r[3], r[4], r[5]) for r in raw]


async def folder(ews: Ews, value: str) -> str:
    """A folder id for a well-known name, a display name or a path like Inbox/Projects."""
    v = value.strip()
    key = v.lower()
    if key in WELL_KNOWN:
        return WELL_KNOWN[key]
    tree = await folder_tree(ews)
    matches = [f for f in tree if f.path.lower() == key] or [f for f in tree if f.name.lower() == key]
    if not matches:
        raise ExchangeError("unresolved", kind="folder", query=v)
    if len(matches) > 1:
        raise ExchangeError(
            "ambiguous", kind="folder", query=v, candidates="; ".join(f.path for f in matches[:MAX_CANDIDATES])
        )
    return matches[0].id
