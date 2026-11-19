"""A stand-in Exchange for the chart's end-to-end run (scripts/chart-e2e.sh): HTTPS with a certificate from
a CA made for the run, NTLM checked by a real acceptor (pyspnego) against a user file, channel binding
required as IIS Extended Protection requires it, and one operation, `ResolveNames`, which is what a
password sign-in sends. It runs in the server's own image, which already has everything it needs.

    NTLM_USER_FILE=/fake/users python /fake/fake_ews.py /tls/tls.crt /tls/tls.key

It logs one JSON line per sign-in check with the outcome only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys

import spnego
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
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
        return await self._answer(send, 200, RESOLVED, [(b"content-type", b"text/xml; charset=utf-8")])

    @staticmethod
    async def _answer(send, status: int, body: bytes, headers: list | None = None) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers or []})
        await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    cert, key = sys.argv[1], sys.argv[2]
    with open(cert, "rb") as f:
        app = FakeEws(f.read())
    uvicorn.run(app, host="0.0.0.0", port=8443, ssl_certfile=cert, ssl_keyfile=key, log_level="warning")  # noqa: S104
