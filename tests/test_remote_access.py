"""Read-only Tailscale Serve detection never configures or exposes SSC."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from ssc.remote_access import CommandResult, inspect_tailscale


def runner_for(*, backend: str = "Running", serve: object | None = None):
    calls: list[list[str]] = []

    def run(arguments: Sequence[str]) -> CommandResult:
        command = list(arguments)
        calls.append(command)
        if command[1:3] == ["status", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "BackendState": backend,
                        "Self": {"DNSName": "observatory.example.ts.net."},
                    }
                ),
                "",
            )
        if command[1:] == ["serve", "status", "--json"]:
            if serve is None:
                return CommandResult(1, "", "no serve config")
            return CommandResult(0, json.dumps(serve), "")
        raise AssertionError(f"unexpected command: {command}")

    return run, calls


def serve_document(*, port: int = 7777, funnel: bool = False) -> dict[str, object]:
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "observatory.example.ts.net:443": {
                "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}},
            }
        },
        "AllowFunnel": {"observatory.example.ts.net:443": True} if funnel else {},
    }


def test_absent_cli_is_reported_without_running_any_command() -> None:
    status = inspect_tailscale(7777, which=lambda _name: None)
    assert status.state == "not_installed"
    assert status.url is None
    assert status.setup_command == "sudo tailscale serve --bg 127.0.0.1:7777"


def test_non_loopback_ssc_bind_is_reported_before_tailscale_state() -> None:
    calls: list[Sequence[str]] = []

    def runner(arguments: Sequence[str]) -> CommandResult:
        calls.append(arguments)
        raise AssertionError("unsafe bind must be reported without invoking Tailscale")

    status = inspect_tailscale(7777, host="0.0.0.0", executable="/usr/bin/tailscale", runner=runner)
    assert status.state == "unsafe_bind"
    assert "0.0.0.0" in status.detail
    assert calls == []


def test_signed_out_tailnet_is_distinct_from_missing_tailscale() -> None:
    runner, calls = runner_for(backend="NeedsLogin")
    status = inspect_tailscale(7777, executable="/usr/bin/tailscale", runner=runner)
    assert status.state == "signed_out"
    assert status.dns_name == "observatory.example.ts.net"
    assert calls == [["/usr/bin/tailscale", "status", "--json"]]


def test_private_serve_proxy_for_this_exact_ssc_port_is_active() -> None:
    runner, calls = runner_for(serve=serve_document())
    status = inspect_tailscale(7777, executable="/usr/bin/tailscale", runner=runner)
    assert status.state == "serving"
    assert status.url == "https://observatory.example.ts.net"
    assert status.funnel_enabled is False
    assert calls[-1] == ["/usr/bin/tailscale", "serve", "status", "--json"]


def test_another_local_service_does_not_claim_ssc_is_remote() -> None:
    runner, _calls = runner_for(serve=serve_document(port=3000))
    status = inspect_tailscale(7777, executable="/usr/bin/tailscale", runner=runner)
    assert status.state == "ready"
    assert status.url is None


def test_funnel_on_the_ssc_handler_is_a_loud_public_exposure() -> None:
    runner, _calls = runner_for(serve=serve_document(funnel=True))
    status = inspect_tailscale(7777, executable="/usr/bin/tailscale", runner=runner)
    assert status.state == "public_exposure"
    assert status.funnel_enabled is True
    assert "Funnel" in status.detail


@pytest.mark.parametrize("public_first", [False, True])
def test_public_ssc_route_is_reported_even_with_another_private_route(public_first: bool) -> None:
    private = serve_document()["Web"]
    public = {
        "public.example.ts.net:443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:7777"}},
        }
    }
    routes = {**public, **private} if public_first else {**private, **public}
    runner, _calls = runner_for(serve={
        "Web": routes,
        "AllowFunnel": {"public.example.ts.net:443": True},
    })

    status = inspect_tailscale(7777, executable="/usr/bin/tailscale", runner=runner)

    assert status.state == "public_exposure"
    assert status.funnel_enabled is True
    assert status.url == "https://public.example.ts.net"
