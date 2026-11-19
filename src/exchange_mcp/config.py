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

    from exchange_mcp.core.time import windows_zone

    try:
        ZoneInfo(value)
    except Exception:
        raise ConfigError(f"{PREFIX}TIMEZONE is not a time zone") from None
    if windows_zone(value) is None:
        # Without the Windows name a repeating meeting is anchored to UTC and moves at clock changes.
        raise ConfigError(f"{PREFIX}TIMEZONE has no Windows zone name here; use a zone listed in core/time.py")
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
class StateSettings:
    """Where the shared state lives (`state.remote`). Absent: in this process, one replica."""

    url: str = ""  # redis:// or rediss://, standalone
    host: str = ""
    port: int = 6379
    sentinels: tuple[tuple[str, int], ...] = ()
    sentinel_master: str = "mymaster"
    db: int = 0
    tls: bool = False
    username: str = ""
    password_file: Path | None = None
    sentinel_password_file: Path | None = None
    prefix: str = "exchange-mcp:"

    @property
    def mode(self) -> str:
        return "redis" if self.url else "sentinel"


DEFAULT_STATE_PREFIX = "exchange-mcp:"


def _host_port(name: str, item: str) -> tuple[str, int]:
    item = item.strip()
    if item.startswith("["):  # [IPv6]:port
        host, sep, port = item[1:].partition("]:")
    else:
        host, sep, port = item.rpartition(":")
    if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ConfigError(f"{name} is a comma-separated list of host:port")
    return host, int(port)


def _networks(name: str, value: str) -> tuple[str, ...]:
    import ipaddress

    out = []
    for item in (v.strip() for v in value.split(",")):
        if not item:
            continue
        try:
            out.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise ConfigError(f"{name} is a comma-separated list of addresses or CIDR networks") from None
    return tuple(out)


def _db_range(db: int) -> None:
    if not 0 <= db <= 15:
        raise ConfigError(f"{PREFIX}STATE_DB, or the database in {PREFIX}STATE_URL, is 0-15")


