"""Settings: https everywhere, verification always on, password-mode refresh capped, login formats sane."""

from __future__ import annotations

import pytest
from conftest import base_env

from exchange_mcp.app import http_client
from exchange_mcp.config import ConfigError, Settings


@pytest.mark.parametrize(
    "key,value",
    [
        ("EWS_URL", "http://mail.example.com/EWS/Exchange.asmx"),
        ("EWS_URL", "https://user:pw@mail.example.com/EWS/Exchange.asmx"),
        ("PUBLIC_URL", "http://mcp.example.com"),
        ("PUBLIC_URL", "https://mcp.example.com/path"),
    ],
)
def test_tls_is_always_verified(key, value):
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(**{key: value}))


def test_the_http_client_verifies_certificates():
    s = Settings.from_env(base_env())
    client = http_client(s)
    pool = client._transport._pool  # noqa: SLF001
    ctx = pool._ssl_context  # noqa: SLF001
    import ssl

    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert client.follow_redirects is False


def test_there_is_no_setting_that_turns_verification_off():
    import inspect

    import exchange_mcp.app as app
    import exchange_mcp.config as config

    for mod in (app, config):
        src = inspect.getsource(mod)
        assert "verify=False" not in src and "CERT_NONE" not in src


def test_password_mode_refresh_is_capped():
    with pytest.raises(ConfigError, match="1-24"):
        Settings.from_env(base_env(REFRESH_HOURS="25"))
    assert Settings.from_env(base_env()).refresh_hours == 12
    assert Settings.from_env(base_env(REFRESH_HOURS="24")).refresh_hours == 24


def test_login_domain_matches_the_format():
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(LOGIN_FORMAT="down_level", LOGIN_DOMAIN="corp.example"))
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(LOGIN_FORMAT="upn", LOGIN_DOMAIN="CORP"))
    with pytest.raises(ConfigError, match="required"):
        Settings.from_env(base_env(LOGIN_DOMAIN=""))
    assert Settings.from_env(base_env(LOGIN_FORMAT="down_level", LOGIN_DOMAIN="CORP")).login_domain == "CORP"


def test_the_file_provider_needs_an_absolute_key_file():
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(KEY_PROVIDER="file"))
    with pytest.raises(ConfigError, match="absolute"):
        Settings.from_env(base_env(KEY_PROVIDER="file", KEY_FILE="key"))


def test_unknown_areas_are_refused():
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(AREAS="mail,rules"))


# ---------------------------------------------------------------- the shared state store


def test_no_store_means_memory_and_a_store_is_one_of_two_kinds():
    assert Settings.from_env(base_env()).state is None
    url = Settings.from_env(base_env(STATE_URL="redis://valkey:6379/2")).state
    assert (url.mode, url.host, url.port, url.db, url.tls, url.prefix) == (
        "redis",
        "valkey",
        6379,
        2,
        False,
        "exchange-mcp:",
    )
    tls = Settings.from_env(base_env(STATE_URL="rediss://valkey:6380")).state
    assert tls.tls and tls.port == 6380 and tls.db == 0
    s = Settings.from_env(base_env(STATE_SENTINELS="s1:26379, s2:26379,[fd00::1]:26379", STATE_DB="1")).state
    assert s.mode == "sentinel" and s.sentinel_master == "mymaster" and s.db == 1
    assert s.sentinels == (("s1", 26379), ("s2", 26379), ("fd00::1", 26379))
    assert s.password_file is None and s.sentinel_password_file is None  # no password is a first-class setup


@pytest.mark.parametrize(
    ("extra", "says"),
    [
        ({"STATE_URL": "redis://v:6379", "STATE_SENTINELS": "s:26379"}, "two different stores"),
        ({"STATE_URL": "redis://user:pw@v:6379"}, "carries no credentials"),
        ({"STATE_URL": "http://v:6379"}, "redis://host"),
        ({"STATE_URL": "redis://v:6379/1", "STATE_DB": "2"}, "different databases"),
        ({"STATE_URL": "redis://v:6379", "STATE_TLS": "true"}, "contradicts"),
        ({"STATE_URL": "redis://v:6379", "STATE_SENTINEL_PASSWORD_FILE": "/run/x"}, "is for Sentinel"),
        ({"STATE_SENTINELS": "s1"}, "host:port"),
        ({"STATE_SENTINELS": "s1:99999"}, "host:port"),
        ({"STATE_SENTINELS": "s1:26379", "STATE_DB": "x"}, "integer"),
        ({"STATE_SENTINELS": "s1:26379", "STATE_TLS": "yes"}, "true or false"),
        ({"STATE_SENTINELS": "s1:26379", "STATE_PASSWORD_FILE": "relative"}, "absolute path"),
        ({"STATE_PASSWORD_FILE": "/run/x"}, "not named"),
        ({"STATE_SENTINEL_MASTER": "other"}, "not named"),
        ({"METRICS_PORT": "8000"}, "port of its own"),
    ],
)
def test_a_store_configured_wrong_stops_the_start(extra, says):
    with pytest.raises(ConfigError) as e:
        Settings.from_env(base_env(**extra))
    assert says in str(e.value)


def test_a_store_password_comes_from_a_closed_file_only(tmp_path):
    from exchange_mcp.state.remote import read_password

    good = tmp_path / "pw"
    good.write_text("s3cret\n")
    good.chmod(0o400)
    assert read_password(good, "X") == "s3cret"
    wide = tmp_path / "wide"
    wide.write_text("s3cret")
    wide.chmod(0o440)
    empty = tmp_path / "empty"
    empty.write_text("\n")
    empty.chmod(0o400)
    big = tmp_path / "big"
    big.write_text("x" * 5000)
    big.chmod(0o400)
    for path, says in (
        (wide, "readable by others"),
        (empty, "empty"),
        (big, "larger"),
        (tmp_path, "regular file"),
        (tmp_path / "absent", "cannot be read"),
    ):
        with pytest.raises(ConfigError) as e:
            read_password(path, "X")
        assert says in str(e.value)
    with pytest.raises(ConfigError):  # the environment never carries it
        Settings.from_env(base_env(STATE_PASSWORD="s3cret"))
