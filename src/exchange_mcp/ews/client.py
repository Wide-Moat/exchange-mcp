"""The only code that speaks to Exchange.

It sends an operation only if it is on the allowlist, to the one configured EWS address, with the
server version and the person's time zone, and turns every failure into an `ExchangeError`: EWS reports
most errors inside an HTTP 200, per response message.

The allowlist is the boundary manifesto rule 28 describes. An operation that is not here cannot be
reached by any tool, any bug or any injected instruction: rules, forwarding, delegates, permissions,
export, synchronisation, subscriptions and impersonation are absent on purpose.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from lxml import etree

from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.ews.soap import envelope, find, findall, parse, q, text
from exchange_mcp.ews.transport import Ntlm

log = logging.getLogger("exchange_mcp.ews")

SERVER_VERSION = "Exchange2016"

READS = frozenset(
    {
        "ResolveNames",
        "FindPeople",
        "GetRoomLists",
        "GetRooms",
        "FindItem",
        "FindFolder",
        "GetItem",
        "GetFolder",
        "GetAttachment",
        "GetConversationItems",
        "GetUserAvailability",
        "GetUserOofSettings",
        "GetMailTips",
        "GetUserConfiguration",
    }
)
WRITES = frozenset(
    {
        "CreateItem",
        "UpdateItem",
        "MoveItem",
        "DeleteItem",
        "SendItem",
        "CreateFolder",
        "UpdateFolder",
        "MoveFolder",
        "DeleteFolder",
        "CreateAttachment",
        "SetUserOofSettings",
    }
)
ALLOWED = READS | WRITES

# Set by the app: makes a one-connection client with the same TLS settings, for NTLM calls.
isolated: Callable[[], httpx.AsyncClient] | None = None

BUSY_RETRIES = 2
BUSY_MAX_WAIT = 5.0
# An attachment travels base64 inside the SOAP answer: 25 MB of file is about 34 MB of answer.
MAX_RESPONSE_BYTES = 40 * 1024 * 1024


def _children(el):
    return [] if el is None else list(el)


@dataclass
class Sent:
    """What one request carried, for the tests that inspect every request (never logged)."""

    operation: str
    body: bytes


class Ews:
    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        auth: httpx.Auth,
        *,
        timezone_id: str | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self._http = http
        self._url = url
        self._auth = auth
        self._tz = timezone_id
        self._sleep = sleep

    async def call(self, body: etree._Element) -> list[etree._Element]:
        """Send one operation; return its response messages, each with ResponseClass Success or Warning.

        A message whose class is Error raises, so a batch either succeeds whole or says what failed.
        """
        operation = operation_name(body)
        if operation not in ALLOWED:
            raise RuntimeError(f"EWS operation {operation} is not on the allowlist")
        payload = envelope(body, version=SERVER_VERSION, timezone_id=self._tz)
        for attempt in range(BUSY_RETRIES + 1):
            try:
                messages = await self._once(operation, payload)
            except _Busy as busy:
                if attempt == BUSY_RETRIES or busy.wait > BUSY_MAX_WAIT:
                    raise ExchangeError("busy", seconds=max(1, round(busy.wait))) from None
                await self._sleep(busy.wait)
                continue
            return messages
        raise AssertionError("unreachable")

    async def _once(self, operation: str, payload: bytes) -> list[etree._Element]:
        ref = secrets.token_hex(4)
        write = operation in WRITES

        def unknown_outcome() -> ExchangeError:
            # A write that may have reached Exchange is never reported as "nothing was done".
            return ExchangeError("uncertain", ref=ref) if write else ExchangeError("busy", seconds=30)

        try:
            content, status = await self._post(payload)
        except _TooLarge:
            log.warning("ews %s response over %s bytes ref=%s", operation, MAX_RESPONSE_BYTES, ref)
            if write:
                raise unknown_outcome() from None
            raise ExchangeError("too_large", size="the answer", limit=f"{MAX_RESPONSE_BYTES // 2**20} MB") from None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            log.warning("ews %s could not connect ref=%s", operation, ref)
            raise ExchangeError("busy", seconds=30) from None  # never left this machine
        except httpx.HTTPError as e:
            log.warning("ews %s transport %s ref=%s", operation, type(e).__name__, ref)
            raise unknown_outcome() from None
        if status == 401:
            raise ExchangeError("reconnect")
        if status == 403:
            raise ExchangeError("forbidden")
        if status >= 500 and not content.lstrip().startswith(b"<"):
            log.warning("ews %s http %s ref=%s", operation, status, ref)
            raise unknown_outcome()
        try:
            root = parse(content)
        except etree.XMLSyntaxError:
            log.warning("ews %s unparsable ref=%s", operation, ref)
            raise unknown_outcome() from None
        fault = root.find(f"{q('s', 'Body')}/{q('s', 'Fault')}")
        if fault is not None:
            code = _fault_code(fault)
            if code == "ErrorServerBusy":
                raise _Busy(_backoff(fault))
            log.warning("ews %s fault %s ref=%s", operation, code, ref)
            raise ExchangeError("failed", code=code, ref=ref)
        body = root.find(q("s", "Body")) if etree.QName(root).localname == "Envelope" else None
        response = body[0] if body is not None and len(body) else None
        if response is None or etree.QName(response).localname != f"{operation}Response":
            # A proxy's error page or anything else that parses as XML is not an answer from EWS.
            log.warning("ews %s unexpected answer ref=%s", operation, ref)
            raise unknown_outcome()
        container = find(response, "m:ResponseMessages")
        if container is not None:
            messages = list(container)
            checked = messages
            if not messages:
                raise unknown_outcome()
        else:
            # Availability and automatic-reply operations answer with one ResponseMessage beside their data.
            messages = [response]
            own = find(response, "m:ResponseMessage")
            checked = [own] if own is not None else []
        errors = [m for m in checked if m.get("ResponseClass") == "Error"]
        if not errors:
            return messages
        busy = [m for m in errors if text(m, "m:ResponseCode") == "ErrorServerBusy"]
        if busy and len(busy) == len(checked):
            raise _Busy(_backoff(busy[0]))  # nothing in the batch happened: safe to send again
        if write and len(errors) < len(checked):
            # Part of a batch was applied. Sending it again would repeat what succeeded; saying it failed
            # would hide it. Report it as partly done.
            log.info("ews %s partial %s/%s ref=%s", operation, len(checked) - len(errors), len(checked), ref)
            raise ExchangeError("partial", done=len(checked) - len(errors), total=len(checked), ref=ref)
        code = text(errors[0], "m:ResponseCode", "ErrorUnknown")
        log.info("ews %s %s ref=%s", operation, code, ref)
        raise _error(code, ref)

    async def _post(self, payload: bytes) -> tuple[bytes, int]:
        """The answer, read up to MAX_RESPONSE_BYTES: an attachment or a body cannot make this process
        allocate without bound.

        NTLM authenticates a connection, and its three legs must travel on one. The shared pool may hand a
        later leg another connection when calls run at once, so an NTLM call gets a client of its own with
        a single connection, closed when the call ends."""
        if isolated is not None and isinstance(self._auth, Ntlm):
            async with isolated() as own:
                return await self._read(own, payload)
        return await self._read(self._http, payload)

    async def _read(self, http: httpx.AsyncClient, payload: bytes) -> tuple[bytes, int]:
        async with http.stream(
            "POST",
            self._url,
            content=payload,
            headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml"},
            auth=self._auth,
        ) as r:
            declared = r.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                raise _TooLarge
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise _TooLarge
            return bytes(buf), r.status_code


class _TooLarge(Exception):
    pass


def operation_name(body: etree._Element) -> str:
    """The EWS operation an element asks for; availability and automatic-reply elements end in Request."""
    name = etree.QName(body).localname
    return name.removesuffix("Request") if name.endswith("Request") else name


class _Busy(Exception):
    def __init__(self, wait: float) -> None:
        self.wait = wait


def _backoff(el: etree._Element) -> float:
    """EWS says how long to wait as <t:Value Name="BackOffMilliseconds">n</t:Value> inside MessageXml."""
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        named = etree.QName(node).localname == "Value" and node.get("Name") == "BackOffMilliseconds"
        if (named or etree.QName(node).localname == "BackOffMilliseconds") and (node.text or "").strip().isdigit():
            return int(node.text.strip()) / 1000
    return 1.0


def _fault_code(fault: etree._Element) -> str:
    for node in fault.iter():
        if etree.QName(node).localname == "ResponseCode" and node.text:
            return node.text
    return "SoapFault"


NOT_FOUND = frozenset(
    {"ErrorItemNotFound", "ErrorFolderNotFound", "ErrorInvalidIdMalformed", "ErrorInvalidIdNotAnItemAttachmentId"}
)
FORBIDDEN = frozenset({"ErrorAccessDenied", "ErrorInsufficientResources", "ErrorQuotaExceeded"})
RECONNECT = frozenset({"ErrorInvalidRequest401", "ErrorAccountDisabled", "ErrorExpiredSubscription"})


def _error(code: str, ref: str) -> ExchangeError:
    if code in NOT_FOUND:
        return ExchangeError("not_found")
    if code in FORBIDDEN:
        return ExchangeError("forbidden")
    if code in RECONNECT:
        return ExchangeError("reconnect")
    return ExchangeError("failed", code=code, ref=ref)


def items(message: etree._Element, container: str = "m:Items") -> list[etree._Element]:
    return _children(find(message, container))


def root_folder(message: etree._Element) -> etree._Element | None:
    return find(message, "m:RootFolder")


__all__ = ["ALLOWED", "Ews", "READS", "WRITES", "findall", "items", "root_folder"]
