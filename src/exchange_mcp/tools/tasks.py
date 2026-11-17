"""tasks: the person's own tasks. There is no way to assign a task to someone else, by design."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from exchange_mcp.core.context import call
from exchange_mcp.core.errors import invalid
from exchange_mcp.core.project import clamp_limit, compact, page_result
from exchange_mcp.core.time import ews as ews_time
from exchange_mcp.core.time import local, parse
from exchange_mcp.ews import tasks as et
from exchange_mcp.ews.common import move_to_deleted
from exchange_mcp.registry import tool

ID = Annotated[str, Field(description="A task id from list_tasks.", min_length=1, max_length=512)]


def _row(t: et.Task, tz) -> dict[str, Any]:
    return compact(
        {
            "id": t.id,
            "subject": t.subject[:255],
            "status": t.status,
            "due": (local(t.due, tz) or "")[:10] or None,
            "start": (local(t.start, tz) or "")[:10] or None,
            "importance": t.importance.lower() if t.importance != "Normal" else None,
            "percent_complete": t.percent or None,
            "completed": local(t.completed, tz),
            "reminder": local(t.reminder, tz),
        }
    )


@tool(area="tasks", title="List tasks", read_only=True, idempotent=True, returns_list=True)
async def list_tasks(
    status: Annotated[
        Literal["open", "completed", "all"], Field(description="open: not completed; completed; all.")
    ] = "open",
    query: Annotated[str | None, Field(description="Words in the subject.", max_length=256)] = None,
    due_before: Annotated[str | None, Field(description="Only tasks due before this local date or time.")] = None,
    limit: Annotated[int | None, Field(description="How many, 1 to 25; 10 by default.")] = None,
    cursor: Annotated[str | None, Field(description="The cursor from the previous page.")] = None,
) -> dict[str, Any]:
    """Lists the person's own tasks from their Tasks folder, soonest due first, with status, due date and
    importance. Call it to answer what is on the person's to-do list, what is overdue, or to find a task's
    id before changing or deleting it. It reads only."""
    async with call("list_tasks") as c:
        n, extra = clamp_limit(limit)
        scope = {"s": status, "q": query or "", "d": due_before or ""}
        start = (await c.cursor_in(cursor, scope) or {}).get("o", 0)
        due = ews_time(parse(due_before, c.tz, field="due_before")) if due_before else None
        found, page = await et.find_tasks(
            c.ews, status=status, query=(query or "").strip() or None, due_before=due, offset=start, limit=n
        )
        nxt = await c.cursor_out({"o": page.next_offset} if page.next_offset is not None else None, scope)
        return page_result([_row(t, c.tz) for t in found], nxt, total=page.total, **extra)


@tool(area="tasks", title="Save a task")
async def save_task(
    id: Annotated[str | None, Field(description="A task id to change it; absent creates one.", max_length=512)] = None,
    subject: Annotated[str | None, Field(description="Required for a new task.", max_length=255)] = None,
    due: Annotated[str | None, Field(description="Local date or time, 2026-11-20.")] = None,
    start: Annotated[str | None, Field(description="Local date or time work starts.")] = None,
    status: Literal["not_started", "in_progress", "completed", "waiting", "deferred"] | None = None,
    importance: Literal["low", "normal", "high"] | None = None,
    body: str | None = None,
    reminder: Annotated[str | None, Field(description="Local time of a reminder, 2026-11-20 09:00.")] = None,
) -> dict[str, Any]:
    """Creates a task in the person's own Tasks folder, or changes one from list_tasks; status completed
    marks it done. Call it when the person wants to note a to-do, set a due date or reminder, or record
    progress. Fields left out stay as they are. Tasks stay the person's own: none is assigned or sent."""
    async with call("save_task") as c:
        tz = c.tz
        fields = et.TaskFields(
            subject=subject.strip() if subject else None,
            body=body,
            importance=importance.capitalize() if importance else None,
            reminder=ews_time(parse(reminder, tz, field="reminder")) if reminder else None,
            due=ews_time(parse(due, tz, field="due")) if due else None,
            start=ews_time(parse(start, tz, field="start")) if start else None,
            status=et.STATUS[status] if status else None,
        )
        if due and start and parse(due, tz) < parse(start, tz):
            raise invalid("due is before start. Nothing was saved.")
        if id is None:
            if not fields.subject:
                raise invalid("A new task needs subject. Nothing was saved.")
            tid = await et.create_task(c.ews, fields)
            return compact({"saved": "created", "id": tid, "subject": fields.subject, "due": due})
        ck = await et.task_change_key(c.ews, id)
        await et.update_task(c.ews, id, ck, fields)
        return compact({"saved": "updated", "id": id, "status": status})


@tool(area="tasks", title="Delete a task")
async def delete_task(id: ID) -> dict[str, Any]:
    """Moves one of the person's tasks to Deleted Items. Call it when the person asks to remove a task
    found with list_tasks; to mark a task done, use save_task with status completed instead. It refuses
    ids that are not tasks; the task can be moved back until Deleted Items is emptied."""
    async with call("delete_task") as c:
        await et.task_change_key(c.ews, id)
        await move_to_deleted(c.ews, [id])
        return {"deleted": id, "where": "Deleted Items"}
