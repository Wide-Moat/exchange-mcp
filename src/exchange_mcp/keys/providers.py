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


def exposed(st: os.stat_result) -> bool:
    """Readable by anyone outside this process's own user and groups. Kubernetes mounts a Secret for a
    non-root pod as root:<fsGroup> 0440, so the pod's own group may read it; nobody else may."""
    mode = stat.S_IMODE(st.st_mode)
    if mode & stat.S_IRWXO:
        return True
    return bool(mode & stat.S_IRWXG) and st.st_gid not in {os.getgid(), *os.getgroups()}


MAX_SECRET_FILE = 64 * 1024


def read_bounded(path: Path, limit: int = MAX_SECRET_FILE) -> bytes:
    """A secret file, refused when larger than any real one: a wrong mount cannot exhaust memory."""
    with path.open("rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ConfigError(f"{path.name} is larger than {limit} bytes; it is not a key file")
    return data


def read_key_file(path: Path) -> list[tuple[str, bytes]]:
    try:
        st = path.stat()
    except OSError:
        raise ConfigError("the key file cannot be read") from None
    if exposed(st):
        raise ConfigError(
            "the key file is readable by others or by a group this server is not in; mount it 0400, or 0440 "
            "with the pod's fsGroup"
        )
    keys: list[tuple[str, bytes]] = []
    try:
        lines = read_bounded(path).decode("ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        raise ConfigError("the key file cannot be read as text") from None
    for n, line in enumerate(lines, 1):
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
        if any(kid == k for k, _ in keys):
            raise ConfigError(f"the key file names key id {kid} twice; a rotation adds a new id on top")
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
