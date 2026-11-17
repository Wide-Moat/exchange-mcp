"""Sealed blobs: what the server would otherwise keep, carried by whoever needs it next.

The server keeps nothing between requests. The person's credential travels inside their OAuth tokens,
a sign-in step inside its form, a cursor inside the page that returned it, a file link inside its URL.
Each is JSON with its kind, its expiry and the key id that sealed it, encrypted and authenticated:

- `LocalSealer` uses AES-256-GCM with keys this process holds (`file` and `memory` providers). The kind
  and key id are also the associated data, so a blob of one kind never opens as another.
- `VaultSealer` hands the plaintext to a Vault or OpenBao transit key and never holds key material.
  The ciphertext is authenticated by transit, and the kind and expiry inside it are checked on open.

`open` returns None for anything forged, altered, of another kind, expired, or sealed under a key id
this server no longer holds: the caller answers 401 and the person signs in again.
"""

from __future__ import annotations

import base64
import json
import os
import time
import zlib
from collections.abc import Callable
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_BLOB = 16 * 1024  # larger is neither made nor opened
KEY_BYTES = 32


class TooLarge(ValueError):
    """The payload does not fit in one sealed blob."""


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s: str) -> bytes:
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if b64(raw) != s:  # one spelling per blob: the spare bits of the last character must be zero
        raise ValueError("non-canonical base64")
    return raw


class Sealer(Protocol):
    async def seal(self, kind: str, payload: dict[str, Any], ttl: float) -> str: ...

    async def open(self, kind: str, blob: str | None) -> dict[str, Any] | None: ...


def _body(kind: str, payload: dict[str, Any], ttl: float, now: float) -> bytes:
    if "k" in payload or "exp" in payload:
        raise ValueError("payload keys 'k' and 'exp' are reserved")
    return zlib.compress(json.dumps({**payload, "k": kind, "exp": int(now + ttl)}, separators=(",", ":")).encode(), 9)


def _check(kind: str, raw: bytes, now: float) -> dict[str, Any] | None:
    try:
        body = json.loads(zlib.decompress(raw))
    except (zlib.error, ValueError):
        return None
    if not isinstance(body, dict) or body.get("k") != kind:
        return None
    exp = body.get("exp")
    if not isinstance(exp, int) or exp < now:
        return None
    body.pop("k")
    return body


class LocalSealer:
    """AES-256-GCM under the keys this process holds. The first key seals; every key opens."""

    def __init__(self, keys: list[tuple[str, bytes]], clock: Callable[[], float] = time.time) -> None:
        if not keys:
            raise ValueError("at least one key is required")
        for kid, key in keys:
            if not kid or "." in kid or len(kid) > 32:
                raise ValueError("a key id is 1-32 characters without a dot")
            if len(key) != KEY_BYTES:
                raise ValueError("a key is exactly 32 bytes")
        if len({kid for kid, _ in keys}) != len(keys):
            raise ValueError("key ids must be unique")
        self._active = keys[0][0]
        self._aead = {kid: AESGCM(key) for kid, key in keys}
        self._clock = clock

    @property
    def key_id(self) -> str:
        return self._active

    async def seal(self, kind: str, payload: dict[str, Any], ttl: float) -> str:
        nonce = os.urandom(12)
        ct = self._aead[self._active].encrypt(nonce, _body(kind, payload, ttl, self._clock()), _aad(kind, self._active))
        blob = f"{self._active}.{b64(nonce + ct)}"
        if len(blob) > MAX_BLOB:  # never hand out what open() would refuse
            raise TooLarge(len(blob))
        return blob

    async def open(self, kind: str, blob: str | None) -> dict[str, Any] | None:
        if not blob or len(blob) > MAX_BLOB or "." not in blob:
            return None
        kid, _, rest = blob.partition(".")
        aead = self._aead.get(kid)
        if aead is None:
            return None
        try:
            raw = unb64(rest)
            plain = aead.decrypt(raw[:12], raw[12:], _aad(kind, kid))
        except (ValueError, InvalidTag, TypeError):
            return None
        return _check(kind, plain, self._clock())


def _aad(kind: str, kid: str) -> bytes:
    return f"exchange-mcp\n{kind}\n{kid}".encode()


class VaultSealer:
    """Seals through a Vault or OpenBao transit key. Key material never enters this process; every
    seal and open is a request the secrets manager can audit."""

    def __init__(self, transit, clock: Callable[[], float] = time.time) -> None:
        self._transit = transit  # keys.vault.Transit
        self._clock = clock

    async def seal(self, kind: str, payload: dict[str, Any], ttl: float) -> str:
        ct = await self._transit.encrypt(_body(kind, payload, ttl, self._clock()))
        blob = "v." + b64(ct.encode())
        if len(blob) > MAX_BLOB:
            raise TooLarge(len(blob))
        return blob

    async def open(self, kind: str, blob: str | None) -> dict[str, Any] | None:
        if not blob or len(blob) > MAX_BLOB or not blob.startswith("v."):
            return None
        try:
            ct = unb64(blob[2:]).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        plain = await self._transit.decrypt(ct)
        return None if plain is None else _check(kind, plain, self._clock())
