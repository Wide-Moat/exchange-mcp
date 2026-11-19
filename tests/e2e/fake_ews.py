"""A stand-in Exchange for the chart's end-to-end run (scripts/chart-e2e.sh): HTTPS with a certificate from
a CA made for the run, NTLM checked by a real acceptor (pyspnego) against a user file, channel binding
required as IIS Extended Protection requires it, and the operations two flows send:

- a password sign-in: `ResolveNames`;
- `save_draft` of a new message: `ResolveNames`, `GetUserConfiguration` (OWA.UserOptions, with a signature
  that OWA adds to new messages), `CreateItem`, `GetItem` (the body that was written, read back) and
  `GetMailTips`.

It runs in the server's own image, which already has everything it needs.

    NTLM_USER_FILE=/fake/users python /fake/fake_ews.py /tls/tls.crt /tls/tls.key

It logs one JSON line per sign-in check with the outcome only, and one per operation with its name only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from html import escape

import spnego
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from lxml import etree
from spnego.channel_bindings import GssChannelBindings

RESOLVED = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    b'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    b'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><s:Body><m:ResolveNamesResponse>'
    b'<m:ResponseMessages><m:ResolveNamesResponseMessage ResponseClass="Success"><m:ResponseCode>NoError'
    b'</m:ResponseCode><m:ResolutionSet TotalItemsInView="1" IncludesLastItemInRange="true"><t:Resolution>'
    b"<t:Mailbox><t:Name>Alex Example</t:Name><t:EmailAddress>alex@corp.example</t:EmailAddress>"
    b"<t:RoutingType>SMTP</t:RoutingType><t:MailboxType>Mailbox</t:MailboxType></t:Mailbox>"
    b"<t:Contact><t:DisplayName>Alex Example</t:DisplayName><t:Alias>alex</t:Alias></t:Contact>"
    b"</t:Resolution></m:ResolutionSet></m:ResolveNamesResponseMessage></m:ResponseMessages>"
    b"</m:ResolveNamesResponse></s:Body></s:Envelope>"
)


NS = (
    'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
    'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"'
)
T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
# What the e2e run looks for in the draft the server wrote: proof the signature came from GetUserConfiguration.
SIGNATURE_TEXT = "Alex Example | e2e signature from OWA"
DRAFT_ID = "E2E-DRAFT-1"
PROPERTY_SET = "6e1f8a43-3c0b-4f5e-9d61-2b7a0c5e9f12"


def _soap(operation: str, inner: str) -> bytes:
    msg = (
        f'<m:{operation}ResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
        f"{inner}</m:{operation}ResponseMessage>"
    )
    return (
        f'<?xml version="1.0" encoding="utf-8"?><s:Envelope {NS}><s:Body><m:{operation}Response>'
        f"<m:ResponseMessages>{msg}</m:ResponseMessages></m:{operation}Response></s:Body></s:Envelope>"
    ).encode()


def _entry(key: str, kind: str, value: str) -> str:
    return (
        f"<t:DictionaryEntry><t:DictionaryKey><t:Type>String</t:Type><t:Value>{key}</t:Value></t:DictionaryKey>"
        f"<t:DictionaryValue><t:Type>{kind}</t:Type><t:Value>{escape(value)}</t:Value></t:DictionaryValue>"
        "</t:DictionaryEntry>"
    )


OWA_OPTIONS = _soap(
    "GetUserConfiguration",
    '<m:UserConfiguration><t:UserConfigurationName Name="OWA.UserOptions"><t:DistinguishedFolderId Id="root"/>'
    "</t:UserConfigurationName><t:Dictionary>"
    + _entry("signaturehtml", "String", f"<div>{SIGNATURE_TEXT}</div>")
    + _entry("signaturetext", "String", SIGNATURE_TEXT)
    + _entry("autoaddsignature", "Boolean", "true")
    + _entry("autoaddsignatureonreply", "Boolean", "true")
    + "</t:Dictionary></m:UserConfiguration>",
)
MAIL_TIPS = _soap(
    "GetMailTips",
    "<m:MailTips><t:RecipientAddress><t:EmailAddress>alex@corp.example</t:EmailAddress></t:RecipientAddress>"
    "</m:MailTips>",
)


def _property(name: str) -> str:
    return (
        f'<t:ExtendedProperty><t:ExtendedFieldURI PropertySetId="{PROPERTY_SET}" PropertyName="{name}" '
        'PropertyType="String"/><t:Value>1</t:Value></t:ExtendedProperty>'
    )


def end_point_hash(pem: bytes) -> bytes:
    cert = x509.load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    algo = cert.signature_hash_algorithm
    if algo is None or isinstance(algo, (hashes.MD5, hashes.SHA1)):
        return hashlib.sha256(der).digest()
    h = hashes.Hash(algo)
    h.update(der)
    return h.finalize()


class FakeEws:
    def __init__(self, cert_pem: bytes) -> None:
        self.bindings = GssChannelBindings(application_data=b"tls-server-end-point:" + end_point_hash(cert_pem))
        self.acceptors: dict[tuple, object] = {}
        self.draft_body = ""
        self.draft_signed = False

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        conn = tuple(scope.get("client") or ())
        if scope["path"] == "/healthz":
            return await self._answer(send, 200, b"ok")
        if not body:  # a handshake leg carries no body
            if not auth.startswith("NTLM "):
                return await self._answer(send, 401, b"", [(b"www-authenticate", b"NTLM")])
            acceptor = spnego.server(protocol="ntlm", channel_bindings=self.bindings)
            self.acceptors[conn] = acceptor
            out = acceptor.step(base64.b64decode(auth[5:]))
            return await self._answer(send, 401, b"", [(b"www-authenticate", b"NTLM " + base64.b64encode(out))])
        acceptor = self.acceptors.pop(conn, None)
        accepted = False
        if acceptor is not None and auth.startswith("NTLM "):
            try:
                acceptor.step(base64.b64decode(auth[5:]))
                accepted = acceptor.complete
            except Exception:  # noqa: BLE001 - a wrong password is a failed acceptor step
                accepted = False
        print(json.dumps({"event": "ews_sign_in", "accepted": accepted}), flush=True)
        if not accepted:
            return await self._answer(send, 401, b"", [(b"www-authenticate", b"NTLM")])
        operation, answer = self.operation(body)
        print(json.dumps({"event": "ews_op", "op": operation}), flush=True)
        if answer is None:
            return await self._answer(send, 500, b"unsupported operation")
        return await self._answer(send, 200, answer, [(b"content-type", b"text/xml; charset=utf-8")])

    def operation(self, body: bytes) -> tuple[str, bytes | None]:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(body, parser=parser)
        op_el = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")[0]
        operation = etree.QName(op_el).localname
        if operation == "ResolveNames":
            return operation, RESOLVED
        if operation == "GetUserConfiguration":
            return operation, OWA_OPTIONS
        if operation == "GetMailTips":
            return operation, MAIL_TIPS
        if operation == "CreateItem":
            found = op_el.find(f".//{T}Body")
            self.draft_body = found.text or "" if found is not None else ""
            self.draft_signed = any(
                p.get("PropertyName") == "ExchangeMcpSignature" for p in op_el.iter(f"{T}ExtendedFieldURI")
            )
            return operation, _soap(
                "CreateItem", f'<m:Items><t:Message><t:ItemId Id="{DRAFT_ID}" ChangeKey="CK1"/></t:Message></m:Items>'
            )
        if operation == "GetItem":
            return operation, _soap(
                "GetItem",
                f'<m:Items><t:Message><t:ItemId Id="{DRAFT_ID}" ChangeKey="CK1"/><t:Subject>e2e</t:Subject>'
                f'<t:Body BodyType="HTML">{escape(self.draft_body)}</t:Body><t:IsDraft>true</t:IsDraft>'
                + _property("ExchangeMcpDraft")
                + (_property("ExchangeMcpSignature") if self.draft_signed else "")
                + "<t:ToRecipients><t:Mailbox><t:Name>Alex Example</t:Name><t:EmailAddress>alex@corp.example"
                "</t:EmailAddress></t:Mailbox></t:ToRecipients><t:IsRead>true</t:IsRead></t:Message></m:Items>",
            )
        return operation, None

    @staticmethod
    async def _answer(send, status: int, body: bytes, headers: list | None = None) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers or []})
        await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    cert, key = sys.argv[1], sys.argv[2]
    with open(cert, "rb") as f:
        app = FakeEws(f.read())
    uvicorn.run(app, host="0.0.0.0", port=8443, ssl_certfile=cert, ssl_keyfile=key, log_level="warning")  # noqa: S104
