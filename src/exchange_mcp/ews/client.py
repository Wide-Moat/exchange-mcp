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
from dataclasses import dataclass

import httpx
from lxml import etree

from exchange_mcp.core.errors import ExchangeError
from exchange_mcp.ews.soap import envelope, find, findall, parse, q, text

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

BUSY_RETRIES = 2
BUSY_MAX_WAIT = 5.0


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
        try:
            r = await self._http.post(
                self._url,
                content=payload,
                headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml"},
                auth=self._auth,
            )
        except httpx.TimeoutException:
            log.warning("ews %s timed out ref=%s", operation, ref)
            raise ExchangeError("uncertain", ref=ref) if write else ExchangeError("busy", seconds=30) from None
        except httpx.HTTPError as e:
            log.warning("ews %s transport %s ref=%s", operation, type(e).__name__, ref)
            raise ExchangeError("busy", seconds=30) from None
        if r.status_code == 401:
            raise ExchangeError("reconnect")
        if r.status_code == 403:
            raise ExchangeError("forbidden")
        if r.status_code >= 500 and not r.content.lstrip().startswith(b"<"):
            log.warning("ews %s http %s ref=%s", operation, r.status_code, ref)
            raise ExchangeError("uncertain", ref=ref) if write else ExchangeError("busy", seconds=30)
        try:
            root = parse(r.content)
        except etree.XMLSyntaxError:
            log.warning("ews %s unparsable ref=%s", operation, ref)
            raise ExchangeError("uncertain", ref=ref) if write else ExchangeError("busy", seconds=30) from None
        fault = root.find(f"{q('s', 'Body')}/{q('s', 'Fault')}")
        if fault is not None:
            code = _fault_code(fault)
            if code == "ErrorServerBusy":
                raise _Busy(_backoff(fault))
            log.warning("ews %s fault %s ref=%s", operation, code, ref)
            raise ExchangeError("failed", code=code, ref=ref)
        body = root.find(q("s", "Body"))
        response = body[0] if body is not None and len(body) else None
        container = find(response, "m:ResponseMessages")
        if container is not None:
            messages = list(container)
            checked = messages
        else:
            # Availability and automatic-reply operations answer with one ResponseMessage beside their data.
            messages = [response] if response is not None else []
            own = find(response, "m:ResponseMessage")
            checked = [own] if own is not None else []
        for m in checked:
            if m.get("ResponseClass") == "Error":
                code = text(m, "m:ResponseCode", "ErrorUnknown")
                if code == "ErrorServerBusy":
                    raise _Busy(_backoff(m))
                log.info("ews %s %s ref=%s", operation, code, ref)
                raise _error(code, ref)
        return messages


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
    return list(find(message, container) or [])


def root_folder(message: etree._Element) -> etree._Element | None:
    return find(message, "m:RootFolder")


__all__ = ["ALLOWED", "Ews", "READS", "WRITES", "findall", "items", "root_folder"]
