"""EWS for the calendar: the calendar view, one event, availability, saving, answering and cancelling.

Functions here build the request, call EWS and return plain values. They decide nothing about the
person's word, the bucket or who organises a meeting: the tools do that before calling them.
Times go to EWS in UTC (`core.time.ews`) and come back as EWS wrote them; the tools make them local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from lxml import etree

from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.common import mailbox
from exchange_mcp.ews.soap import M, T, find, findall, flag, text

LIST_FIELDS = (
    "item:Subject",
    "item:Sensitivity",
    "item:DisplayTo",
    "item:DisplayCc",
    "calendar:Start",
    "calendar:End",
    "calendar:IsAllDayEvent",
    "calendar:LegacyFreeBusyStatus",
    "calendar:Location",
    "calendar:Organizer",
    "calendar:MyResponseType",
    "calendar:CalendarItemType",
    "calendar:IsMeeting",
    "calendar:IsCancelled",
    "calendar:JoinOnlineMeetingUrl",
)

DETAIL_FIELDS = (
    *LIST_FIELDS,
    "item:Attachments",
    "item:ReminderIsSet",
    "item:ReminderMinutesBeforeStart",
    "calendar:RequiredAttendees",
    "calendar:OptionalAttendees",
    "calendar:Resources",
    "calendar:Recurrence",
)

# CalendarView answers at most this many occurrences per request; the tool advances the window.
VIEW_BATCH = 200

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
SHOW_AS = {"free": "Free", "tentative": "Tentative", "busy": "Busy", "away": "OOF", "elsewhere": "WorkingElsewhere"}


def _fields(*uris: str) -> etree._Element:
    return T.AdditionalProperties(*[T.FieldURI(FieldURI=u) for u in uris])


@dataclass
class Attendee:
    name: str
    address: str
    response: str


@dataclass
class EventAttachment:
    id: str
    name: str
    size: int
    content_type: str
    inline: bool
    kind: str  # file | item


@dataclass
class Event:
    id: str
    change_key: str
    subject: str
    start: str
    end: str
    all_day: bool
    show_as: str
    location: str
    organizer: dict[str, str]
    my_response: str
    kind: str  # Single | Occurrence | Exception | RecurringMaster
    is_meeting: bool
    cancelled: bool
    private: bool
    online_url: str
    invitee_count: int
    html: str = ""
    attendees: list[Attendee] = field(default_factory=list)
    rooms: list[Attendee] = field(default_factory=list)
    recurrence: str = ""
    reminder: int | None = None
    attachments: list[EventAttachment] = field(default_factory=list)

    def organised_by(self, me: str) -> bool:
        """The person organises this event: Exchange says so, or the organiser's address is theirs."""
        return self.my_response == "Organizer" or self.organizer.get("address", "").lower() == me.lower()


def _attendees(el: etree._Element, path: str) -> list[Attendee]:
    out = []
    for a in findall(el, f"{path}/t:Attendee"):
        mb = mailbox(find(a, "t:Mailbox"))
        out.append(Attendee(mb.get("name", ""), mb.get("address", ""), text(a, "t:ResponseType", "Unknown")))
    return out


def _display_count(value: str) -> int:
    return len([p for p in value.split(";") if p.strip()])


