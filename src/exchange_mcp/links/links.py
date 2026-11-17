"""Short-lived sealed links that move bytes between the agent's sandbox and Exchange, so the bytes never
enter the model's context (manifesto rule 10).

A link carries what it may do inside its own URL, sealed (`keys.seal`): a download link names one
attachment and the person whose credential fetches it; an upload link names one draft this server made,
the file name, and the person. Nothing is kept on the server, so any replica serves any link. The
credential inside a link is sealed like the one inside a token and lives at most `MAX_TTL`.
"""

from __future__ import annotations

import secrets
import shlex
import time
from collections.abc import Callable

from exchange_mcp.auth.credential import Credential
from exchange_mcp.keys.seal import Sealer

DOWNLOAD_TTL = 300
UPLOAD_TTL = 180
MAX_TTL = 900
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
BUSY_MAX = 10_000


class Links:
    def __init__(self, public_url: str, sealer: Sealer, clock: Callable[[], float] = time.time) -> None:
        self.public_url = public_url.rstrip("/")
        self.sealer = sealer
        self._clock = clock
        self._busy: dict[str, float] = {}  # one upload at a time per link on this replica

    async def download(self, cred: Credential, person: str, attachment_id: str, name: str, size: int | None) -> dict:
        lid = await self.sealer.seal(
            "download", {"a": attachment_id, "n": name, "s": size, "p": person, "c": cred.to_payload()}, DOWNLOAD_TTL
        )
        url = f"{self.public_url}/files/d/{lid}"
        # --globoff: curl expands {a,b} and [1-9] in its own arguments, whatever the shell quoting
        return {
            "url": url,
            "command": f"curl -fsS --globoff -o '{safe_name(name)}' '{url}'",
            "expires_in": DOWNLOAD_TTL,
        }

    async def opened_download(self, lid: str) -> dict | None:
        return await self.sealer.open("download", lid)

    async def upload(self, cred: Credential, person: str, draft_id: str, op: str, name: str = "") -> dict:
        """op: `attach` (a file for the draft) or `body` (the draft's HTML body)."""
        uid = secrets.token_urlsafe(12)
        body = {"u": uid, "o": op, "d": draft_id, "n": name, "p": person, "c": cred.to_payload()}
        url = f"{self.public_url}/files/u/{await self.sealer.seal('upload', body, UPLOAD_TTL)}"
        local = name if op == "attach" else "letter.html"
        return {
            "name": local,
            "url": url,
            "expires_in": UPLOAD_TTL,
            # the person's own local file, quoted as it is
            "command": f"curl -fsS --globoff -T {shlex.quote(local)} '{url}'",
        }

    async def claim_upload(self, lid: str) -> dict | None:
        body = await self.sealer.open("upload", lid)
        now = self._clock()
        if body is None or self._busy.get(body["u"], 0) > now:
            return None
        if len(self._busy) >= BUSY_MAX:
            for k in [k for k, exp in self._busy.items() if exp < now]:
                del self._busy[k]
        self._busy[body["u"]] = now + UPLOAD_TTL
        return body

    def release_upload(self, uid: str) -> None:
        self._busy.pop(uid, None)


def safe_name(name: str) -> str:
    """A bare file name for a shell command: no directories (an attachment name is someone else's text, and
    `../../.bashrc` must not become a path), no quotes, no control characters, no leading dash."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch not in "'\"`$" and ch.isprintable())[:200].strip()
    return base if base not in ("", ".", "..") and not base.startswith(("-", ".")) else "file"
