"""The contract every tool keeps: names, descriptions, annotations, flat arguments, refusals before EWS."""

from __future__ import annotations

import inspect
import re

from exchange_mcp.registry import ALLOWED_VERBS, AREAS, all_tools
from exchange_mcp.server import ExchangeServer


async def test_confirmation_must_be_the_boolean_true(rt, ews, invoke):
    for value in ("yes", 1, "true", None):
        r = await invoke("send_draft", id="D1", user_confirmed=value)
        assert r.is_error and ("boolean true" in r.text or "confirm" in r.text), value
    assert ews.ops() == []


async def test_an_unknown_argument_is_refused_before_upstream(rt, ews, invoke):
    r = await invoke("list_messages", mailbox="ceo@corp.example")
    assert r.is_error and "has no argument mailbox" in r.text
    assert ews.ops() == []


async def test_an_off_area_is_not_published_and_cannot_be_called(rt, ews):
    from mcp.types import CallToolRequestParams

    s = ExchangeServer(frozenset({"account"}))
    names = {t.name for t in await s.list_tools()}
    assert names == {"get_connection"}
    r = await s._handle_call_tool(None, CallToolRequestParams(name="send_draft", arguments={"id": "x"}))
    assert r.is_error and ews.ops() == []


async def test_an_id_given_as_a_number_is_refused(rt, ews, invoke):
    r = await invoke("get_message", id=12345678901234567890)
    assert r.is_error and "is an id string, exactly as a tool returned it" in r.text
    r = await invoke("update_messages", ids=[1, 2], action="mark_read")
    assert r.is_error and ews.ops() == []


def test_names_use_allowed_verbs_and_no_prefix():
    for name in all_tools():
        assert name.startswith(ALLOWED_VERBS), name
        assert not re.match(r"^(exchange|ews|mcp)_", name), name


def test_descriptions_read_like_a_man_page():
    for name, spec in all_tools().items():
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", spec.description) if s.strip()]
        assert len(sentences) >= 3, name
        assert name.replace("_", " ") not in spec.description.lower()[:40] or len(sentences) >= 3
        if spec.irreversible:
            assert "cannot be undone" in spec.description.lower(), name


def test_annotations_follow_behaviour():
    for name, spec in all_tools().items():
        if spec.read_only:
            assert not spec.notifies_others and not spec.irreversible, name
        if spec.irreversible:
            assert not spec.idempotent, name
        src = inspect.getsource(spec.fn)
        if spec.notifies_others:
            assert ".guard(" in src or "guard.check" in src or "guard.spend(" in src, (
                f"{name} notifies others without the guard"
            )
        if spec.read_only:
            assert not re.search(r"em\.(send|create|update|move|set|add|delete|rename|mark)", src), name


def test_every_tool_belongs_to_a_known_area():
    for spec in all_tools().values():
        assert spec.area in AREAS


async def test_arguments_are_flat_and_described():
    s = ExchangeServer(frozenset(AREAS))
    obvious = {
        "limit",
        "cursor",
        "id",
        "ids",
        "query",
        "subject",
        "cc",
        "bcc",
        "state",
        "action",
        "kind",
        "unread_only",
        "has_attachments",
        "include_body",
        "importance",
        "comment",
        "response",
        "status",
        "start",
        "end",
        "name",
        "body",
        "company",
        "job_title",
        "notes",
        "location",
        "private",
        "show_as",
        "source",
        "emails",
        "phones",
        "due",
        "due_before",
        "reminder",
        "duration",
        "attendees",
        "rooms",
        "repeat",
        "repeat_until",
        "repeat_count",
        "reminder_minutes",
        "send_response",
        "working_hours_only",
        "people",
        "folder_name",
    }
    for t in await s.list_tools():
        for arg, schema in (t.input_schema.get("properties") or {}).items():
            flat = schema.get("type") in ("string", "integer", "boolean", "array", None)
            assert flat and "$ref" not in str(schema) and "properties" not in schema, f"{t.name}.{arg}"
            if arg not in obvious:
                assert schema.get("description"), f"{t.name}.{arg} has no description"


async def test_lists_are_capped():
    for name, spec in all_tools().items():
        if spec.returns_list:
            params = inspect.signature(spec.fn).parameters
            assert "limit" in params and "cursor" in params and "offset" not in params, name


def test_no_mailbox_argument_anywhere():
    for name, spec in all_tools().items():
        params = set(inspect.signature(spec.fn).parameters)
        assert not params & {"mailbox", "user", "server", "tenant", "impersonate", "as_user", "assignee"}, name
