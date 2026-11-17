"""Settings, from the environment. Nothing here names an installation, and nothing secret comes from here:
key material is read from files or from a secrets manager (`keys.providers`), and a variable that looks
like it carries a key, a secret, a token or a password stops the server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from exchange_mcp.registry import parse_areas

PREFIX = "EXCHANGE_MCP_"
# A variable ending in one of these would carry material; files and the secrets manager carry it instead.
FORBIDDEN_SUFFIXES = ("_KEY", "_KEYS", "_SECRET", "_SECRETS", "_TOKEN", "_PASSWORD")

AuthMode = Literal["adfs", "password"]
LoginFormat = Literal["upn", "down_level"]
KeyProvider = Literal["vault", "file", "memory"]

PASSWORD_REFRESH_MAX_HOURS = 24
PASSWORD_REFRESH_DEFAULT_HOURS = 12
ADFS_REFRESH_HOURS = 30 * 24


class ConfigError(ValueError):
    pass


def _https(name: str, value: str, *, bare: bool = False) -> str:
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ConfigError(f"{name} must be an https address without credentials")
    if u.query or u.fragment:
        raise ConfigError(f"{name} must not carry a query or a fragment")
    if bare and u.path not in ("", "/"):
        raise ConfigError(f"{name} is a bare origin: no path")
    return value.rstrip("/")


def _public_url(value: str) -> str:
    u = urlsplit(value)
    local = u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1") and not u.username
    if local and u.path in ("", "/") and not u.query and not u.fragment:
        return value.rstrip("/")  # local development only
    return _https(f"{PREFIX}PUBLIC_URL", value, bare=True)


def _file(name: str, value: str | None, *, required: bool) -> Path | None:
    if not value:
        if required:
            raise ConfigError(f"{name} is required")
        return None
    p = Path(value)
    if not p.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return p


def _zone(value: str) -> str:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(value)
    except Exception:
        raise ConfigError(f"{PREFIX}TIMEZONE is not a time zone") from None
    return value


def _choice(name: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)}")
    return value


def refuse_material_in_env(env: dict[str, str]) -> None:
    named = sorted(k for k in env if k.startswith(PREFIX) and k.endswith(FORBIDDEN_SUFFIXES))
    if named:
        raise ConfigError(
            f"{', '.join(named)}: keys, secrets, tokens and passwords are never read from the environment, "
            "where /proc and the pod spec show them. Use a file or the secrets manager (docs/auth-modes.md)."
        )


@dataclass(frozen=True)
class Adfs:
    issuer: str  # https://<adfs host>/adfs
    client_id: str
    signing_key_file: Path  # PEM private key for private_key_jwt
    signing_key_id: str  # the kid AD FS knows the key by
    resource: str  # the Exchange Web API identifier


@dataclass(frozen=True)
class Vault:
    addr: str
    key_name: str
    token_file: Path
    mount: str = "transit"


@dataclass(frozen=True)
class Settings:
    public_url: str
    ews_url: str
    auth_mode: AuthMode
    key_provider: KeyProvider
    ca_bundle: Path | None = None
    login_format: LoginFormat = "upn"
    login_domain: str = ""
    login_also: frozenset[str] = frozenset()
    key_file: Path | None = None
    vault: Vault | None = None
    adfs: Adfs | None = None
    areas: frozenset[str] = field(default_factory=lambda: parse_areas(None))
    timezone: str = "UTC"
    refresh_hours: int = PASSWORD_REFRESH_DEFAULT_HOURS
    attempts_per_login: int = 5
    attempts_per_network: int = 20
    port: int = 8000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ if env is None else env)
        refuse_material_in_env(e)

        def get(name: str, default: str = "") -> str:
            return (e.get(PREFIX + name) or default).strip()

        mode = _choice(f"{PREFIX}AUTH_MODE", get("AUTH_MODE"), ("adfs", "password"))
        provider = _choice(f"{PREFIX}KEY_PROVIDER", get("KEY_PROVIDER", "file"), ("vault", "file", "memory"))

        login_format = _choice(f"{PREFIX}LOGIN_FORMAT", get("LOGIN_FORMAT", "upn"), ("upn", "down_level"))
        login_domain = get("LOGIN_DOMAIN")
        if mode == "password" and not login_domain:
            raise ConfigError(f"{PREFIX}LOGIN_DOMAIN is required in password mode")
        if login_format == "down_level" and ("." in login_domain or "\\" in login_domain):
            raise ConfigError(f"{PREFIX}LOGIN_DOMAIN is the NetBIOS domain name for down_level logins")
        login_also = frozenset(a.strip().lower() for a in get("LOGIN_ALSO").split(",") if a.strip())
        if login_format == "upn" and login_domain and ("." not in login_domain or "@" in login_domain):
            raise ConfigError(f"{PREFIX}LOGIN_DOMAIN is the UPN suffix (example: corp.example) for upn logins")

        vault = None
        if provider == "vault":
            vault = Vault(
                addr=_https(f"{PREFIX}VAULT_ADDR", get("VAULT_ADDR"), bare=True),
                key_name=get("VAULT_KEY_NAME") or _missing("VAULT_KEY_NAME"),
                token_file=_file(f"{PREFIX}VAULT_TOKEN_FILE", get("VAULT_TOKEN_FILE"), required=True),  # type: ignore[arg-type]
                mount=get("VAULT_MOUNT", "transit"),
            )

        adfs = None
        if mode == "adfs":
            adfs = Adfs(
                issuer=_https(f"{PREFIX}ADFS_ISSUER", get("ADFS_ISSUER")),
                client_id=get("ADFS_CLIENT_ID") or _missing("ADFS_CLIENT_ID"),
                signing_key_file=_file(f"{PREFIX}ADFS_KEY_FILE", get("ADFS_KEY_FILE"), required=True),  # type: ignore[arg-type]
                signing_key_id=get("ADFS_KEY_ID") or _missing("ADFS_KEY_ID"),
                resource=get("ADFS_RESOURCE") or _missing("ADFS_RESOURCE"),
            )

        try:
            refresh_hours = int(get("REFRESH_HOURS", str(PASSWORD_REFRESH_DEFAULT_HOURS)))
            per_login = int(get("ATTEMPTS_PER_LOGIN", "5"))
            per_network = int(get("ATTEMPTS_PER_NETWORK", "20"))
            port = int(e.get("PORT", "8000"))
        except ValueError:
            raise ConfigError("numeric settings must be integers") from None
        if mode == "password" and not 1 <= refresh_hours <= PASSWORD_REFRESH_MAX_HOURS:
            raise ConfigError(f"{PREFIX}REFRESH_HOURS is 1-{PASSWORD_REFRESH_MAX_HOURS} in password mode")
        if not (1 <= per_login <= 10 and 1 <= per_network <= 100):
            raise ConfigError("sign-in attempt limits are 1-10 per login and 1-100 per network")

        try:
            areas = parse_areas(get("AREAS") or None)
        except ValueError as err:
            raise ConfigError(str(err)) from None

        return cls(
            public_url=_public_url(get("PUBLIC_URL")),
            ews_url=_https(f"{PREFIX}EWS_URL", get("EWS_URL")),
            auth_mode=mode,  # type: ignore[arg-type]
            key_provider=provider,  # type: ignore[arg-type]
            ca_bundle=_file(f"{PREFIX}CA_BUNDLE", get("CA_BUNDLE"), required=False),
            login_format=login_format,  # type: ignore[arg-type]
            login_domain=login_domain,
            login_also=login_also,
            key_file=_file(f"{PREFIX}KEY_FILE", get("KEY_FILE"), required=provider == "file"),
            vault=vault,
            adfs=adfs,
            areas=areas,
            timezone=_zone(get("TIMEZONE", "UTC")),
            refresh_hours=refresh_hours if mode == "password" else ADFS_REFRESH_HOURS,
            attempts_per_login=per_login,
            attempts_per_network=per_network,
            port=port,
        )


def _missing(name: str) -> str:
    raise ConfigError(f"{PREFIX}{name} is required")
