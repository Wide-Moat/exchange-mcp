"""How a request to EWS proves who sends it: NTLM with the person's password, or an AD FS bearer token.

NTLM is a three-leg handshake on one connection. When Exchange enforces Extended Protection, the
authenticate message must carry the TLS channel binding (the hash of the server certificate,
RFC 5929 `tls-server-end-point`); it is taken from the connection the handshake runs on.

The password lives in this object for one request and is never logged or put in an exception.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Generator

import httpx
import spnego
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from spnego.channel_bindings import GssChannelBindings


def _end_point_hash(der: bytes) -> bytes:
    """RFC 5929: the certificate's own signature hash, with MD5 and SHA-1 replaced by SHA-256."""
    cert = x509.load_der_x509_certificate(der)
    algo = cert.signature_hash_algorithm
    if algo is None or isinstance(algo, (hashes.MD5, hashes.SHA1)):
        return hashlib.sha256(der).digest()
    h = hashes.Hash(algo)
    h.update(der)
    return h.finalize()


def _peer_certificate(response: httpx.Response) -> bytes | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    ssl_object = stream.get_extra_info("ssl_object")
    return ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None


def _challenge(response: httpx.Response) -> bytes | None:
    for value in response.headers.get_list("www-authenticate"):
        scheme, _, token = value.partition(" ")
        if scheme.lower() == "ntlm" and token.strip():
            try:
                return base64.b64decode(token.strip(), validate=True)
            except ValueError:
                return None
    return None


class Ntlm(httpx.Auth):
    requires_request_body = True

    def __init__(self, login: str, password: str) -> None:
        self._login = login
        self._password = password

    def __repr__(self) -> str:  # never the credential
        return "Ntlm(<redacted>)"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # Leg 0, anonymous: Exchange answers 401 and the TLS handshake shows its certificate, which the
        # channel binding needs before the negotiate message is made (the message integrity code covers
        # every leg of one context, so the context cannot change after the first message).
        response = yield request
        if response.status_code != 401:
            return
        cert = _peer_certificate(response)
        bindings = (
            GssChannelBindings(application_data=b"tls-server-end-point:" + _end_point_hash(cert)) if cert else None
        )
        ctx = spnego.client(
            self._login,
            self._password,
            hostname=request.url.host,
            service="HTTP",
            protocol="ntlm",
            channel_bindings=bindings,
        )
        request.headers["Authorization"] = "NTLM " + base64.b64encode(ctx.step() or b"").decode()
        response = yield request
        if response.status_code != 401:
            return
        challenge = _challenge(response)
        if challenge is None:
            return
        request.headers["Authorization"] = "NTLM " + base64.b64encode(ctx.step(challenge) or b"").decode()
        yield request


class Bearer(httpx.Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "Bearer(<redacted>)"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request
