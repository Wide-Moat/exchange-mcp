"""Times as people say them, in the person's time zone, and times as EWS writes them (UTC, ISO 8601)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
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
    "America/Toronto": "Eastern Standard Time",
    "America/Vancouver": "Pacific Standard Time",
    "America/Mexico_City": "Central Standard Time (Mexico)",
    "America/Phoenix": "US Mountain Standard Time",
    "America/Anchorage": "Alaskan Standard Time",
    "Pacific/Honolulu": "Hawaiian Standard Time",
    "America/Bogota": "SA Pacific Standard Time",
    "America/Argentina/Buenos_Aires": "Argentina Standard Time",
    "Australia/Melbourne": "AUS Eastern Standard Time",
    "Australia/Brisbane": "E. Australia Standard Time",
    "Australia/Perth": "W. Australia Standard Time",
    "Australia/Adelaide": "Cen. Australia Standard Time",
    "Pacific/Auckland": "New Zealand Standard Time",
    "Asia/Seoul": "Korea Standard Time",
    "Asia/Hong_Kong": "China Standard Time",
    "Asia/Bangkok": "SE Asia Standard Time",
    "Asia/Jakarta": "SE Asia Standard Time",
    "Asia/Karachi": "Pakistan Standard Time",
    "Asia/Riyadh": "Arab Standard Time",
    "Asia/Tehran": "Iran Standard Time",
    "Africa/Cairo": "Egypt Standard Time",
    "Africa/Johannesburg": "South Africa Standard Time",
    "Africa/Lagos": "W. Central Africa Standard Time",
    "Europe/Oslo": "W. Europe Standard Time",
    "Europe/Stockholm": "W. Europe Standard Time",
    "Europe/Copenhagen": "Romance Standard Time",
    "Europe/Sofia": "FLE Standard Time",
    "Europe/Belgrade": "Central Europe Standard Time",
    "Europe/Chisinau": "E. Europe Standard Time",
    "Europe/Volgograd": "Volgograd Standard Time",
    "Europe/Astrakhan": "Astrakhan Standard Time",
    "Europe/Saratov": "Saratov Standard Time",
    "Europe/Ulyanovsk": "Astrakhan Standard Time",
    "Asia/Barnaul": "Altai Standard Time",
    "Asia/Tomsk": "Tomsk Standard Time",
    "Asia/Novokuznetsk": "North Asia Standard Time",
    "Asia/Chita": "Transbaikal Standard Time",
    "Asia/Sakhalin": "Sakhalin Standard Time",
    "Asia/Srednekolymsk": "Russia Time Zone 10",
    "Asia/Anadyr": "Russia Time Zone 11",
    "Asia/Bishkek": "Central Asia Standard Time",
    "Asia/Dhaka": "Bangladesh Standard Time",
    "Asia/Kathmandu": "Nepal Standard Time",
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
        if off == "Z":
            return dt.replace(tzinfo=UTC)
        sign, hh_o, mm_o = off[0], int(off[1:3]), int(off[-2:])
        if hh_o > 14 or mm_o > 59 or (hh_o == 14 and mm_o):
            raise invalid(f"{field} has an offset that does not exist; offsets run from -14:00 to +14:00.")
        delta = timedelta(hours=hh_o, minutes=mm_o)
        return dt.replace(tzinfo=timezone(delta if sign == "+" else -delta))
    aware = dt.replace(tzinfo=tz)
    if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != dt:
        raise invalid(f"{field} does not exist in {tz.key}: the clocks skip it. Give an offset or another time.")
    if dt.replace(tzinfo=tz, fold=1).utcoffset() != aware.utcoffset():
        raise invalid(f"{field} happens twice in {tz.key} (the clocks go back). Give it with an offset.")
    return aware


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