def _event(el: etree._Element) -> Event:
    iid = el.find("{*}ItemId")
    atts = []
    for a in findall(el, "t:Attachments/*"):
        aid = a.find("{*}AttachmentId")
        atts.append(
            EventAttachment(
                id=aid.get("Id") if aid is not None else "",
                name=text(a, "t:Name", "attachment"),
                size=int(text(a, "t:Size", "0") or 0),
                content_type=text(a, "t:ContentType", "application/octet-stream"),
                inline=flag(a, "t:IsInline"),
                kind="file" if etree.QName(a).localname == "FileAttachment" else "item",
            )
        )
    required = _attendees(el, "t:RequiredAttendees")
    optional = _attendees(el, "t:OptionalAttendees")
    rooms = _attendees(el, "t:Resources")
    listed = len(required) + len(optional) + len(rooms)
    reminder = None
    if flag(el, "t:ReminderIsSet") and text(el, "t:ReminderMinutesBeforeStart").isdigit():
        reminder = int(text(el, "t:ReminderMinutesBeforeStart"))
    return Event(
        id=iid.get("Id"),
        change_key=iid.get("ChangeKey", ""),
        subject=text(el, "t:Subject"),
        start=text(el, "t:Start"),
        end=text(el, "t:End"),
        all_day=flag(el, "t:IsAllDayEvent"),
        show_as=text(el, "t:LegacyFreeBusyStatus", "Busy"),
        location=text(el, "t:Location"),
        organizer=mailbox(find(el, "t:Organizer/t:Mailbox")),
        my_response=text(el, "t:MyResponseType", "Unknown"),
        kind=text(el, "t:CalendarItemType", "Single"),
        is_meeting=flag(el, "t:IsMeeting"),
        cancelled=flag(el, "t:IsCancelled"),
        private=text(el, "t:Sensitivity") in ("Private", "Confidential"),
        online_url=text(el, "t:JoinOnlineMeetingUrl"),
        invitee_count=listed or _display_count(text(el, "t:DisplayTo")) + _display_count(text(el, "t:DisplayCc")),
        html=text(el, "t:Body"),
        attendees=required + optional,
        rooms=rooms,
        recurrence=describe_recurrence(find(el, "t:Recurrence")),
        reminder=reminder,
        attachments=atts,
    )


@dataclass
class View:
    events: list[Event]
    complete: bool  # Exchange returned the last occurrence in the window


async def calendar_view(ews: Ews, start: str, end: str, max_entries: int = VIEW_BATCH) -> View:
    """Occurrences overlapping [start, end), recurrences expanded, in start order. CalendarView does not
    page: when the window holds more than max_entries, Exchange returns the first ones and says so."""
    messages = await ews.call(
        M.FindItem(
            M.ItemShape(T.BaseShape("IdOnly"), _fields(*LIST_FIELDS)),
            M.CalendarView(MaxEntriesReturned=str(max_entries), StartDate=start, EndDate=end),
            M.ParentFolderIds(T.DistinguishedFolderId(Id="calendar")),
            Traversal="Shallow",
        )
    )
    root = find(messages[0], "m:RootFolder")
    complete = root is None or root.get("IncludesLastItemInRange", "true") == "true"
    return View([_event(e) for e in findall(root, "t:Items/t:CalendarItem")], complete)


async def get_event(ews: Ews, item: str, *, body: bool = True) -> Event:
    uris = list(DETAIL_FIELDS) + (["item:Body"] if body else [])
    shape = M.ItemShape(T.BaseShape("IdOnly"), T.BodyType("HTML"), _fields(*uris))
    messages = await ews.call(M.GetItem(shape, M.ItemIds(T.ItemId(Id=item))))
    found = findall(messages[0], "m:Items/*")
    if not found:
        raise ExchangeError("not_found")
    if etree.QName(found[0]).localname != "CalendarItem":
        from exchange_mcp.core.errors import invalid

        raise invalid("That id is not a calendar event. Find events with list_events. Nothing was done.")
    return _event(found[0])


# ---------------------------------------------------------------- recurrence


def recurrence(kind: str, first: date, *, until: date | None, count: int | None) -> etree._Element:
    """A t:Recurrence: one pattern, then one range (EndDateRecurrence or NumberedRecurrence)."""
    if kind == "daily":
        pattern = T.DailyRecurrence(T.Interval("1"))
    elif kind == "weekdays":
        pattern = T.WeeklyRecurrence(T.Interval("1"), T.DaysOfWeek(" ".join(WEEKDAYS[:5])))
    elif kind == "weekly":
        pattern = T.WeeklyRecurrence(T.Interval("1"), T.DaysOfWeek(WEEKDAYS[first.weekday()]))
    elif kind == "monthly":
        pattern = T.AbsoluteMonthlyRecurrence(T.Interval("1"), T.DayOfMonth(str(first.day)))
    elif kind == "yearly":
        pattern = T.AbsoluteYearlyRecurrence(T.DayOfMonth(str(first.day)), T.Month(MONTHS[first.month - 1]))
    else:
        raise ValueError(kind)
    if until is not None:
        rng = T.EndDateRecurrence(T.StartDate(first.isoformat()), T.EndDate(until.isoformat()))
    else:
        rng = T.NumberedRecurrence(T.StartDate(first.isoformat()), T.NumberOfOccurrences(str(count)))
    return T.Recurrence(pattern, rng)


