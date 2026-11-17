"""Times as people say them, in the person's time zone, and times as EWS writes them (UTC, ISO 8601)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from exchange_mcp.core.errors import invalid

# The person's IANA zone as the Windows zone name Exchange keeps a recurring meeting in. Without it a
# recurring series is anchored to UTC and moves by an hour across daylight-saving changes.
WINDOWS_ZONES = {
    "UTC": "UTC",
    "Etc/UTC": "UTC",
    "Europe/London": "GMT Standard Time",
    "Europe/Dublin": "GMT Standard Time",
    "Europe/Lisbon": "GMT Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Amsterdam": "W. Europe Standard Time",
    "Europe/Rome": "W. Europe Standard Time",
    "Europe/Vienna": "W. Europe Standard Time",
    "Europe/Zurich": "W. Europe Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "Europe/Madrid": "Romance Standard Time",
    "Europe/Brussels": "Romance Standard Time",
    "Europe/Prague": "Central Europe Standard Time",
    "Europe/Budapest": "Central Europe Standard Time",
    "Europe/Warsaw": "Central European Standard Time",
    "Europe/Kiev": "FLE Standard Time",
    "Europe/Kyiv": "FLE Standard Time",
    "Europe/Helsinki": "FLE Standard Time",
    "Europe/Riga": "FLE Standard Time",
    "Europe/Vilnius": "FLE Standard Time",
    "Europe/Tallinn": "FLE Standard Time",
    "Europe/Athens": "GTB Standard Time",
    "Europe/Bucharest": "GTB Standard Time",
    "Europe/Istanbul": "Turkey Standard Time",
    "Europe/Minsk": "Belarus Standard Time",
    "Europe/Moscow": "Russian Standard Time",
    "Europe/Kaliningrad": "Kaliningrad Standard Time",
    "Europe/Samara": "Russia Time Zone 3",
    "Asia/Yekaterinburg": "Ekaterinburg Standard Time",
    "Asia/Omsk": "Omsk Standard Time",
    "Asia/Novosibirsk": "N. Central Asia Standard Time",
    "Asia/Krasnoyarsk": "North Asia Standard Time",
    "Asia/Irkutsk": "North Asia East Standard Time",
    "Asia/Yakutsk": "Yakutsk Standard Time",
    "Asia/Vladivostok": "Vladivostok Standard Time",
    "Asia/Magadan": "Magadan Standard Time",
    "Asia/Kamchatka": "Russia Time Zone 11",
    "Asia/Almaty": "Central Asia Standard Time",
    "Asia/Tashkent": "West Asia Standard Time",
    "Asia/Tbilisi": "Georgian Standard Time",
    "Asia/Yerevan": "Caucasus Standard Time",
    "Asia/Baku": "Azerbaijan Standard Time",
    "Asia/Dubai": "Arabian Standard Time",
    "Asia/Jerusalem": "Israel Standard Time",
    "Asia/Kolkata": "India Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Tokyo": "Tokyo Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Australia/Sydney": "AUS Eastern Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Sao_Paulo": "E. South America Standard Time",
}


def windows_zone(iana: str) -> str | None:
    return WINDOWS_ZONES.get(iana)


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
