"""The sealing key: where it may come from, and what a blob refuses to be."""

from __future__ import annotations

import os
import stat

import httpx
import pytest
from conftest import base_env, key_line

from exchange_mcp.config import ConfigError, Settings
from exchange_mcp.keys.providers import read_key_file
from exchange_mcp.keys.seal import LocalSealer, VaultSealer
from exchange_mcp.keys.vault import Transit


@pytest.mark.parametrize(
    "name", ["SEALING_KEY", "SECRET", "KEYS", "VAULT_TOKEN", "ADFS_PRIVATE_KEY", "PASSWORD", "LINK_SECRETS"]
)
def test_a_key_in_the_environment_stops_the_server(name):
    with pytest.raises(ConfigError, match="never read from the environment"):
        Settings.from_env(base_env(**{name: "x" * 44}))


def test_the_named_files_are_allowed(tmp_path):
    f = tmp_path / "key"
    f.write_text(key_line())
    f.chmod(0o400)
    s = Settings.from_env(base_env(KEY_PROVIDER="file", KEY_FILE=str(f)))
    assert s.key_file == f


def test_a_weak_or_open_key_file_stops_the_server(tmp_path):
    f = tmp_path / "key"
    f.write_text(key_line())
    f.chmod(0o404)
    with pytest.raises(ConfigError, match="readable by others"):
        read_key_file(f)
    f.chmod(0o400)
    assert len(read_key_file(f)) == 1
    f.chmod(0o440)  # the pod's own fsGroup, as Kubernetes mounts a Secret for a non-root pod
    assert len(read_key_file(f)) == 1
    import os as _os

    foreign = next(g for g in range(60000, 61000) if g not in {_os.getgid(), *_os.getgroups()})
    st = f.stat()
    fake = _os.stat_result((st.st_mode, *st[1:5], foreign, *st[6:]))
    from exchange_mcp.keys.providers import exposed

    assert exposed(fake)  # group-readable by a group this process is not in
    short = tmp_path / "short"
    short.write_text("k1:" + "A" * 20)
    short.chmod(0o400)
    with pytest.raises(ConfigError, match="32 bytes"):
        read_key_file(short)
    empty = tmp_path / "empty"
    empty.write_text("\n")
    empty.chmod(0o400)
    with pytest.raises(ConfigError, match="no key"):
        read_key_file(empty)


async def test_a_rotated_key_file_seals_with_the_first_and_opens_with_all(tmp_path):
    old, new = os.urandom(32), os.urandom(32)
    f = tmp_path / "key"
    f.write_text(key_line("old", old))
    f.chmod(0o600)
    before = LocalSealer(read_key_file(f))
    old_blob = await before.seal("access", {"x": 1}, 60)
    f.write_text(key_line("new", new) + "\n" + key_line("old", old))
    after = LocalSealer(read_key_file(f))
    assert after.key_id == "new"
    assert (await after.open("access", old_blob))["x"] == 1  # still opens during the rotation
    new_blob = await after.seal("access", {"x": 2}, 60)
    assert new_blob.startswith("new.") and await before.open("access", new_blob) is None
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


async def test_kind_expiry_and_tampering():
    now = [1000.0]
    s = LocalSealer([("k1", os.urandom(32))], clock=lambda: now[0])
    blob = await s.seal("access", {"x": 1}, 60)
    assert (await s.open("access", blob))["x"] == 1
    assert await s.open("refresh", blob) is None  # another kind
    tampered = blob[:-2] + ("A" if blob[-2] != "A" else "B") + blob[-1]
    assert await s.open("access", tampered) is None
    now[0] += 61
    assert await s.open("access", blob) is None  # expired


async def test_a_token_of_a_retired_key_does_not_open():
    a = LocalSealer([("a", os.urandom(32))])
    b = LocalSealer([("b", os.urandom(32))])
    blob = await a.seal("access", {}, 60)
    assert await b.open("access", blob) is None


async def test_reserved_payload_keys_are_refused():
    s = LocalSealer([("k1", os.urandom(32))])
    with pytest.raises(ValueError):
        await s.seal("access", {"k": "refresh"}, 60)


async def test_vault_sealer_never_holds_key_material(tmp_path):
    """The transit fake encrypts with a key only it knows; the sealer only ever sees ciphertext."""
    import base64

    secret = os.urandom(32)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        calls.append(req.url.path)
        assert req.headers["X-Vault-Token"] == "vault-token"
        body = json.loads(req.content)
        if req.url.path.endswith("/encrypt/exchange-mcp"):
            n = os.urandom(12)
            ct = AESGCM(secret).encrypt(n, base64.b64decode(body["plaintext"]), None)
            return httpx.Response(200, json={"data": {"ciphertext": "vault:v1:" + base64.b64encode(n + ct).decode()}})
        try:
            raw = base64.b64decode(body["ciphertext"].removeprefix("vault:v1:"))
            pt = AESGCM(secret).decrypt(raw[:12], raw[12:], None)
        except Exception:
            return httpx.Response(400, json={"errors": ["cipher: message authentication failed"]})
        return httpx.Response(200, json={"data": {"plaintext": base64.b64encode(pt).decode()}})

    token = tmp_path / "token"
    token.write_text("vault-token\n")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = VaultSealer(Transit(http, "https://vault.example.com", "exchange-mcp", token))
    blob = await s.seal("access", {"x": 2}, 60)
    assert blob.startswith("v.")
    assert (await s.open("access", blob))["x"] == 2
    assert await s.open("refresh", blob) is None
    assert await s.open("access", blob[:-3] + "AAA") is None
    assert all(p.startswith("/v1/transit/") for p in calls)


def test_the_vault_address_must_be_https(tmp_path):
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env(
            base_env(
                KEY_PROVIDER="vault",
                VAULT_ADDR="http://vault.example.com",
                VAULT_KEY_NAME="exchange-mcp",
                VAULT_TOKEN_FILE="/var/run/vault/token",
            )
        )
