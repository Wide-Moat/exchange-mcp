"""HTTP routes behind the sealed links: stream an attachment out of Exchange, receive a file or an HTML
body for a draft this server made."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from exchange_mcp.auth.credential import Credential, person_key
from exchange_mcp.core.context import runtime
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.core.html import prepare_outgoing
from exchange_mcp.ews import mail as em
from exchange_mcp.ews.client import Ews
from exchange_mcp.links.links import MAX_ATTACHMENT_BYTES, MAX_BODY_BYTES, safe_name

log = logging.getLogger("exchange_mcp.links")
_UPLOADS = asyncio.Semaphore(3)  # at most this many uploads held in memory at once, across all people
UPLOAD_SECONDS = 120


def _deny() -> Response:
    return JSONResponse({"error": "This link has expired or is not valid. Ask the assistant for a new one."}, 403)


def _ews(cred: Credential) -> Ews:
    rt = runtime()
    return Ews(rt.http, rt.settings.ews_url, cred.auth())


def _cred(link: dict) -> Credential | None:
    cred = Credential.from_payload(link.get("c") or {})
    if cred is None or person_key(cred.login) != link.get("p"):
        return None
    return cred


async def download(request: Request) -> Response:
    rt = runtime()
    link = await rt.links.opened_download(request.path_params["lid"])
    cred = _cred(link) if link else None
    if link is None or cred is None:
        return _deny()
    if isinstance(link.get("s"), int) and link["s"] > MAX_ATTACHMENT_BYTES:
        return JSONResponse({"error": "The attachment is larger than this connector passes through."}, 413)
    try:
        name, ctype, data = await em.attachment_bytes(_ews(cred), link["a"])
    except ExchangeError as e:
        log.info("download refused: %s", e.key)
        return JSONResponse({"error": "Exchange did not return the attachment. Ask for a new link."}, 502)
    if len(data) > MAX_ATTACHMENT_BYTES * 2:
        return JSONResponse({"error": "The attachment is too large to pass through."}, 413)
    safe = safe_name(link.get("n") or name)
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in safe)
    return Response(
        data,
        media_type=ctype if "/" in ctype else "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def upload(request: Request) -> Response:
    rt = runtime()
    link = await rt.links.claim_upload(request.path_params["lid"])
    cred = _cred(link) if link else None
    if link is None or cred is None:
        return _deny()
    try:
        limit = MAX_BODY_BYTES if link["o"] == "body" else MAX_ATTACHMENT_BYTES
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return JSONResponse({"error": f"The file is larger than {limit // 2**20} MB."}, 413)
        if _UPLOADS.locked():
            return JSONResponse({"error": "The server is receiving other files. Try again in a minute."}, 503)
        async with _UPLOADS:
            data = bytearray()
            try:
                async with asyncio.timeout(UPLOAD_SECONDS):  # a stalled client cannot hold a slot
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > limit:
                            return JSONResponse({"error": f"The file is larger than {limit // 2**20} MB."}, 413)
            except TimeoutError:
                return JSONResponse({"error": "The upload took too long. Nothing was saved; try again."}, 408)
            if not data:
                return JSONResponse({"error": "The upload was empty."}, 400)
            return await _store(link, cred, bytes(data))
    finally:
        rt.links.release_upload(link["u"])


async def _store(link: dict, cred: Credential, data: bytes) -> Response:
    ews = _ews(cred)
    try:
        draft = await em.get_message(ews, link["d"], body=False)
        if not draft.ours or not draft.is_draft:
            return _deny()
        if link["o"] == "attach":
            name = safe_name(link["n"])
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            await em.add_attachment(ews, draft.id, name, ctype, data)
            return JSONResponse({"attached": name, "size": len(data), "next": "Show the draft again with save_draft."})
        prepared = prepare_outgoing(data)
        if prepared.errors:
            return JSONResponse({"error": "The HTML was not saved.", "problems": prepared.errors}, 400)
        await em.update_draft(
            ews,
            draft.id,
            draft.change_key,
            subject=None,
            html=prepared.html,
            to=None,
            cc=None,
            bcc=None,
            importance=None,
        )
        for img in prepared.inline_images:
            await em.add_attachment(
                ews,
                draft.id,
                f"{img.content_id}.{img.content_type.split('/')[-1]}",
                img.content_type,
                img.data,
                content_id=img.content_id,
            )
        return JSONResponse({"body": "saved", **prepared.report()})
    except ExchangeError as e:
        log.info("upload refused: %s", e.key)
        return JSONResponse(
            {
                "error": "Exchange did not accept all of it. Part may be saved: show the draft with save_draft "
                "before trying again."
            },
            502,
        )
