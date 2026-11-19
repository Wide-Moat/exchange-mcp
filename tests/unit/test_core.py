"""The bucket, cursors and limits."""

from __future__ import annotations

import pytest
from conftest import memory_store

from exchange_mcp.core.cursor import Cursors
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.core.guard import Guard
from exchange_mcp.core.project import clamp_limit


async def test_the_bucket_refuses_the_eleventh_action(sealer):
    t = [0.0]
    g = Guard(memory_store(sealer, clock=lambda: t[0]))
    for i in range(10):
        assert (await g.check("p", kind="mail", user_confirmed=True))["sends_left"] == 9 - i
    with pytest.raises(ExchangeError) as e:
        await g.check("p", kind="mail", user_confirmed=True)
    assert e.value.key == "rate" and "draft is kept" in e.value.text and "Wait 6 seconds" in e.value.text
    await g.check("q", kind="mail", user_confirmed=True)  # another person has their own bucket
    t[0] += 6.1
    await g.check("p", kind="mail", user_confirmed=True)  # one refilled


async def test_the_guard_wants_the_word_before_the_count(sealer):
    g = Guard(memory_store(sealer))
    with pytest.raises(ExchangeError) as e:
        await g.check("p", kind="mail", user_confirmed=False, recipients=50)
    assert e.value.key == "confirm"
    with pytest.raises(ExchangeError) as e:
        await g.check("p", kind="mail", user_confirmed=True, recipients=21)
    assert e.value.key == "too_many"


def test_a_limit_below_one_is_refused():
    with pytest.raises(ExchangeError):
        clamp_limit(0)
    assert clamp_limit(None) == (10, {})
    assert clamp_limit(99)[0] == 25


async def test_a_cursor_from_another_tool_or_person_is_refused(sealer):
    c = Cursors(sealer)
    cur = await c.make("list_messages", "p1", {"o": 10}, {"f": "inbox"})
    assert await c.read("list_messages", "p1", cur, {"f": "inbox"}) == {"o": 10}
    for tool, person, scope in (
        ("list_events", "p1", {"f": "inbox"}),
        ("list_messages", "p2", {"f": "inbox"}),
        ("list_messages", "p1", {"f": "sent"}),
    ):
        with pytest.raises(ExchangeError):
            await c.read(tool, person, cur, scope)
    with pytest.raises(ExchangeError):
        await c.read("list_messages", "p1", cur[:-4] + "AAAA", {"f": "inbox"})
