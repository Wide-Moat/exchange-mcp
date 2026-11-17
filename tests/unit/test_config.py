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