def describe_recurrence(el: etree._Element | None) -> str:
    """A recurrence in words: "every week on Monday, Wednesday until 2026-12-31"."""
    if el is None or len(el) == 0:
        return ""
    pattern = el[0]
    kind = etree.QName(pattern).localname
    n = text(pattern, "t:Interval", "1")
    every = "every" if n == "1" else f"every {n}"
    days = ", ".join(text(pattern, "t:DaysOfWeek").split())
    index = text(pattern, "t:DayOfWeekIndex").lower()
    words = {
        "DailyRecurrence": f"{every} day" + ("" if n == "1" else "s"),
        "WeeklyRecurrence": f"{every} week" + ("" if n == "1" else "s") + f" on {days}",
        "AbsoluteMonthlyRecurrence": f"{every} month" + ("" if n == "1" else "s") + f" on day "
        f"{text(pattern, 't:DayOfMonth')}",
        "RelativeMonthlyRecurrence": f"{every} month" + ("" if n == "1" else "s") + f" on the {index} {days}",
        "AbsoluteYearlyRecurrence": f"every year on {text(pattern, 't:DayOfMonth')} {text(pattern, 't:Month')}",
        "RelativeYearlyRecurrence": f"every year on the {index} {days} of {text(pattern, 't:Month')}",
    }.get(kind, "repeating")
    end = el[1] if len(el) > 1 else None
    if end is not None:
        end_kind = etree.QName(end).localname
        if end_kind == "EndDateRecurrence":
            words += f" until {text(end, 't:EndDate')[:10]}"
        elif end_kind == "NumberedRecurrence":
            words += f", {text(end, 't:NumberOfOccurrences')} times"
        else:
            words += ", with no end"
    return words


# ---------------------------------------------------------------- saving


@dataclass
class EventFields:
    """What save_event writes; None leaves a field as it is (on update) or unset (on create)."""

    subject: str | None = None
    html: str | None = None
    reminder: int | None = None
    start: str | None = None
    end: str | None = None
    show_as: str | None = None
    location: str | None = None
    required: list[str] | None = None
    rooms: list[str] | None = None
    recurrence: etree._Element | None = None
    private: bool | None = None
    time_zone: str | None = None  # Windows zone name; anchors a recurring series to local time


def _mailboxes(tag: str, addresses: list[str]) -> etree._Element:
    return getattr(T, tag)(*[T.Attendee(T.Mailbox(T.EmailAddress(a))) for a in addresses])


def _calendar_item(f: EventFields) -> etree._Element:
    """A t:CalendarItem in the order of the EWS schema: ItemType fields (Subject, Sensitivity, Body,
    ReminderIsSet, ReminderMinutesBeforeStart), then Start, End, LegacyFreeBusyStatus, Location,
    RequiredAttendees, Resources, Recurrence."""
    item = T.CalendarItem()
    if f.subject is not None:
        item.append(T.Subject(f.subject))
    if f.private is not None:
        item.append(T.Sensitivity("Private" if f.private else "Normal"))
    if f.html is not None:
        item.append(T.Body(f.html, BodyType="HTML"))
    if f.reminder is not None:
        item.append(T.ReminderIsSet("true" if f.reminder >= 0 else "false"))
        if f.reminder >= 0:
            item.append(T.ReminderMinutesBeforeStart(str(f.reminder)))
    if f.start is not None:
        item.append(T.Start(f.start))
    if f.end is not None:
        item.append(T.End(f.end))
    if f.show_as is not None:
        item.append(T.LegacyFreeBusyStatus(f.show_as))
    if f.location is not None:
        item.append(T.Location(f.location))
    if f.required:
        item.append(_mailboxes("RequiredAttendees", f.required))
    if f.rooms:
        item.append(_mailboxes("Resources", f.rooms))
    if f.recurrence is not None:
        item.append(f.recurrence)
    if f.time_zone:  # after Recurrence in the schema sequence
        item.append(T.StartTimeZone(Id=f.time_zone))
        item.append(T.EndTimeZone(Id=f.time_zone))
    return item


