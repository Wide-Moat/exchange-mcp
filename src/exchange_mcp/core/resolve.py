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
    alias: str = ""

    def short(self) -> str:
        who = f"{self.name} <{self.address}>" if self.name else self.address
        return f"{who}, {self.title}" if self.title else who  # QA D13: the title tells namesakes apart


async def candidates(ews: Ews, query: str, *, scope: str = "ActiveDirectoryContacts") -> list[Person]:
    from exchange_mcp.ews import people as ep

    found = await ep.resolve_names(ews, query, directory_only=scope == "ActiveDirectory")
    return ep.partial(
        [
            Person(
                name=clean_name(f.name) or "",
                address=f.address,
                kind=f.kind,
                title=clean_name(f.title) or "",
                department=clean_name(f.department) or "",
                phone=f.phone,
                alias=f.alias,
            )
            for f in found
            if f.address
        ],
        getattr(found, "truncated", False),
    )


async def recipient(ews: Ews, query: str) -> Person:
    """One person to write to or invite: an address, or a name that matches exactly one entry."""
    q = query.strip()
    if not q or len(q) > 256:
        raise invalid("A recipient is a name or an e-mail address.")
    found = await candidates(ews, q)
    if ADDRESS.match(q):
        exact = [p for p in found if p.address.lower() == q.lower()]
        if exact:
            return _no_group(exact[0])
        if len(found) == 1 and not getattr(found, "truncated", False):
            # A secondary address of one entry (a person or a list) resolves to its primary address.
            return _no_group(found[0])
        if any(p.kind in GROUPS for p in found):
            raise invalid("That address belongs to a distribution list. Write to people one by one. Nothing was done.")
        return Person(name="", address=q, kind="OneOff")  # outside the address book: sent as typed
    if not found:
        raise ExchangeError("unresolved", kind="person", query=q)
    exact = [p for p in found if p.name.lower() == q.lower()]
    pool = exact if len(exact) == 1 and not getattr(found, "truncated", False) else found
    if getattr(found, "truncated", False) and len(pool) <= 1:
        # Exchange listed only part of the matches: one exact name among them proves nothing.
        raise ExchangeError("ambiguous", kind="person", query=q, candidates="more than Exchange lists; use the address")
    if len(pool) > 1:
        raise ExchangeError(
            "ambiguous",
            kind="person",
            query=q,
            candidates="; ".join(p.short() for p in pool[:MAX_CANDIDATES]),
        )
    return _no_group(pool[0])


def _no_group(p: Person) -> Person:
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
    from exchange_mcp.ews.mail import folder_tree as raw_tree

    got = await raw_tree(ews, root)
    raw = [r for r in got if r.mail]  # calendars, contacts, tasks have their own tools
    by_id = {r.id: r for r in raw}

    def path(fid: str, depth: int = 0) -> str:
        r = by_id.get(fid)
        if r is None or depth > 32:
            return ""
        parent = path(r.parent, depth + 1)
        name = clean_name(r.name) or ""
        return f"{parent}/{name}" if parent else name

    from exchange_mcp.ews.people import partial

    folders = [Folder(r.id, clean_name(r.name) or "", path(r.id), r.unread, r.total, r.children) for r in raw]
    return partial(folders, getattr(got, "truncated", False))


@dataclass(frozen=True)
class Located:
    id: str
    label: str  # what a result calls the folder: the well-known name, or its path


async def locate(ews: Ews, value: str, *, archive: bool = False) -> Located:
    """A folder for a well-known name, a display name or a path like Inbox/Projects; with `archive`, in the
    online archive, where inbox and deleted are the archive's own well-known folders."""
    from exchange_mcp.ews.mail import ARCHIVE_WELL_KNOWN

    v = value.strip()
    key = v.lower() or "inbox"
    known = ARCHIVE_WELL_KNOWN if archive else WELL_KNOWN
    tree = None
    if archive and key in ("inbox", "archive", "deleted"):
        # Live (2016 CU23): an archive policy files mail into a copy of the mailbox's tree, under the
        # mailbox's own folder names ("Inbox" in the person's language) at the archive's top level; the
        # archive's distinguished inbox stays empty. That copy is used when it exists.
        from exchange_mcp.ews.mail import folder_name

        own = (await folder_name(ews, "deleteditems" if key == "deleted" else "inbox")).lower()
        tree = await folder_tree(ews, "archivemsgfolderroot")
        top = [f for f in tree if f.path.lower() == own]
        if len(top) == 1:
            return Located(top[0].id, top[0].path)
    if key in known:
        return Located(known[key], key)
    tree = tree if tree is not None else await folder_tree(ews, "archivemsgfolderroot" if archive else "msgfolderroot")
    matches = [f for f in tree if f.path.lower() == key] or [f for f in tree if f.name.lower() == key]
    if not matches:
        raise ExchangeError("unresolved", kind="folder", query=v)
    if len(matches) > 1:
        raise ExchangeError(
            "ambiguous", kind="folder", query=v, candidates="; ".join(f.path for f in matches[:MAX_CANDIDATES])
        )
    return Located(matches[0].id, matches[0].path)


async def folder(ews: Ews, value: str, *, archive: bool = False) -> str:
    """The folder id of `locate`."""
    return (await locate(ews, value, archive=archive)).id


# ---------------------------------------------------------------- addresses of senders


async def owner_of(ews: Ews, address: str) -> Person | None:
    """The address-book entry that owns this exact address (primary or secondary), or None: an address
    outside the organisation, or one Exchange cannot tell apart."""
    found = await candidates(ews, address)  # a failed lookup is an error, not "no owner" (manifesto 12)
    exact = [p for p in found if p.address.lower() == address.lower()]
    if len(exact) == 1:
        return exact[0]
    if not exact and len(found) == 1 and not getattr(found, "truncated", False):
        return found[0]
    return None


async def address_of_dn(ews: Ews, dn: str) -> str | None:
    """The SMTP address behind an Exchange X.500 DN, or None when Exchange does not give exactly one."""
    try:
        found = await candidates(ews, dn)
    except ExchangeError:
        return None
    smtp = {p.address.lower(): p.address for p in found if ADDRESS.match(p.address)}
    return next(iter(smtp.values())) if len(smtp) == 1 and not getattr(found, "truncated", False) else None
