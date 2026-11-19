"""HTTP routes behind the sealed links: stream an attachment out of Exchange, receive a file or an HTML
body for a draft this server made."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from exchange_mcp import metrics
from exchange_mcp.auth.credential import Credential, person_key
from exchange_mcp.core.context import runtime
from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.core.html import prepare_outgoing
from exchange_mcp.ews import mail as em
from exchange_mcp.ews.client import Ews
from exchange_mcp.links.links import MAX_ATTACHMENT_BYTES, MAX_BODY_BYTES, safe_name
from exchange_mcp.state.store import StoreFull, StoreUnavailable

log = logging.getLogger("exchange_mcp.links")
_UPLOADS = asyncio.Semaphore(3)  # at most this many uploads held in memory at once, across all people
# A download is held whole (EWS returns it base64 inside SOAP): at most this many at once, so 25 MB each
# stays far below the container's memory limit.
_DOWNLOADS = asyncio.Semaphore(4)
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
    response = await _download(request)
    metrics.link("download", _result(response))
    return response


async def _download(request: Request) -> Response:
    rt = runtime()
    link = await rt.links.opened_download(request.path_params["lid"])
    cred = _cred(link) if link else None
    if link is None or cred is None:
        return _deny()
    if isinstance(link.get("s"), int) and link["s"] > MAX_ATTACHMENT_BYTES:
        return JSONResponse({"error": "The attachment is larger than this connector passes through."}, 413)
    if _DOWNLOADS.locked():
        return JSONResponse({"error": "The server is sending other files. Try again in a minute."}, 503)
    try:
        async with _DOWNLOADS:
            name, ctype, data = await em.attachment_bytes(_ews(cred), link["a"])
    except ExchangeError as e:
        log.info("download refused: %s", e.key)
        return JSONResponse({"error": "Exchange did not return the attachment. Ask for a new link."}, 502)
    if len(data) > MAX_ATTACHMENT_BYTES:
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


def _result(response: Response) -> str:
    return {200: "ok", 403: "denied", 413: "too_large", 503: "busy", 408: "timeout", 400: "invalid"}.get(
        response.status_code, "upstream_error"
    )


async def upload(request: Request) -> Response:
    response = await _upload(request)
    metrics.link("upload", _result(response))
    return response


async def _upload(request: Request) -> Response:
    rt = runtime()
    try:
        link = await rt.links.claim_upload(request.path_params["lid"])
    except (StoreUnavailable, StoreFull):
        log.info("upload refused: state store unavailable")
        return JSONResponse(
            {"error": "The server cannot take files right now. Nothing was saved; try again shortly."}, 503
        )
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
        try:
            await rt.links.release_upload(link["u"])
        except StoreUnavailable:
            log.info("upload mark not released: state store unavailable")  # it expires with the link


async def _store(link: dict, cred: Credential, data: bytes) -> Response:
    ews = _ews(cred)
    try:
        draft = await em.get_message(ews, link["d"], body=False)
        if not draft.ours or not draft.is_draft:
            return _deny()
        if link["o"] == "attach":
            name = safe_name(link["n"])
            # One link, one file: uploading again (a retried curl, a second run) must not attach it twice.
            # The draft is the record, so this holds across replicas: a file of this name is already there.
            if any(a.name == name and not a.inline for a in draft.attachments):
                return JSONResponse(
                    {
                        "attached": name,
                        "already": True,
                        "next": "A file of this name is already on the draft; nothing was added. If this is a "
                        "different file, rename it and ask save_draft for a new link.",
                    }
                )
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