def _state(get, prefix_default: str = DEFAULT_STATE_PREFIX) -> StateSettings | None:
    url = get("STATE_URL")
    hosts = [h for h in get("STATE_SENTINELS").split(",") if h.strip()]
    rest = {n: get(n) for n in ("STATE_USERNAME", "STATE_PASSWORD_FILE", "STATE_SENTINEL_PASSWORD_FILE", "STATE_DB")}
    loose = {**rest, "STATE_TLS": get("STATE_TLS"), "STATE_SENTINEL_MASTER": get("STATE_SENTINEL_MASTER")}
    if url and hosts:
        raise ConfigError(f"{PREFIX}STATE_URL and {PREFIX}STATE_SENTINELS are two different stores: set one")
    if not url and not hosts:
        stray = sorted(PREFIX + n for n, v in loose.items() if v)
        if stray:
            raise ConfigError(
                f"{', '.join(stray)} configure a store that is not named: set {PREFIX}STATE_URL or "
                f"{PREFIX}STATE_SENTINELS"
            )
        return None
    try:
        db = int(rest["STATE_DB"] or "0")
    except ValueError:
        raise ConfigError(f"{PREFIX}STATE_DB must be an integer") from None
    tls_word = get("STATE_TLS", "false").lower()
    if tls_word not in ("true", "false"):
        raise ConfigError(f"{PREFIX}STATE_TLS is true or false")
    tls = tls_word == "true"
    prefix = get("STATE_PREFIX", prefix_default)
    if not prefix or len(prefix) > 64 or any(c.isspace() for c in prefix):
        raise ConfigError(f"{PREFIX}STATE_PREFIX is 1-64 characters without spaces")
    common = {
        "db": db,
        "username": rest["STATE_USERNAME"],
        "password_file": _file(f"{PREFIX}STATE_PASSWORD_FILE", rest["STATE_PASSWORD_FILE"], required=False),
        "prefix": prefix,
    }
    if url:
        try:
            u = urlsplit(url)
            port = u.port or 6379
        except ValueError:
            raise ConfigError(f"{PREFIX}STATE_URL is not an address") from None
        if u.scheme not in ("redis", "rediss") or not u.hostname:
            raise ConfigError(f"{PREFIX}STATE_URL is redis://host:port/db or rediss://host:port/db")
        if u.username or u.password:
            raise ConfigError(
                f"{PREFIX}STATE_URL carries no credentials: use {PREFIX}STATE_USERNAME and {PREFIX}STATE_PASSWORD_FILE"
            )
        if u.query or u.fragment:
            raise ConfigError(f"{PREFIX}STATE_URL must not carry a query or a fragment")
        path = u.path.strip("/")
        if path:
            if not path.isdigit():
                raise ConfigError(f"{PREFIX}STATE_URL ends in a database number")
            if rest["STATE_DB"] and int(path) != db:
                raise ConfigError(f"{PREFIX}STATE_URL and {PREFIX}STATE_DB name different databases")
            common["db"] = int(path)
        _db_range(int(common["db"]))  # type: ignore[call-overload]
        if rest["STATE_SENTINEL_PASSWORD_FILE"]:
            raise ConfigError(f"{PREFIX}STATE_SENTINEL_PASSWORD_FILE is for Sentinel; this store is a URL")
        if get("STATE_TLS") and tls != (u.scheme == "rediss"):
            raise ConfigError(f"{PREFIX}STATE_TLS contradicts the scheme of {PREFIX}STATE_URL")
        return StateSettings(url=url, host=u.hostname, port=port, tls=u.scheme == "rediss", **common)
    _db_range(db)
    sentinels = tuple(_host_port(f"{PREFIX}STATE_SENTINELS", h) for h in hosts)
    master = get("STATE_SENTINEL_MASTER", "mymaster")
    return StateSettings(
        sentinels=sentinels,
        sentinel_master=master,
        tls=tls,
        sentinel_password_file=_file(
            f"{PREFIX}STATE_SENTINEL_PASSWORD_FILE", rest["STATE_SENTINEL_PASSWORD_FILE"], required=False
        ),
        **common,
    )


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
    client_hosts: frozenset[str] = frozenset()  # empty: any https or loopback redirect may register
    key_file: Path | None = None
    vault: Vault | None = None
    adfs: Adfs | None = None
    areas: frozenset[str] = field(default_factory=lambda: parse_areas(None))
    timezone: str = "UTC"
    refresh_hours: int = PASSWORD_REFRESH_DEFAULT_HOURS
    attempts_per_login: int = 5
    attempts_per_network: int = 20
    port: int = 8000
    metrics_port: int = 9090
    state: StateSettings | None = None
    # The proxies (an ingress controller) whose X-Forwarded-For names the client. Empty: the client's
    # network is unknown behind a proxy, so the per-network sign-in limit is off.
    trusted_proxies: tuple[str, ...] = ()

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
            metrics_port = int(get("METRICS_PORT", "9090"))
        except ValueError:
            raise ConfigError("numeric settings must be integers") from None
        if mode == "password" and not 1 <= refresh_hours <= PASSWORD_REFRESH_MAX_HOURS:
            raise ConfigError(f"{PREFIX}REFRESH_HOURS is 1-{PASSWORD_REFRESH_MAX_HOURS} in password mode")
        if not 1 <= metrics_port <= 65535 or metrics_port == port:
            raise ConfigError(f"{PREFIX}METRICS_PORT is a port of its own, never the public one")
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
            client_hosts=frozenset(h.strip().lower() for h in get("CLIENT_HOSTS").split(",") if h.strip()),
            key_file=_file(f"{PREFIX}KEY_FILE", get("KEY_FILE"), required=provider == "file"),
            vault=vault,
            adfs=adfs,
            areas=areas,
            timezone=_zone(get("TIMEZONE", "UTC")),
            refresh_hours=refresh_hours if mode == "password" else ADFS_REFRESH_HOURS,
            attempts_per_login=per_login,
            attempts_per_network=per_network,
            port=port,
            metrics_port=metrics_port,
            state=_state(get),
            trusted_proxies=_networks(f"{PREFIX}TRUSTED_PROXIES", get("TRUSTED_PROXIES")),
        )


def _missing(name: str) -> str:
    raise ConfigError(f"{PREFIX}{name} is required")
