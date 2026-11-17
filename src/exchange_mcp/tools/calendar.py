"""calendar: the person's own calendar, other people's free/busy, meetings and answers to invitations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from exchange_mcp.core import resolve
from exchange_mcp.core.content import content_block, name
from exchange_mcp.core.context import call
from exchange_mcp.core.errors import ExchangeError, invalid
from exchange_mcp.core.guard import MAX_RECIPIENTS
from exchange_mcp.core.html import html_to_text, text_to_html
from exchange_mcp.core.project import clamp_limit, compact, page_result
from exchange_mcp.core.time import ews as ews_time
from exchange_mcp.core.time import local, parse, window, windows_zone
from exchange_mcp.ews import calendar as ec
from exchange_mcp.ews.common import move_to_deleted
from exchange_mcp.registry import tool

LIST_DAYS = 62
AVAILABILITY_DAYS = 42
MAX_VIEWS = 5
MAX_ATTENDEES_SHOWN = 50
MAX_BUSY_SHOWN = 50
MAX_SLOTS = 10
BODY_CHARS = 20_000
QUALITY = {"Excellent": 0, "Good": 1, "Fair": 2, "Poor": 3}
RESPONSE = {
    "Organizer": "organiser",
    "Accept": "accepted",
    "Tentative": "tentative",
    "Decline": "declined",
    "NoResponseReceived": "not answered",
}
ID = Annotated[str, Field(description="An event id from list_events.", min_length=1, max_length=512)]
PEOPLE = Annotated[list[str] | None, Field(description="Names or addresses.")]


def _t(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _subject(ev: ec.Event, me: str) -> str | None:
    if not ev.subject:
        return None
    return ev.subject if ev.organised_by(me) else content_block("the organiser (subject)", ev.subject)


def _row(ev: ec.Event, me: str, tz) -> dict[str, Any]:
    return compact(
        {
            "id": ev.id,
            "subject": _subject(ev, me),
            "start": local(ev.start, tz),
            "end": local(ev.end, tz),
            "all_day": ev.all_day,
            "location": name(ev.location),
            "organiser": None if ev.organised_by(me) else name(ev.organizer.get("name")) or ev.organizer.get("address"),
            "my_response": RESPONSE.get(ev.my_response),
            "attendees": ev.invitee_count or None,
            "online_link": ev.online_url,
            "recurring": ev.kind != "Single",
            "private": ev.private,
            "cancelled": ev.cancelled,
            "show_as": ev.show_as.lower() if ev.show_as != "Busy" else None,
        }
    )


@tool(area="calendar", title="List events", read_only=True, idempotent=True, returns_list=True)
async def list_events(
    start: Annotated[str, Field(description="Local start of the window, 2026-11-17 or 2026-11-17 09:00.")],
    end: Annotated[str, Field(description="Local end of the window; at most 62 days after start.")],
    query: Annotated[str | None, Field(description="Only events whose subject contains these words.")] = None,
    limit: Annotated[int | None, Field(description="How many, 1 to 25; 10 by default.")] = None,
    cursor: Annotated[str | None, Field(description="The cursor from the previous page.")] = None,
) -> dict[str, Any]:
    """Lists the events in the person's own calendar between two local times, in start order, with
    repeating meetings expanded into their occurrences. Call it to answer what is on the calendar, whether
    the person is free, or to find an event's id before reading, changing, answering or cancelling it.
    The window is at most 62 days. It reads only."""
    async with call("list_events") as c:
        n, extra = clamp_limit(limit)
        tz = c.tz
        s, e = window(start, end, tz, max_days=LIST_DAYS)
        q = (query or "").strip().lower()
        scope = {"s": start, "e": end, "q": q}
        state = await c.cursor_in(cursor, scope) or {}
        # CalendarView does not page: the position is the start of the last event processed, and the ids
        # already processed at that very start.
        pos: str | None = state.get("p")
        done: set[str] = set(state.get("k", []))
        picked: list[ec.Event] = []
        more = False
        for _ in range(MAX_VIEWS):
            view = await ec.calendar_view(c.ews, pos or ews_time(s), ews_time(e))
            for ev in view.events:
                if pos is not None and (_t(ev.start) < _t(pos) or (_t(ev.start) == _t(pos) and ev.id in done)):
                    continue
                if not q or q in ev.subject.lower():
                    if len(picked) == n:
                        more = True
                        break
                    picked.append(ev)
                if pos is not None and _t(ev.start) == _t(pos):
                    done.add(ev.id)
                else:
                    pos, done = ev.start, {ev.id}
            if more or view.complete or not view.events:
                break
        else:
            more = True
        nxt = await c.cursor_out({"p": pos, "k": sorted(done)} if more and pos else None, scope)
        return page_result([_row(ev, c.me, tz) for ev in picked], nxt, **extra)


@tool(area="calendar", title="Read an event", read_only=True, idempotent=True)
async def get_event(id: ID, include_body: bool = True) -> dict[str, Any]:
    """Reads one event or one occurrence of a repeating meeting: time, place, organiser, attendees with their
    answers, how it repeats in words, the description, and attachments as download links. Call it after
    list_events when the person asks about a meeting's details or who is coming. The description and
    subject come marked as data when someone else wrote them. It reads only."""
    async with call("get_event") as c:
        tz = c.tz
        ev = await ec.get_event(c.ews, id, body=include_body)
        out = _row(ev, c.me, tz)
        people = [
            compact({"name": name(a.name), "address": a.address, "response": RESPONSE.get(a.response, "unknown")})
            for a in ev.attendees
        ]
        out["attendees"] = people[:MAX_ATTENDEES_SHOWN]
        if len(people) > MAX_ATTENDEES_SHOWN:
            out["attendees_note"] = f"The first {MAX_ATTENDEES_SHOWN} of {len(people)} attendees."
        out["rooms"] = [compact({"name": name(r.name), "address": r.address}) for r in ev.rooms]
        out["repeats"] = ev.recurrence or None
        out["reminder_minutes"] = ev.reminder
        if include_body and ev.html:
            body = html_to_text(ev.html)
            out["body"] = content_block("the organiser", body[:BODY_CHARS])
            if len(body) > BODY_CHARS:
                out["body_truncated"] = True
                out["body_chars"] = len(body)
        files = []
        for a in ev.attachments:
            if a.inline:
                continue
            entry: dict[str, Any] = {"name": name(a.name), "size": a.size, "type": a.content_type}
            if a.kind == "file" and c.rt.links is not None:
                entry.update(await c.rt.links.download(c.cred, c.person, a.id, a.name, a.size))
            elif a.kind != "file":
                entry["note"] = "An attached item: open it in Outlook."
            files.append(entry)
        out["attachments"] = files
        return compact(out)


@tool(area="calendar", title="Find meeting times", read_only=True, idempotent=True)
async def find_meeting_times(
    people: Annotated[list[str], Field(description="Names or addresses; the person is not included unless named.")],
    start: Annotated[str, Field(description="Local start of the window.")],
    end: Annotated[str, Field(description="Local end; at most 42 days after start.")],
    duration: Annotated[
        int | None, Field(description="Meeting length in minutes; given, returns free slots.", ge=5, le=1440)
    ] = None,
    rooms: Annotated[list[str] | None, Field(description="Room names or addresses to check too.")] = None,
    working_hours_only: Annotated[
        bool, Field(description="With duration: suggest only slots inside working hours.")
    ] = True,
) -> dict[str, Any]:
    """Shows when people and rooms are busy, or with duration suggests up to 10 times when all are free,
    best first. Call it before proposing or saving a meeting with others. At most 20 people and rooms and a
    window of 42 days; busy times carry no subjects. It reads only and invites nobody."""
    async with call("find_meeting_times") as c:
        tz = c.tz
        if not people and not rooms:
            raise invalid("Give at least one person or room.")
        count = len(people) + len(rooms or [])
        if count > MAX_RECIPIENTS:
            raise ExchangeError("too_many", limit=MAX_RECIPIENTS, what="people and rooms", count=count)
        s, e = window(start, end, tz, max_days=AVAILABILITY_DAYS)
        who = [(p, "Required") for p in await resolve.recipients(c.ews, people)]
        who += [(r, "Room") for r in await resolve.recipients(c.ews, rooms or [])]
        ask_s, ask_e = s, e
        if duration:
            # Suggestions are computed per whole day: ask for whole UTC days, then keep what is in the window.
            ask_s = s.astimezone(UTC).replace(hour=0, minute=0, second=0)
            ask_e = e.astimezone(UTC).replace(hour=0, minute=0, second=0)
            if ask_e < e:
                ask_e += timedelta(days=1)
        found, suggestions = await ec.availability(
            c.ews,
            [(p.address, kind) for p, kind in who],
            ask_s,
            ask_e,
            duration=duration,
            working_hours_only=working_hours_only,
        )
        names = {p.address: p.name for p, _ in who}
        out: dict[str, Any] = {}
        rows = []
        for a in found:
            row: dict[str, Any] = {"who": name(names.get(a.address)) or a.address, "address": a.address}
            if not a.known:
                row["availability"] = "unknown: Exchange did not answer for this address"
            elif not duration:
                blocks = [b for b in a.busy if _t(b.start) < e and _t(b.end) > s]
                row["busy"] = [
                    compact(
                        {
                            "start": local(b.start, tz),
                            "end": local(b.end, tz),
                            "show_as": b.kind.lower() if b.kind != "Busy" else None,
                        }
                    )
                    for b in blocks[:MAX_BUSY_SHOWN]
                ]
                if len(blocks) > MAX_BUSY_SHOWN:
                    row["busy_note"] = f"The first {MAX_BUSY_SHOWN} of {len(blocks)} busy times."
            rows.append(compact(row) if a.known and duration else row)
        out["people"] = rows
        if duration:
            span = timedelta(minutes=duration)
            ok = [
                x
                for x in suggestions
                if s <= _t(x.start) and _t(x.start) + span <= e and (x.work_time or not working_hours_only)
            ]
            ok.sort(key=lambda x: (QUALITY.get(x.quality, 4), _t(x.start)))
            out["slots"] = [
                {
                    "start": local(x.start, tz),
                    "end": local(ews_time(_t(x.start) + span), tz),
                    "quality": x.quality.lower(),
                }
                for x in ok[:MAX_SLOTS]
            ]
            if not out["slots"]:
                out["note"] = "No time in this window suits everyone. Widen the window or drop someone."
        return out


@tool(area="calendar", title="Save an event", notifies_others=True)
async def save_event(
    id: Annotated[str | None, Field(description="Absent creates one.")] = None,
    subject: str = "",
    start: Annotated[str, Field(description="Local time, 2026-11-17 15:00.")] = "",
    end: str = "",
    duration: Annotated[int | None, Field(description="Minutes, instead of end.")] = None,
    attendees: PEOPLE = None,
    rooms: PEOPLE = None,
    location: str | None = None,
    body: str = "",
    repeat: Literal["daily", "weekdays", "weekly", "monthly", "yearly"] | None = None,
    repeat_until: Annotated[str, Field(description="Last local date.")] = "",
    repeat_count: int | None = None,
    reminder_minutes: Annotated[int | None, Field(description="-1 turns it off.")] = None,
    show_as: Literal["free", "tentative", "busy", "away", "elsewhere"] | None = None,
    private: bool | None = None,
    user_confirmed: Annotated[bool, Field(description="True only after the person agreed to send.")] = False,
) -> dict[str, Any]:
    """Creates or changes an event in the person's own calendar; subject and start create one. Call it to
    block time, or with attendees or rooms to invite: invitations and updates go out at once, so they need
    user_confirmed=true after the person agreed. Only the organiser changes a meeting; a repeat needs
    repeat_until or repeat_count."""
    async with call("save_event") as c:
        tz = c.tz
        if end and duration is not None:
            raise invalid("Give end or duration, not both. Nothing was saved.")
        given = len(attendees or []) + len(rooms or [])
        if given > MAX_RECIPIENTS:
            raise ExchangeError("too_many", limit=MAX_RECIPIENTS, what="attendees and rooms", count=given)
        if repeat and not (repeat_until or repeat_count):
            raise invalid("A repeating event needs an end: repeat_until or repeat_count. Nothing was saved.")
        if (repeat_until or repeat_count) and not repeat:
            raise invalid("repeat_until and repeat_count need repeat. Nothing was saved.")
        if repeat_until and repeat_count:
            raise invalid("Give repeat_until or repeat_count, not both. Nothing was saved.")
        if duration is not None and not 1 <= duration <= 20160:
            raise invalid("duration is 1 to 20160 minutes. Nothing was saved.")
        if repeat_count is not None and not 1 <= repeat_count <= 999:
            raise invalid("repeat_count is 1 to 999. Nothing was saved.")
        if reminder_minutes is not None and reminder_minutes < -1:
            raise invalid("reminder_minutes is -1 (off) or more. Nothing was saved.")
        s = parse(start, tz, field="start") if start else None
        e = parse(end, tz, field="end") if end else None
        if id is None:
            if not (subject or "").strip() or s is None:
                raise invalid("A new event needs subject and start. Nothing was saved.")
            if e is None and duration is None:
                raise invalid("A new event needs end or duration. Nothing was saved.")
        if s is not None and duration is not None:
            e = s + timedelta(minutes=duration)
        if s is not None and e is not None and e <= s:
            raise invalid("end must be after start. Nothing was saved.")

        existing = None
        if id is not None:
            existing = await ec.get_event(c.ews, id, body=False)
            if not existing.organised_by(c.me):
                raise ExchangeError("not_organiser")
            was_s, was_e = _t(existing.start), _t(existing.end)
            if s is not None and e is None:
                e = s + (was_e - was_s)
            elif e is not None and s is None:
                s = was_s
            if duration is not None and s is None:
                s, e = was_s, was_s + timedelta(minutes=duration)
            if s is not None and e is not None and e <= s:
                raise invalid("end must be after start. Nothing was saved.")
        rule = None
        if repeat:
            first = (s or _t(existing.start)).astimezone(tz).date()
            until = parse(repeat_until, tz, field="repeat_until").date() if repeat_until else None
            if until is not None and until < first:
                raise invalid("repeat_until is before the first occurrence. Nothing was saved.")
            rule = ec.recurrence(repeat, first, until=until, count=repeat_count)

        before = len(existing.attendees) + len(existing.rooms) if existing else 0
        after = given if (attendees is not None or rooms is not None or existing is None) else before
        if existing is not None and attendees is None and rooms is not None:
            after = len(existing.attendees) + len(rooms)
        elif existing is not None and rooms is None and attendees is not None:
            after = len(attendees) + len(existing.rooms)
        told = max(before, after)
        if told:
            c.rt.guard.confirmed(kind="invite", user_confirmed=user_confirmed)
        required = await resolve.recipients(c.ews, attendees) if attendees is not None else None
        booked = await resolve.recipients(c.ews, rooms) if rooms is not None else None
        place = location
        if place is None and existing is None and booked:
            place = "; ".join(r.name or r.address for r in booked)
        fields = ec.EventFields(
            subject=subject.strip() if subject else None,
            html=text_to_html(body) if body else None,
            reminder=reminder_minutes,
            start=ews_time(s) if s is not None and (start or duration is not None or existing is None) else None,
            end=ews_time(e) if e is not None else None,
            show_as=ec.SHOW_AS[show_as] if show_as else None,
            location=place,
            required=[p.address for p in required] if required is not None else None,
            rooms=[r.address for r in booked] if booked is not None else None,
            recurrence=rule,
            private=private,
            time_zone=windows_zone(c.rt.settings.timezone) if (rule is not None or s is not None) else None,
        )
        receipt: dict[str, Any] = {}
        if told:
            receipt = c.guard(kind="invite", user_confirmed=user_confirmed, recipients=told)
        if existing is None:
            eid = await ec.create_event(c.ews, fields, send=bool(told))
        else:
            eid = existing.id
            await ec.update_event(c.ews, eid, existing.change_key, fields, send=bool(told))
        return compact(
            {
                "saved": "created" if existing is None else "updated",
                "id": eid,
                "start": local(fields.start, tz),
                "end": local(fields.end, tz),
                "attendees": fields.required,
                "rooms": fields.rooms,
                "invitations": ("sent" if existing is None else "updates sent") if told else None,
                **receipt,
            }
        )


@tool(area="calendar", title="Answer an invitation", notifies_others=True)
async def respond_to_event(
    id: ID,
    response: Literal["accept", "tentative", "decline"],
    comment: Annotated[str | None, Field(description="A short note to the organiser.", max_length=2000)] = None,
    send_response: Annotated[
        bool, Field(description="Tell the organiser; false records the answer in the calendar only.")
    ] = True,
) -> dict[str, Any]:
    """Accepts, tentatively accepts or declines a meeting the person was invited to, and by default tells
    the organiser. Call it when the person says they will or will not attend. It is the person's own answer
    and needs no confirmation; it refuses the person's own meetings, which have nothing to answer. A
    declined meeting leaves the calendar."""
    async with call("respond_to_event") as c:
        ev = await ec.get_event(c.ews, id, body=False)
        if ev.organised_by(c.me):
            raise invalid("You organise this meeting, so there is no invitation to answer. Nothing was sent.")
        receipt = c.rt.guard.spend(c.person, kind="invite") if send_response else {}
        await ec.respond(c.ews, ev.id, ev.change_key, response, comment, send=send_response)
        return {"response": response, "organiser_told": send_response, **receipt}


@tool(area="calendar", title="Cancel an event", notifies_others=True, irreversible=True)
async def cancel_event(
    id: ID,
    comment: Annotated[str | None, Field(description="A note sent with the cancellation.", max_length=2000)] = None,
    user_confirmed: Annotated[
        bool, Field(description="True only after the person agreed to cancel a meeting with attendees.")
    ] = False,
) -> dict[str, Any]:
    """Cancels a meeting the person organises and tells every attendee, or deletes an event without
    attendees to Deleted Items. Call it when the person wants a meeting off; confirm first, because a
    cancellation cannot be undone once attendees have it. Only the organiser cancels; an invitee
    declines with respond_to_event instead."""
    async with call("cancel_event") as c:
        ev = await ec.get_event(c.ews, id, body=False)
        if not ev.organised_by(c.me):
            raise ExchangeError("not_organiser")
        told = len(ev.attendees) + len(ev.rooms)
        if not told:
            await move_to_deleted(c.ews, [ev.id])
            return {"deleted": ev.id, "where": "Deleted Items"}
        receipt = c.guard(kind="cancel", user_confirmed=user_confirmed, recipients=told)
        await ec.cancel(c.ews, ev.id, ev.change_key, comment)
        return {"cancelled": ev.id, "attendees_told": told, **receipt}
