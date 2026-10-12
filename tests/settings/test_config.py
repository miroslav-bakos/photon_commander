"""``AppConfig.advertised_host``/``origin`` — the pairing URL's own host.

Reported live: a wildcard bind (``0.0.0.0``) advertised ``localhost``, which
resolves to *the phone itself* when read off a scanned QR code, not this
host. Mocking follows ``tests/api/test_main.py``'s own pattern for
``psutil.net_if_addrs``.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

import pytest

from ssc.settings.config import AppConfig


@dataclass(frozen=True, slots=True)
class _FakeAddr:
    family: socket.AddressFamily
    address: str


def test_a_wildcard_bind_advertises_a_real_lan_address_not_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "psutil.net_if_addrs",
        lambda: {
            "lo": [_FakeAddr(socket.AF_INET, "127.0.0.1")],
            "eth0": [_FakeAddr(socket.AF_INET, "192.168.1.4")],
        },
    )
    config = AppConfig(host="0.0.0.0")
    assert config.advertised_host == "192.168.1.4"
    assert config.origin == "http://192.168.1.4:7777"


def test_a_wildcard_bind_with_no_private_interface_falls_back_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An isolated host — loopback only, or only a public address bound
    directly — was never reachable from a phone anyway; localhost is at
    least still correct for a browser on the same machine."""
    monkeypatch.setattr(
        "psutil.net_if_addrs",
        lambda: {"lo": [_FakeAddr(socket.AF_INET, "127.0.0.1")]},
    )
    config = AppConfig(host="0.0.0.0")
    assert config.advertised_host == "localhost"


def test_a_wildcard_bind_prefers_the_first_private_interface_when_multihomed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "psutil.net_if_addrs",
        lambda: {
            "lo": [_FakeAddr(socket.AF_INET, "127.0.0.1")],
            "eth0": [_FakeAddr(socket.AF_INET, "192.168.1.4")],
            "wlan0": [_FakeAddr(socket.AF_INET, "10.0.0.9")],
        },
    )
    config = AppConfig(host="0.0.0.0")
    assert config.advertised_host == "192.168.1.4"


def test_an_explicit_loopback_bind_still_advertises_localhost() -> None:
    """A different, intentional choice from the wildcard bind above -- "local
    only, no phone" -- so this one case keeps its old behaviour."""
    config = AppConfig(host="127.0.0.1")
    assert config.advertised_host == "localhost"
    assert config.origin == "http://localhost:7777"


def test_an_explicit_lan_host_is_advertised_verbatim() -> None:
    """Already a real, dialable address -- nothing to substitute."""
    config = AppConfig(host="192.168.100.50")
    assert config.advertised_host == "192.168.100.50"


def test_tls_disabled_by_default_and_origin_is_plain_http() -> None:
    config = AppConfig(host="192.168.100.50")
    assert config.tls_enabled is False
    assert config.scheme == "http"
    assert config.origin == "http://192.168.100.50:7777"


def test_tls_enabled_changes_the_scheme_and_the_origin() -> None:
    """Reported bug: a hardcoded scheme sent a pairing QR/CORS check to the
    wrong origin once TLS was turned on."""
    config = AppConfig(host="192.168.100.50", tls_enabled=True)
    assert config.scheme == "https"
    assert config.origin == "https://192.168.100.50:7777"


def test_standard_https_origin_omits_the_default_port() -> None:
    config = AppConfig(
        host="192.168.100.50",
        port=443,
        tls_enabled=True,
        tls_http_redirect_port=80,
    )
    assert config.origin == "https://192.168.100.50"
    assert config.tls_http_redirect_port == 80


def test_auth_required_defaults_to_no_environment_opinion() -> None:
    """``None`` — not ``False`` — so the stored Settings toggle can win
    (CLAUDE.md 2026-09-09: loginless by default, but a real setting, not a
    hardcoded posture)."""
    assert AppConfig().auth_required is None


def test_auth_required_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSC_AUTH_REQUIRED", "1")
    assert AppConfig().auth_required is True
    monkeypatch.setenv("SSC_AUTH_REQUIRED", "0")
    assert AppConfig().auth_required is False


def test_default_reconnect_wait_backs_off_after_a_prolonged_outage() -> None:
    from ssc.api.services import DEFAULT_RECONNECT_INITIAL_DELAY_S, DEFAULT_RECONNECT_MAX_DELAY_S

    config = AppConfig()
    assert config.reconnect_initial_delay_s == DEFAULT_RECONNECT_INITIAL_DELAY_S
    assert config.reconnect_max_delay_s == DEFAULT_RECONNECT_MAX_DELAY_S
    assert config.reconnect_initial_delay_s <= 2.0
    assert config.reconnect_max_delay_s == 60.0
