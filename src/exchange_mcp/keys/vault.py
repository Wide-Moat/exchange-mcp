"""The transit engine of Vault or OpenBao (the same API): encrypt and decrypt without the key.

The token this server presents comes from a file (written by the Vault agent or a projected
Kubernetes token exchange), read on each request so a renewed token is picked up. It never comes
from the environment.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx


class TransitError(RuntimeError):
    """Transit refused or failed. The message never carries plaintext or ciphertext."""


class Transit:
    def __init__(self, http: httpx.AsyncClient, addr: str, key: str, token_file: Path, mount: str = "transit") -> None:
        if not addr.startswith("https://"):
            raise ValueError("the secrets manager address must be https")
        self._http = http
        self._base = f"{addr.rstrip('/')}/v1/{mount.strip('/')}"
        self._key = key
        self._token_file = token_file

    def _headers(self) -> dict[str, str]:
        with self._token_file.open("rb") as f:
            raw = f.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise TransitError("the secrets manager token file is larger than any token")
        token = raw.decode("ascii", "replace").strip()
        if not token:
            raise TransitError("the secrets manager token file is empty")
        return {"X-Vault-Token": token}

    async def encrypt(self, plaintext: bytes) -> str:
        r = await self._http.post(
            f"{self._base}/encrypt/{self._key}",
            json={"plaintext": base64.b64encode(plaintext).decode()},
            headers=self._headers(),
        )
        if r.status_code != 200:
            raise TransitError(f"transit encrypt answered {r.status_code}")
        return r.json()["data"]["ciphertext"]

    async def decrypt(self, ciphertext: str) -> bytes | None:
        """The plaintext, or None when transit says the ciphertext is not one of its own."""
        r = await self._http.post(
            f"{self._base}/decrypt/{self._key}", json={"ciphertext": ciphertext}, headers=self._headers()
        )
        if r.status_code == 400:  # malformed, altered, or of a key version that was trimmed
            return None
        if r.status_code != 200:
            raise TransitError(f"transit decrypt answered {r.status_code}")
        try:
            return base64.b64decode(r.json()["data"]["plaintext"], validate=True)
        except (KeyError, ValueError):
            return None
