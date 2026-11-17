"""tasks: what each tool sends to EWS, and every refusal in docs/requirements.md."""

from __future__ import annotations

import inspect

from conftest import ok

from exchange_mcp.registry import all_tools

CREATED = ok("CreateItem", '<m:Items><t:Task><t:ItemId Id="T9" ChangeKey="K"/></t:Task></m:Items>')
ASSIGNING = {"assignee", "assign_to", "assigned_to", "owner", "delegate", "delegate_to", "to"}


def got(kind: str = "Task") -> bytes:
    return ok("GetItem", f'<m:Items><t:{kind}><t:ItemId Id="T1" ChangeKey="CK1"/></t:{kind}></m:Items>')


async def test_tasks_are_not_assigned_to_others(rt, ews, invoke):
    for spec in all_tools().values():
        if spec.area == "tasks":
            assert not ASSIGNING & set(inspect.signature(spec.fn).parameters), spec.name
    r = await invoke("save_task", subject="Report", assignee="pat@corp.example")
    assert r.is_error and "has no argument assignee" in r.text
    assert ews.ops() == []
    ews.on("CreateItem", CREATED).on("GetItem", got()).on("UpdateItem", ok("UpdateItem"))
    await invoke("save_task", subject="Report", status="waiting", due="2026-11-20")
    await invoke("save_task", id="T1", status="completed")
    for seen in ews.seen:
        xml = seen.xml()
        for forbidden in ("TaskRequest", "Delegator", "DelegationState", "Owner", "Recipients"):
            assert forbidden not in xml, forbidden


async def test_a_new_task_is_written_in_schema_order(rt, ews, invoke):
    ews.on("CreateItem", CREATED)
    r = await invoke(
        "save_task",
        subject="Report",
        body="Numbers",
        importance="high",
        reminder="2026-11-19 09:00",
        due="2026-11-20",
        start="2026-11-18",
        status="in_progress",
    )
    assert not r.is_error, r.text
    task = ews.last("CreateItem").body.find(".//{*}Task")
    assert [c.tag.split("}")[1] for c in task] == [
        "Subject",
        "Body",
        "Importance",
        "ReminderDueBy",
        "ReminderIsSet",
        "DueDate",
        "StartDate",
        "Status",
    ]
    assert task.find("{*}Status").text == "InProgress"
    assert task.find("{*}ReminderDueBy").text == "2026-11-19T08:00:00Z"


async def test_list_tasks_filters_open_tasks(rt, ews, invoke):
    ews.on(
        "FindItem",
        ok(
            "FindItem",
            '<m:RootFolder TotalItemsInView="1" IncludesLastItemInRange="true"><t:Items><t:Task>'
            '<t:ItemId Id="T1" ChangeKey="CK"/><t:Subject>Report</t:Subject><t:DueDate>2026-11-19T23:00:00Z</t:DueDate>'
            "<t:Status>NotStarted</t:Status></t:Task></t:Items></m:RootFolder>",
        ),
    )
    r = await invoke("list_tasks", due_before="2026-11-21")
    assert not r.is_error, r.text
    assert r.data["items"] == [{"id": "T1", "subject": "Report", "status": "not_started", "due": "2026-11-20"}]
    xml = ews.last("FindItem").xml()
    assert "IsNotEqualTo" in xml and 'Id="tasks"' in xml and "2026-11-20T23:00:00Z" in xml
    assert ews.writes() == []


async def test_delete_task_refuses_what_is_not_a_task(rt, ews, invoke):
    ews.on("GetItem", got("Message"))
    r = await invoke("delete_task", id="T1")
    assert r.is_error and "not a task" in r.text
    assert ews.writes() == []
    ews.on("GetItem", got()).on("DeleteItem", ok("DeleteItem"))
    r = await invoke("delete_task", id="T1")
    assert not r.is_error and ews.last("DeleteItem").body.get("DeleteType") == "MoveToDeletedItems"
