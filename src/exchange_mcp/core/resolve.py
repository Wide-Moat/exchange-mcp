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
        return f"{self.name} <{self.address}>" if self.name else self.address


async def candidates(ews: Ews, query: str, *, scope: str = "ActiveDirectoryContacts") -> list[Person]:
    from exchange_mcp.ews import people as ep

    found = await ep.resolve_names(ews, query, directory_only=scope == "ActiveDirectory")
    return [
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
    ]


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
    from exchange_mcp.ews.mail import folder_tree as raw_tree

    raw = [r for r in await raw_tree(ews, root) if r.mail]  # calendars, contacts, tasks have their own tools
    by_id = {r.id: r for r in raw}

    def path(fid: str, depth: int = 0) -> str:
        r = by_id.get(fid)
        if r is None or depth > 32:
            return ""
        parent = path(r.parent, depth + 1)
        name = clean_name(r.name) or ""
        return f"{parent}/{name}" if parent else name

    return [Folder(r.id, clean_name(r.name) or "", path(r.id), r.unread, r.total, r.children) for r in raw]


async def folder(ews: Ews, value: str, *, archive: bool = False) -> str:
    """A folder id for a well-known name, a display name or a path like Inbox/Projects; with `archive`,
    looked up in the online archive."""
    v = value.strip()
    key = v.lower()
    if archive and key in ("archive", ""):
        return "archivemsgfolderroot"
    if key in WELL_KNOWN and not archive:
        return WELL_KNOWN[key]
    tree = await folder_tree(ews, "archivemsgfolderroot" if archive else "msgfolderroot")
    matches = [f for f in tree if f.path.lower() == key] or [f for f in tree if f.name.lower() == key]
    if not matches:
        raise ExchangeError("unresolved", kind="folder", query=v)
    if len(matches) > 1:
        raise ExchangeError(
            "ambiguous", kind="folder", query=v, candidates="; ".join(f.path for f in matches[:MAX_CANDIDATES])
        )
    return matches[0].id
