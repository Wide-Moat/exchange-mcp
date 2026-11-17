"""The live write suite: every tool that changes something, end to end, on a real mailbox.

    PROBE_TOKEN_FILE=... uv run python tests/live/suite.py http://127.0.0.1:18765 <address the person named>

Run it only with the person's word: it sends about five messages and meeting requests to the address
given, and creates test items (subject "exchange-mcp live check ...") that it moves to Deleted Items at
the end. Every step states an expectation that can fail; the first one that does not hold stops the run
with a non-zero exit. Output names steps and shapes, never content.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from datetime import datetime, timedelta

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from probe import signed_in

TAG = f"exchange-mcp live check {datetime.now():%H%M%S}"
PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201a7c6a4b30000000049454e44ae426082"
    )
).decode()
LETTER = (
    "<html><head><style>.lead{color:#0c0c0d;font-weight:bold}</style></head><body>"
    f'<p class="lead">{TAG}</p><p>HTML body uploaded through a sealed link.</p>'
    f'<img alt="pixel" src="data:image/png;base64,{PNG}"></body></html>'
)
FILE_BYTES = b"exchange-mcp live attachment\n" * 20


class Failed(SystemExit):
    pass


def expect(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        raise Failed(1)


async def call(s: ClientSession, tool: str, /, **args):
    r = await s.call_tool(tool, args)
    text = " ".join(getattr(c, "text", "") for c in r.content)
    return r.is_error, (r.structured_content or {}), text


async def run(s: ClientSession, to: str) -> None:
    created: dict[str, list[str]] = {"events": [], "contacts": [], "tasks": [], "folders": [], "mail": [], "drafts": []}
    try:
        await steps(s, to, created)
    finally:
        await cleanup(s, created)


async def steps(s: ClientSession, to: str, created: dict) -> None:
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    while tomorrow.weekday() >= 5:
        tomorrow += timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M"
    err, conn, _ = await call(s, "get_connection")
    me = conn.get("address", "")
    expect(not err and "@" in me, "get_connection names the signed-in mailbox")

    if os.environ.get("SUITE_FROM") == "calendar":  # mail already checked: do not send it again
        await calendar_and_rest(s, to, created, tomorrow, fmt, me)
        return
    # ------------------------------------------------ HTML mail with an image, an uploaded body and a file
    err, d, text = await call(
        s, "save_draft", to=[to], subject=TAG, body=LETTER, html=True, body_upload=True, attach=["live.txt"]
    )
    expect(not err, f"save_draft with HTML, a body link and an attachment link ({text[:80] if err else ''})")
    report = d.get("html") or {}
    expect(any("style" in x for x in report.get("sanitized", [])), "the <style> rules were inlined")
    expect(any("embedded image" in x for x in report.get("sanitized", [])), "the data: image became an inline cid:")
    expect(bool(d.get("body_upload", {}).get("url")) and len(d.get("uploads", [])) == 1, "upload links came back")
    async with httpx.AsyncClient(timeout=60) as h:
        up = await h.put(d["body_upload"]["url"], content=LETTER.replace("HTML body", "Replaced HTML body").encode())
        expect(up.status_code == 200 and up.json().get("body") == "saved", "the HTML body uploaded through its link")
        up = await h.put(d["uploads"][0]["url"], content=FILE_BYTES)
        expect(up.status_code == 200 and up.json().get("size") == len(FILE_BYTES), "the file uploaded through its link")
        replay = await h.put(d["uploads"][0]["url"].replace("/files/u/", "/files/d/"), content=b"x")
        expect(replay.status_code in (403, 405), "an upload link does not open as a download")
    err, d2, _ = await call(s, "save_draft", id=d["id"])
    expect(not err and "live.txt" in d2.get("attachments", []), "the draft now carries the uploaded file")
    expect("Replaced HTML body" in d2.get("preview", ""), "the draft now carries the uploaded body")
    err, sent, _ = await call(s, "send_draft", id=d["id"], user_confirmed=True)
    expect(not err and sent.get("sent") is True, "send_draft sent the HTML letter")

    # ------------------------------------------------ read it back from Sent Items, download the file
    item = None
    for _ in range(15):
        await asyncio.sleep(2)
        err, page, _ = await call(s, "list_messages", folder="sent", limit=5)
        item = next((i for i in page.get("items", []) if TAG in i.get("subject", "")), None)
        if item:
            break
    expect(item is not None, "the letter is in Sent Items")
    created["mail"].append(item["id"])
    err, msg, _ = await call(s, "get_message", id=item["id"], include_thread=True)
    expect(not err and msg.get("body", "").startswith("<<<content "), "get_message marks the body as data")
    files = [a for a in msg.get("attachments", []) if a.get("name") == "live.txt"]
    expect(len(files) == 1 and files[0].get("url"), "the attachment comes as a download link")
    async with httpx.AsyncClient(timeout=60) as h:
        got = await h.get(files[0]["url"])
    expect(got.status_code == 200 and got.content == FILE_BYTES, "the download returns the same bytes")

    # ------------------------------------------------ organise: flag, read, categories, a folder, move and back
    for action, extra in (
        ("flag", {}),
        ("mark_unread", {}),
        ("mark_read", {}),
        ("unflag", {}),
        ("categorize", {"categories": [TAG]}),
        ("uncategorize", {"categories": [TAG]}),
    ):
        err, _, text = await call(s, "update_messages", ids=[item["id"]], action=action, **extra)
        expect(not err, f"update_messages {action} ({text[:80] if err else ''})")
        err, now, _ = await call(s, "get_message", id=item["id"], include_body=False)
        state = {
            "flag": now.get("flag") == "flagged",
            "unflag": now.get("flag") is None,
            "mark_unread": now.get("unread") is True,
            "mark_read": not now.get("unread"),
            "categorize": TAG in now.get("categories", []),
            "uncategorize": TAG not in now.get("categories", []),
        }[action]
        expect(not err and state, f"get_message shows the message after {action}")
    err, f, _ = await call(s, "save_mail_folder", folder_name=TAG)
    expect(not err and f.get("id"), "save_mail_folder created a folder")
    created["folders"].append(TAG)
    err, f2, _ = await call(s, "save_mail_folder", folder=TAG, folder_name=TAG + " renamed")
    expect(not err and "renamed" in f2.get("changed", []), "save_mail_folder renamed it")
    created["folders"] = [TAG + " renamed"]
    err, mv, _ = await call(s, "update_messages", ids=[item["id"]], action="move", folder=TAG + " renamed")
    expect(not err and len(mv.get("ids", [])) == 1, "update_messages moved the letter and returned its new id")
    created["mail"] = list(mv["ids"])
    err, back, _ = await call(s, "update_messages", ids=mv["ids"], action="move", folder="sent")
    expect(not err and len(back.get("ids", [])) == 1, "and moved it back")
    sent_id = back["ids"][0]
    created["mail"] = [sent_id]

    # ------------------------------------------------ forward and reply-all keep the thread
    err, fw, text = await call(s, "save_draft", mode="forward", message=sent_id, to=[to], body=f"Forward: {TAG}")
    expect(
        not err and fw.get("subject", "").lower().startswith(("fw", "tr", "wg", "пере")),
        f"save_draft forward ({text[:80] if err else ''})",
    )
    created["drafts"].append(fw["id"])
    err, _, _ = await call(s, "send_draft", id=fw["id"], user_confirmed=True)
    expect(not err, "send_draft sent the forward")
    created["drafts"].remove(fw["id"])
    err, ra, text = await call(s, "save_draft", mode="reply_all", message=sent_id, body=f"Reply all: {TAG}")
    expect(
        not err and ra.get("subject", "").lower().startswith(("re", "aw", "отв")),
        f"save_draft reply_all ({text[:80] if err else ''})",
    )
    err, _, _ = await call(s, "update_messages", ids=[ra["id"]], action="delete")
    expect(not err, "an unsent reply draft goes to Deleted Items")

    await calendar_and_rest(s, to, created, tomorrow, fmt, me)


async def calendar_and_rest(s: ClientSession, to: str, created: dict, tomorrow: datetime, fmt: str, me: str) -> None:
    # ------------------------------------------------ calendar: free/busy, slots, a meeting through its life
    day0, day1 = tomorrow.replace(hour=9).strftime(fmt), tomorrow.replace(hour=18).strftime(fmt)
    err, busy, text = await call(s, "find_meeting_times", people=[me], start=day0, end=day1)
    expect(
        not err and len(busy.get("people", [])) == 1,
        f"find_meeting_times shows busy times ({text[:80] if err else ''})",
    )
    err, slots, text = await call(s, "find_meeting_times", people=[me], start=day0, end=day1, duration=30)
    expect(
        not err and isinstance(slots.get("slots"), list),
        f"find_meeting_times suggests slots ({text[:80] if err else ''})",
    )
    err, solo, text = await call(s, "save_event", subject=TAG + " solo", start=tomorrow.strftime(fmt), duration=30)
    expect(not err and solo.get("id"), f"save_event without attendees ({text[:80] if err else ''})")
    created["events"].append(solo["id"])
    err, rep, text = await call(
        s,
        "save_event",
        subject=TAG + " weekly",
        start=tomorrow.strftime(fmt),
        duration=15,
        repeat="weekly",
        repeat_count=3,
    )
    expect(not err and rep.get("id"), f"save_event with a weekly repeat ({text[:80] if err else ''})")
    created["events"].append(rep["id"])
    err, rev, _ = await call(s, "get_event", id=rep["id"], include_body=False)
    words = str(rev.get("repeats", "")).lower()
    expect(not err and "week" in words and "3" in words, "get_event reads back a weekly repeat of three")
    err, refused, refused_text = await call(
        s,
        "save_event",
        subject=TAG + " meeting",
        start=tomorrow.replace(hour=15).strftime(fmt),
        duration=30,
        attendees=[to],
    )
    expect(err and "confirm" in refused_text, "an invitation without the person's word is refused for that reason")
    err, mt, text = await call(
        s,
        "save_event",
        subject=TAG + " meeting",
        start=tomorrow.replace(hour=15).strftime(fmt),
        duration=30,
        attendees=[to],
        body="Live check of invitations.",
        user_confirmed=True,
    )
    expect(not err and mt.get("id"), f"save_event sent an invitation ({text[:80] if err else ''})")
    created["events"].append(mt["id"])
    err, ev, _ = await call(s, "get_event", id=mt["id"])
    expect(not err and len(ev.get("attendees", [])) == 1, "get_event shows the attendee")
    err, moved, text = await call(
        s, "save_event", id=mt["id"], start=tomorrow.replace(hour=16).strftime(fmt), user_confirmed=True
    )
    expect(not err, f"save_event moved the meeting and sent the update ({text[:80] if err else ''})")
    err, cx, text = await call(s, "cancel_event", id=mt["id"], comment="Live check: cancelled.", user_confirmed=True)
    expect(
        not err and cx.get("cancelled"), f"cancel_event cancelled it and told the attendee ({text[:80] if err else ''})"
    )
    created["events"].remove(mt["id"])
    err, own, own_text = await call(s, "respond_to_event", id=solo["id"], response="accept")
    expect(err and "You organise this meeting" in own_text, "the organiser cannot answer their own event")

    # ------------------------------------------------ automatic reply: scheduled tomorrow night, then off
    # Only when none is set: turning it off keeps the saved texts, so a reply the person has would change.
    err, conn1, _ = await call(s, "get_connection")
    # Opt-in: even when off, the saved reply texts and audience would be replaced by the test's.
    if conn1.get("automatic_reply") != "disabled" or os.environ.get("SUITE_AUTOMATIC_REPLY") != "1":
        print("skip  automatic reply (set SUITE_AUTOMATIC_REPLY=1; it replaces the saved reply texts)", flush=True)
        await contacts_and_tasks(s, created, tomorrow)
        return
    night0 = tomorrow.replace(hour=23, minute=0).strftime(fmt)
    night1 = (tomorrow + timedelta(days=1)).replace(hour=1, minute=0).strftime(fmt)
    try:
        err, oof, text = await call(
            s,
            "save_automatic_reply",
            state="scheduled",
            internal_text=TAG,
            start=night0,
            end=night1,
            user_confirmed=True,
        )
        expect(not err, f"save_automatic_reply scheduled ({text[:80] if err else ''})")
        err, conn2, _ = await call(s, "get_connection")
        expect(conn2.get("automatic_reply") == "scheduled", "get_connection shows it scheduled")
    finally:
        err, _, _ = await call(s, "save_automatic_reply", state="off")  # whatever happened above
    err2, conn3, _ = await call(s, "get_connection")
    expect(not err and conn3.get("automatic_reply") == "disabled", "and it is off again")

    await contacts_and_tasks(s, created, tomorrow)


async def contacts_and_tasks(s: ClientSession, created: dict, tomorrow: datetime) -> None:
    # ------------------------------------------------ contacts and tasks
    err, c, text = await call(
        s, "save_contact", name=TAG, emails=["live.check@example.com"], phones=["+10000000000"], company="Example"
    )
    expect(not err and c.get("id"), f"save_contact created a contact ({text[:80] if err else ''})")
    created["contacts"].append(c["id"])
    err, found, _ = await call(s, "list_people", query="live.check@example.com", source="contacts")
    expect(not err and any(p.get("id") == c["id"] for p in found.get("items", [])), "list_people finds it by address")
    err, t, text = await call(s, "save_task", subject=TAG, due=tomorrow.strftime("%Y-%m-%d"))
    expect(not err and t.get("id"), f"save_task created a task ({text[:80] if err else ''})")
    created["tasks"].append(t["id"])
    err, _, _ = await call(s, "save_task", id=t["id"], status="completed")
    err2, done, _ = await call(s, "list_tasks", status="completed", query=TAG)
    expect(not err and any(x.get("id") == t["id"] for x in done.get("items", [])), "the task is completed")


async def cleanup(s: ClientSession, created: dict) -> None:
    """Every test item goes to Deleted Items; any that cannot is a failure of the run."""
    left = []
    steps = (
        [("update_messages", {"ids": created.get("drafts", []), "action": "delete"})] if created.get("drafts") else []
    )
    steps += [("cancel_event", {"id": e, "user_confirmed": True}) for e in created["events"]]
    steps += [("delete_contact", {"id": c}) for c in created["contacts"]]
    steps += [("delete_task", {"id": t}) for t in created["tasks"]]
    if created["mail"]:
        steps.append(("update_messages", {"ids": created["mail"], "action": "delete"}))
    steps += [("delete_mail_folder", {"folder": f, "user_confirmed": True}) for f in created["folders"]]
    for name, args in steps:
        try:
            err, _, text = await call(s, name, **args)
        except Exception as e:  # noqa: BLE001 - one failed step must not leave the rest behind
            err, text = True, type(e).__name__
        print(("ok    " if not err else "LEFT  ") + f"cleanup: {name} {text[:60] if err else ''}", flush=True)
        if err:
            left.append(name)
    try:  # every test letter in Sent Items, including the forward, whose id changes when sent
        err, page, _ = await call(s, "list_messages", folder="sent", limit=25)
        stray = [i["id"] for i in page.get("items", []) if TAG in i.get("subject", "")]
        if stray:
            err, _, _ = await call(s, "update_messages", ids=stray, action="delete")
            print(("ok    " if not err else "LEFT  ") + f"cleanup: {len(stray)} test letters", flush=True)
            if err:
                left.append("letters")
    except Exception:  # noqa: BLE001
        left.append("letters")
    if left:
        print("FAIL  cleanup left items behind:", ", ".join(left), flush=True)
        raise Failed(1)


async def main(base: str, to: str) -> None:
    tok = await signed_in(base)
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=120)
    async with streamable_http_client(f"{base}/mcp", http_client=client) as st, ClientSession(st[0], st[1]) as s:
        await s.initialize()
        await run(s, to)
    print("all expectations held")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1].rstrip("/"), sys.argv[2]))