async def create_event(ews: Ews, f: EventFields, *, send: bool) -> str:
    """Saves a new event; `send` sends the invitations (the tool has already had the person's word)."""
    messages = await ews.call(
        M.CreateItem(
            M.SavedItemFolderId(T.DistinguishedFolderId(Id="calendar")),
            M.Items(_calendar_item(f)),
            SendMeetingInvitations="SendToAllAndSaveCopy" if send else "SendToNone",
        )
    )
    ids = findall(messages[0], "m:Items/*/t:ItemId")
    if not ids:
        raise ExchangeError("uncertain", ref="create-without-id")
    return ids[0].get("Id")


_URIS = (
    ("subject", "item:Subject"),
    ("private", "item:Sensitivity"),
    ("html", "item:Body"),
    ("reminder", "item:ReminderMinutesBeforeStart"),
    ("start", "calendar:Start"),
    ("end", "calendar:End"),
    ("show_as", "calendar:LegacyFreeBusyStatus"),
    ("location", "calendar:Location"),
    ("required", "calendar:RequiredAttendees"),
    ("rooms", "calendar:Resources"),
    ("recurrence", "calendar:Recurrence"),
)


async def update_event(ews: Ews, item: str, ck: str, f: EventFields, *, send: bool) -> None:
    """Changes the given fields of an event the person organises; `send` sends the update to attendees."""
    updates = []
    if f.time_zone:
        for uri, tag in (("calendar:StartTimeZone", "StartTimeZone"), ("calendar:EndTimeZone", "EndTimeZone")):
            updates.append(T.SetItemField(T.FieldURI(FieldURI=uri), T.CalendarItem(getattr(T, tag)(Id=f.time_zone))))
    for attr, uri in _URIS:
        value = getattr(f, attr)
        if value is None:
            continue
        if attr == "reminder":
            updates.append(
                T.SetItemField(
                    T.FieldURI(FieldURI="item:ReminderIsSet"),
                    T.CalendarItem(T.ReminderIsSet("true" if value >= 0 else "false")),
                )
            )
            if value >= 0:
                updates.append(
                    T.SetItemField(T.FieldURI(FieldURI=uri), T.CalendarItem(T.ReminderMinutesBeforeStart(str(value))))
                )
            continue
        if attr in ("required", "rooms") and not value:
            updates.append(T.DeleteItemField(T.FieldURI(FieldURI=uri)))
            continue
        one = _calendar_item(EventFields(**{attr: value}))
        updates.append(T.SetItemField(T.FieldURI(FieldURI=uri), one))
    if not updates:
        return
    await ews.call(
        M.UpdateItem(
            M.ItemChanges(T.ItemChange(T.ItemId(Id=item, ChangeKey=ck), T.Updates(*updates))),
            ConflictResolution="AutoResolve",
            SendMeetingInvitationsOrCancellations="SendToChangedAndSaveCopy" if send else "SendToNone",
        )
    )


RESPONSES = {"accept": "AcceptItem", "tentative": "TentativelyAcceptItem", "decline": "DeclineItem"}


async def respond(ews: Ews, item: str, ck: str, response: str, comment: str | None, *, send: bool) -> None:
    """The person's own answer to an invitation. `send=False` records it without telling the organiser."""
    resp = getattr(T, RESPONSES[response])()
    if comment:
        resp.append(T.Body(comment, BodyType="Text"))
    resp.append(T.ReferenceItemId(Id=item, ChangeKey=ck))
    await send_meeting_response(ews, resp, send=send)


async def cancel(ews: Ews, item: str, ck: str, comment: str | None) -> None:
    """The organiser cancels a meeting; Exchange tells every attendee. The tool has the person's word."""
    resp = T.CancelCalendarItem(T.ReferenceItemId(Id=item, ChangeKey=ck))
    if comment:
        resp.append(T.NewBodyContent(comment, BodyType="Text"))
    await send_meeting_response(ews, resp, send=True)


MEETING_RESPONSES = frozenset({"AcceptItem", "TentativelyAcceptItem", "DeclineItem", "CancelCalendarItem"})


async def send_meeting_response(ews: Ews, response: etree._Element, *, send: bool) -> None:
    """The one place outside send_draft where Exchange sends: an answer to an invitation, or the
    organiser's cancellation. Anything else is refused here, before EWS."""
    if etree.QName(response).localname not in MEETING_RESPONSES:
        raise ValueError("send_meeting_response sends meeting responses only")
    await ews.call(M.CreateItem(M.Items(response), MessageDisposition="SendAndSaveCopy" if send else "SaveOnly"))


