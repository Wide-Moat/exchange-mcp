"""Tools declare their area and behaviour once; the server publishes only enabled areas.

The behaviour flags are the source of the MCP annotations, and contract tests check that the code
agrees: a tool that notifies others calls the guard, a read-only tool does not write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

AREAS: dict[str, bool] = {
    # area -> on by default
    "account": True,
    "people": True,
    "mail": True,
    "calendar": True,
    "contacts": True,
    "tasks": True,
}

ALWAYS_ON = frozenset({"account"})

ALLOWED_VERBS = ("list_", "get_", "save_", "delete_", "send_", "update_", "find_", "cancel_", "respond_to_")


@dataclass(frozen=True)
class ToolSpec:
    fn: Callable[..., Any]
    name: str
    title: str
    area: str
    read_only: bool
    notifies_others: bool
    irreversible: bool
    idempotent: bool
    returns_list: bool
    description: str = ""


_TOOLS: dict[str, ToolSpec] = {}


def tool(
    *,
    area: str,
    title: str,
    read_only: bool = False,
    notifies_others: bool = False,
    irreversible: bool = False,
    idempotent: bool = False,
    returns_list: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if area not in AREAS:
        raise ValueError(f"unknown area {area!r}")

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = fn.__name__
        if name in _TOOLS:
            raise ValueError(f"tool {name!r} declared twice")
        _TOOLS[name] = ToolSpec(
            fn=fn,
            name=name,
            title=title,
            area=area,
            read_only=read_only,
            notifies_others=notifies_others,
            irreversible=irreversible,
            idempotent=idempotent,
            returns_list=returns_list,
            description=" ".join((fn.__doc__ or "").split()),
        )
        return fn

    return register


def all_tools() -> dict[str, ToolSpec]:
    from exchange_mcp import tools  # noqa: F401  importing the area modules registers their tools

    return dict(_TOOLS)


def enabled(areas: frozenset[str]) -> list[ToolSpec]:
    on = set(areas) | ALWAYS_ON
    return [s for s in all_tools().values() if s.area in on]


def parse_areas(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset(a for a, on in AREAS.items() if on)
    names = {a.strip() for a in value.split(",") if a.strip()}
    unknown = names - set(AREAS)
    if unknown:
        raise ValueError(f"unknown area(s): {', '.join(sorted(unknown))}")
    return frozenset(names) | ALWAYS_ON
