"""Times as people say them, in the person's time zone, and times as EWS writes them (UTC, ISO 8601)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from exchange_mcp.core.errors import invalid

_LOCAL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse(value: str, tz: ZoneInfo, *, field: str = "time") -> datetime:
    m = _LOCAL.match(value.strip())
    if not m:
        raise invalid(f"{field} must look like 2026-11-17 15:00 (local time) or carry an offset like +03:00.")
    y, mo, d, hh, mm, ss, off = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        raise invalid(f"{field} is not a real date or time.") from None
    if off:
        offset = "+00:00" if off == "Z" else off if ":" in off else off[:3] + ":" + off[3:]
        return datetime.fromisoformat(dt.isoformat() + offset)
    return dt.replace(tzinfo=tz)


def ews(dt: datetime) -> str:
    """The form EWS accepts in every version: UTC with a Z."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(value: str | None, tz: ZoneInfo) -> str | None:
    """An EWS time (UTC, or with an offset) as local time with its offset, to the minute."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).isoformat(timespec="minutes")


def window(start: str, end: str, tz: ZoneInfo, *, max_days: int) -> tuple[datetime, datetime]:
    s, e = parse(start, tz, field="start"), parse(end, tz, field="end")
    if e <= s:
        raise invalid("end must be after start.")
    if e - s > timedelta(days=max_days):
        raise invalid(f"The window is at most {max_days} days. Narrow it or make several calls.")
    return s, e
