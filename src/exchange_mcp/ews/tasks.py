"""EWS for the person's own tasks: list, save, and check that an id is a task.

Nothing here assigns a task to anyone: no TaskRequest, no Delegator, no Owner is ever written.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from exchange_mcp.core.errors import ExchangeError, invalid
from exchange_mcp.ews.client import Ews
from exchange_mcp.ews.common import Page, page_of, paged_view
from exchange_mcp.ews.soap import M, T, findall, flag, text

STATUS = {
    "not_started": "NotStarted",
    "in_progress": "InProgress",
    "completed": "Completed",
    "waiting": "WaitingOnOthers",
    "deferred": "Deferred",
}
STATUS_BACK = {v: k for k, v in STATUS.items()}

LIST_FIELDS = (
    "item:Subject",
    "item:Importance",
    "item:ReminderIsSet",
    "item:ReminderDueBy",
    "task:DueDate",
    "task:StartDate",
    "task:Status",
    "task:PercentComplete",
    "task:CompleteDate",
)


@dataclass
class Task:
    id: str
    subject: str
    due: str
    start: str
    status: str
    importance: str
    percent: int
    completed: str
    reminder: str


def _task(el: etree._Element) -> Task:
    pct = text(el, "t:PercentComplete", "0").split(".")[0]
    return Task(
        id=el.find("{*}ItemId").get("Id"),
        subject=text(el, "t:Subject"),
        due=text(el, "t:DueDate"),
        start=text(el, "t:StartDate"),
        status=STATUS_BACK.get(text(el, "t:Status", "NotStarted"), "not_started"),
        importance=text(el, "t:Importance", "Normal"),
        percent=int(pct) if pct.isdigit() else 0,
        completed=text(el, "t:CompleteDate"),
        reminder=text(el, "t:ReminderDueBy") if flag(el, "t:ReminderIsSet") else "",
    )


def _const(uri: str, value: str, op: str) -> etree._Element:
    return getattr(T, op)(T.FieldURI(FieldURI=uri), T.FieldURIOrConstant(T.Constant(Value=value)))


async def find_tasks(
    ews: Ews, *, status: str, query: str | None, due_before: str | None, offset: int, limit: int
) -> tuple[list[Task], Page]:
    terms = []
    if status == "open":
        terms.append(_const("task:Status", "Completed", "IsNotEqualTo"))
    elif status == "completed":
        terms.append(_const("task:Status", "Completed", "IsEqualTo"))
    if due_before:
        terms.append(_const("task:DueDate", due_before, "IsLessThan"))
    if query:
        terms.append(
            T.Contains(
                T.FieldURI(FieldURI="item:Subject"),
                T.Constant(Value=query),
                ContainmentMode="Substring",
                ContainmentComparison="IgnoreCase",
            )
        )
    body = M.FindItem(
        M.ItemShape(T.BaseShape("IdOnly"), T.AdditionalProperties(*[T.FieldURI(FieldURI=u) for u in LIST_FIELDS])),
        paged_view(offset, limit),
        Traversal="Shallow",
    )
    if terms:
        body.append(M.Restriction(terms[0] if len(terms) == 1 else T.And(*terms)))
    body.append(M.SortOrder(T.FieldOrder(T.FieldURI(FieldURI="task:DueDate"), Order="Ascending")))
    body.append(M.ParentFolderIds(T.DistinguishedFolderId(Id="tasks")))
    messages = await ews.call(body)
    page = page_of(messages[0], "t:Items", offset, limit)
    return [_task(e) for e in page.elements if etree.QName(e).localname == "Task"], page


async def task_change_key(ews: Ews, item: str) -> str:
    """The current ChangeKey of one of the person's tasks; anything that is not a task is refused."""
    messages = await ews.call(M.GetItem(M.ItemShape(T.BaseShape("IdOnly")), M.ItemIds(T.ItemId(Id=item))))
    found = findall(messages[0], "m:Items/*")
    if not found:
        raise ExchangeError("not_found")
    if etree.QName(found[0]).localname != "Task":
        raise invalid("That id is not a task. Find tasks with list_tasks. Nothing was done.")
    return found[0].find("{*}ItemId").get("ChangeKey", "")


@dataclass
class TaskFields:
    """None leaves a field as it is. Times are EWS UTC strings."""

    subject: str | None = None
    body: str | None = None
    importance: str | None = None
    reminder: str | None = None
    due: str | None = None
    start: str | None = None
    status: str | None = None  # EWS value


def _task_el(f: TaskFields) -> etree._Element:
    """A t:Task in schema order: Subject, Body, Importance, ReminderDueBy, ReminderIsSet, DueDate,
    StartDate, Status."""
    t = T.Task()
    if f.subject is not None:
        t.append(T.Subject(f.subject))
    if f.body is not None:
        t.append(T.Body(f.body, BodyType="Text"))
    if f.importance is not None:
        t.append(T.Importance(f.importance))
    if f.reminder is not None:
        t.append(T.ReminderDueBy(f.reminder))
        t.append(T.ReminderIsSet("true"))
    if f.due is not None:
        t.append(T.DueDate(f.due))
    if f.start is not None:
        t.append(T.StartDate(f.start))
    if f.status is not None:
        t.append(T.Status(f.status))
    return t


async def create_task(ews: Ews, f: TaskFields) -> str:
    messages = await ews.call(
        M.CreateItem(M.SavedItemFolderId(T.DistinguishedFolderId(Id="tasks")), M.Items(_task_el(f)))
    )
    ids = findall(messages[0], "m:Items/*/t:ItemId")
    if not ids:
        raise ExchangeError("uncertain", ref="create-without-id")
    return ids[0].get("Id")


_URIS = (
    ("subject", "item:Subject"),
    ("body", "item:Body"),
    ("importance", "item:Importance"),
    ("reminder", "item:ReminderDueBy"),
    ("due", "task:DueDate"),
    ("start", "task:StartDate"),
    ("status", "task:Status"),
)


async def update_task(ews: Ews, item: str, ck: str, f: TaskFields) -> None:
    updates = []
    for attr, uri in _URIS:
        value = getattr(f, attr)
        if value is None:
            continue
        if attr == "reminder":
            updates.append(T.SetItemField(T.FieldURI(FieldURI=uri), T.Task(T.ReminderDueBy(value))))
            updates.append(T.SetItemField(T.FieldURI(FieldURI="item:ReminderIsSet"), T.Task(T.ReminderIsSet("true"))))
            continue
        updates.append(T.SetItemField(T.FieldURI(FieldURI=uri), _task_el(TaskFields(**{attr: value}))))
    if not updates:
        return
    await ews.call(
        M.UpdateItem(
            M.ItemChanges(T.ItemChange(T.ItemId(Id=item, ChangeKey=ck), T.Updates(*updates))),
            ConflictResolution="AutoResolve",
        )
    )


__all__ = ["STATUS", "Task", "TaskFields", "create_task", "find_tasks", "task_change_key", "update_task"]