# ---------------------------------------------------------------- availability


def _utc_zone() -> etree._Element:
    """SerializableTimeZone for UTC: request and answer times are then plain UTC.

    Live (2016 CU23): Exchange answers "The specified time zone isn't valid" when the standard and
    daylight rules are the same transition. Two different transition dates with zero bias each are
    accepted and still mean UTC all year."""

    def rule(month: str) -> list[etree._Element]:
        return [T.Bias("0"), T.Time("02:00:00"), T.DayOrder("5"), T.Month(month), T.DayOfWeek("Sunday")]

    return T.TimeZone(T.Bias("0"), T.StandardTime(*rule("10")), T.DaylightTime(*rule("3")))


def _naive(dt: datetime) -> str:
    from datetime import UTC

    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Busy:
    start: str
    end: str
    kind: str  # Busy | Tentative | OOF | WorkingElsewhere | NoData


@dataclass
class Availability:
    address: str
    known: bool
    busy: list[Busy]


@dataclass
class Suggestion:
    start: str
    quality: str
    work_time: bool


async def availability(
    ews: Ews,
    people: list[tuple[str, str]],
    start: datetime,
    end: datetime,
    *,
    duration: int | None,
    working_hours_only: bool,
) -> tuple[list[Availability], list[Suggestion]]:
    """Free/busy of each (address, "Required"|"Room") in [start, end), and suggested start times when
    duration is given. Times in the answer are UTC without an offset."""
    body = M.GetUserAvailabilityRequest(
        _utc_zone(),
        M.MailboxDataArray(
            *[
                T.MailboxData(T.Email(T.Address(addr)), T.AttendeeType(kind), T.ExcludeConflicts("false"))
                for addr, kind in people
            ]
        ),
        T.FreeBusyViewOptions(
            T.TimeWindow(T.StartTime(_naive(start)), T.EndTime(_naive(end))),
            T.MergedFreeBusyIntervalInMinutes("30"),
            T.RequestedView("FreeBusy"),
        ),
    )
    if duration:
        body.append(
            T.SuggestionsViewOptions(
                T.GoodThreshold("25"),
                T.MaximumResultsByDay("10"),
                T.MaximumNonWorkHourResultsByDay("0" if working_hours_only else "10"),
                T.MeetingDurationInMinutes(str(duration)),
                T.MinimumSuggestionQuality("Excellent"),  # Excellent: everyone free; Good allows conflicts
                T.DetailedSuggestionsWindow(T.StartTime(_naive(start)), T.EndTime(_naive(end))),
            )
        )
    messages = await ews.call(body)
    root = messages[0]
    out = []
    responses = list(root.iter("{*}FreeBusyResponse"))
    for (addr, _), r in zip(people, responses, strict=False):
        msg = r.find("{*}ResponseMessage")
        known = msg is None or msg.get("ResponseClass") != "Error"
        busy = []
        for ev in r.iter("{*}CalendarEvent"):
            kind = text(ev, "t:BusyType", "Busy")
            if kind != "Free":
                busy.append(Busy(text(ev, "t:StartTime"), text(ev, "t:EndTime"), kind))
        out.append(Availability(addr, known, busy))
    for addr, _ in people[len(responses) :]:
        out.append(Availability(addr, False, []))
    sr = next(root.iter("{*}SuggestionsResponse"), None)
    msg = sr.find("{*}ResponseMessage") if sr is not None else None
    if msg is not None and msg.get("ResponseClass") == "Error":
        raise ExchangeError(
            "failed", code=text(msg, "m:ResponseCode", "ErrorUnknown"), ref="suggestions", outcome="Nothing was read."
        )
    suggestions = []
    for s in root.iter("{*}Suggestion"):
        suggestions.append(
            Suggestion(text(s, "t:MeetingTime"), text(s, "t:SuggestionQuality", "Fair"), flag(s, "t:IsWorkTime"))
        )
    return out, suggestions


__all__ = [
    "SHOW_AS",
    "Availability",
    "Event",
    "EventFields",
    "availability",
    "calendar_view",
    "cancel",
    "create_event",
    "get_event",
    "recurrence",
    "respond",
    "update_event",
]
