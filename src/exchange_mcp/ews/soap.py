"""SOAP for EWS: namespaces, element makers, the envelope, and reading a response safely.

Responses are parsed with entity resolution, network access and DTD loading off: the XML comes from
Exchange, but a body can quote anything a sender wrote.
"""

from __future__ import annotations

from typing import Any

from lxml import etree
from lxml.builder import ElementMaker

NS_S = "http://schemas.xmlsoap.org/soap/envelope/"
NS_M = "http://schemas.microsoft.com/exchange/services/2006/messages"
NS_T = "http://schemas.microsoft.com/exchange/services/2006/types"
NSMAP = {"s": NS_S, "m": NS_M, "t": NS_T}

S = ElementMaker(namespace=NS_S, nsmap=NSMAP)
M = ElementMaker(namespace=NS_M, nsmap=NSMAP)
T = ElementMaker(namespace=NS_T, nsmap=NSMAP)

# huge_tree lifts libxml2's 10 MB limit on one text node: GetAttachment carries a whole file's base64 in one.
# The answer is already capped at MAX_RESPONSE_BYTES before parsing; entities, DTDs and network stay off.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True)


def envelope(body: etree._Element, *, version: str, timezone_id: str | None) -> bytes:
    header = S.Header(T.RequestServerVersion(Version=version))
    if timezone_id:
        header.append(T.TimeZoneContext(T.TimeZoneDefinition(Id=timezone_id)))
    return etree.tostring(S.Envelope(header, S.Body(body)), xml_declaration=True, encoding="utf-8")


def parse(content: bytes) -> etree._Element:
    return etree.fromstring(content, parser=_PARSER)


def q(prefix: str, name: str) -> str:
    return f"{{{NSMAP[prefix]}}}{name}"


def find(el: etree._Element | None, path: str) -> etree._Element | None:
    return None if el is None else el.find(path, NSMAP)


def findall(el: etree._Element | None, path: str) -> list[etree._Element]:
    return [] if el is None else list(el.findall(path, NSMAP))


def text(el: etree._Element | None, path: str, default: str = "") -> str:
    found = find(el, path)
    return (found.text or default) if found is not None else default


def flag(el: etree._Element | None, path: str) -> bool:
    return text(el, path).lower() == "true"


def operation_of(body: bytes) -> str:
    """The local name of the operation element inside the SOAP body of a request we built."""
    root = parse(body)
    b = root.find(q("s", "Body"))
    if b is None or len(b) == 0:
        raise ValueError("no operation in the SOAP body")
    return etree.QName(b[0]).localname


def item_id(value: str, change_key: str | None = None) -> etree._Element:
    attrs: dict[str, Any] = {"Id": value}
    if change_key:
        attrs["ChangeKey"] = change_key
    return T.ItemId(**attrs)
