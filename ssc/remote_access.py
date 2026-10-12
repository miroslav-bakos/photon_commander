# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Read-only Tailscale Serve inspection for private SSC remote access.

SSC deliberately does not install Tailscale, authenticate a tailnet, change
its access policy, or run Serve/Funnel. This module only asks the local CLI
for status and reduces its large JSON documents to the few non-secret facts
the Settings screen needs. Telescope control remains bound to loopback;
Tailscale Serve is the HTTPS reverse proxy in front of it.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

RemoteAccessState = Literal[
    "not_installed",
    "daemon_unavailable",
    "signed_out",
    "ready",
    "serving",
    "public_exposure",
    "unsafe_bind",
]


@dataclass(frozen=True, slots=True)
class RemoteAccessStatus:
    state: RemoteAccessState
    detail: str
    detected_path: str | None
    dns_name: str | None
    url: str | None
    funnel_enabled: bool
    setup_command: str
    status_command: str = "tailscale serve status"
    disable_command: str = "sudo tailscale serve off"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]


def _run(arguments: Sequence[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            list(arguments), check=False, capture_output=True, text=True, timeout=3.0
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(returncode=1, stdout="", stderr=str(exc))
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _json_document(result: CommandResult) -> dict[str, Any] | None:
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _proxy_is_ssc(value: object, port: int) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value if "://" in value else f"http://{value}")
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.port == port
    )


def _web_matches_ssc(document: dict[str, Any], port: int) -> tuple[str | None, bool]:
    web = document.get("Web")
    if not isinstance(web, dict):
        return None, False
    private_url: str | None = None
    for host_port, raw_server in web.items():
        if not isinstance(host_port, str) or not isinstance(raw_server, dict):
            continue
        handlers = raw_server.get("Handlers")
        if not isinstance(handlers, dict):
            continue
        for path, raw_handler in handlers.items():
            if not isinstance(raw_handler, dict) or not _proxy_is_ssc(
                raw_handler.get("Proxy"), port
            ):
                continue
            clean_host = host_port.removesuffix(":443").rstrip(".")
            clean_path = path if isinstance(path, str) and path.startswith("/") else "/"
            url = f"https://{clean_host}{'' if clean_path == '/' else clean_path}"
            # Current Serve JSON keeps this map at the document root. Accept
            # the older/nested forms too so an SSC upgrade cannot accidentally
            # describe a public route as private after a Tailscale upgrade.
            raw_funnel = document.get("AllowFunnel")
            funnel = (
                (isinstance(raw_funnel, dict) and bool(raw_funnel.get(host_port)))
                or bool(raw_server.get("AllowFunnel"))
                or bool(raw_handler.get("AllowFunnel"))
            )
            if funnel:
                return url, True
            if private_url is None:
                private_url = url
    return private_url, False


def inspect_tailscale(
    port: int,
    *,
    host: str = "127.0.0.1",
    executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = _run,
) -> RemoteAccessStatus:
    """Return a bounded, non-mutating view of Tailscale and Serve state."""

    setup = f"sudo tailscale serve --bg 127.0.0.1:{port}"
    try:
        loopback_bind = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback_bind = host == "localhost"
    if not loopback_bind:
        return RemoteAccessStatus(
            "unsafe_bind",
            f"Photon Commander is listening on {host}, not loopback. Restrict Photon Commander to 127.0.0.1 before relying on Tailscale Serve for private access.",
            None,
            None,
            None,
            False,
            setup,
        )

    resolved = executable if executable is not None else which("tailscale")
    if resolved is None:
        return RemoteAccessStatus(
            "not_installed",
            "Tailscale is not installed on the computer running Photon Commander.",
            None,
            None,
            None,
            False,
            setup,
        )

    status = _json_document(runner([resolved, "status", "--json"]))
    if status is None:
        return RemoteAccessStatus(
            "daemon_unavailable",
            "The Tailscale CLI is installed, but its local service did not return status.",
            resolved,
            None,
            None,
            False,
            setup,
        )

    backend = status.get("BackendState")
    raw_self = status.get("Self")
    raw_dns = raw_self.get("DNSName") if isinstance(raw_self, dict) else None
    dns_name = raw_dns.rstrip(".") if isinstance(raw_dns, str) and raw_dns.strip() else None
    if backend != "Running":
        return RemoteAccessStatus(
            "signed_out",
            "Tailscale is installed but this computer is not connected to a tailnet.",
            resolved,
            dns_name,
            None,
            False,
            setup,
        )

    serve = _json_document(runner([resolved, "serve", "status", "--json"]))
    if serve is None:
        return RemoteAccessStatus(
            "ready",
            "Tailscale is connected. Configure private Serve access for Photon Commander with the command below.",
            resolved,
            dns_name,
            None,
            False,
            setup,
        )

    url, funnel = _web_matches_ssc(serve, port)
    if url is not None and funnel:
        return RemoteAccessStatus(
            "public_exposure",
            "Photon Commander is exposed through Tailscale Funnel. Turn it off; routine telescope control must remain tailnet-private.",
            resolved,
            dns_name,
            url,
            True,
            setup,
        )
    if url is not None:
        return RemoteAccessStatus(
            "serving",
            "Private HTTPS remote access is active inside your tailnet.",
            resolved,
            dns_name,
            url,
            False,
            setup,
        )
    return RemoteAccessStatus(
        "ready",
        "Tailscale is connected, but Serve is not currently routing to this Photon Commander port.",
        resolved,
        dns_name,
        None,
        False,
        setup,
    )


__all__ = ["CommandResult", "RemoteAccessState", "RemoteAccessStatus", "inspect_tailscale"]
