"""Where the sealing key comes from (docs/auth-modes.md, "The sealing key").

- `file`: a key file the deployment generated. One key per line, `<key id>:<base64url 32 bytes>`; the
  first line seals, every line opens, so a rotation adds a line on top and later removes the old one.
  The file must be readable by its owner only.
- `vault`: a transit key in Vault or OpenBao; nothing is held here.
- `memory`: 32 random bytes made now and lost at exit. One replica; everyone signs in after a restart.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import httpx

from exchange_mcp.config import ConfigError, Settings
from exchange_mcp.keys.seal import KEY_BYTES, LocalSealer, Sealer, VaultSealer, unb64
from exchange_mcp.keys.vault import Transit


def read_key_file(path: Path) -> list[tuple[str, bytes]]:
    try:
        st = path.stat()
    except OSError:
        raise ConfigError("the key file cannot be read") from None
    # Kubernetes mounts Secret files through a symlink; the mode that matters is the target's.
    if stat.S_IMODE(st.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError("the key file is readable by group or others; mount it with defaultMode 0400")
    keys: list[tuple[str, bytes]] = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        kid, sep, material = line.partition(":")
        try:
            key = unb64(material) if sep else b""
        except ValueError:
            key = b""
        if not kid or len(key) != KEY_BYTES:
            raise ConfigError(f"the key file line {n} is not <key id>:<base64url of 32 bytes>")
        keys.append((kid, key))
    if not keys:
        raise ConfigError("the key file holds no key")
    return keys


def make_sealer(settings: Settings, http: httpx.AsyncClient) -> Sealer:
    if settings.key_provider == "file":
        assert settings.key_file is not None
        return LocalSealer(read_key_file(settings.key_file))
    if settings.key_provider == "vault":
        assert settings.vault is not None
        v = settings.vault
        return VaultSealer(Transit(http, v.addr, v.key_name, v.token_file, v.mount))
    return LocalSealer([(f"m{secrets.token_hex(4)}", os.urandom(KEY_BYTES))])
