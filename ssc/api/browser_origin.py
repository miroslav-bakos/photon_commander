# SPDX-License-Identifier: GPL-3.0-only
"""Reject foreign browser controls independently of optional session pairing."""

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.datastructures import URL, Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


class TransportProxyMiddleware:
    """Retain the socket peer before trusted-loopback forwarded-header handling."""
    def __init__(self, app: ASGIApp) -> None:
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        self.app = ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1", "::1"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = dict(scope)
            scope["ssc.transport_peer"] = scope.get("client")
        await self.app(scope, receive, send)


class BrowserOriginMiddleware:
    """Native clients omit Origin; browsers must use the served origin.

    The request scheme comes from ASGI (including the server's trusted proxy
    handling), never from an arbitrary forwarded header inside this middleware.
    CORS protects response reads; this guard protects actual side effects.
    """

    def __init__(self, app: ASGIApp, *, allowed_origins: list[str],
                 private_proxy_origin: Callable[[], Awaitable[str | None]] | None = None) -> None:
        self.app = app
        self.allowed = {_origin(value) for value in allowed_origins} - {None}
        self.private_proxy_origin = private_proxy_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        guarded = scope["type"] == "websocket" or (
            scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        )
        if scope["type"] in {"http", "websocket"}:
            headers = Headers(scope=scope)
            url = URL(scope=scope)
            scheme = {"ws": "http", "wss": "https"}.get(url.scheme, url.scheme)
            served = _origin(f"{scheme}://{url.netloc}")
            hosts = headers.getlist("host")
            request_allowed = set(self.allowed)
            # Serve is verified by local CLI, never by arbitrary forwarded headers.
            # Inspect only proxy requests; ordinary LAN startup has no CLI overhead.
            peer = scope.get("ssc.transport_peer", scope.get("client"))
            proxied = any(headers.get(name) for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "tailscale-user-login"))
            if peer and peer[0] in {"127.0.0.1", "::1"} and proxied and self.private_proxy_origin:
                remote = await self.private_proxy_origin()
                if remote:
                    parsed = urlsplit(remote)
                    verified = _origin(f"{parsed.scheme}://{parsed.netloc}")
                    if verified:
                        request_allowed.add(verified)
                        # Some private proxies rewrite Host to the configured
                        # loopback authority while preserving HTTPS scheme.
                        authorities = {(value[1], value[2]) for value in self.allowed}
                        if served and served[0] == verified[0] and (served[1], served[2]) in authorities:
                            request_allowed.add(served)
            if len(hosts) != 1 or served not in request_allowed:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008,
                                "reason": "service host is not allowed"})
                else:
                    await JSONResponse({"detail": "service host is not allowed"}, status_code=403)(scope, receive, send)
                return
        if guarded:
            headers = Headers(scope=scope)
            origins = headers.getlist("origin")
            if origins:
                url = URL(scope=scope)
                scheme = {"ws": "http", "wss": "https"}.get(url.scheme, url.scheme)
                served = _origin(f"{scheme}://{url.netloc}")
                origin = _origin(origins[0])
                if (
                    len(origins) != 1
                    or origin is None
                    or origin not in request_allowed
                ):
                    if scope["type"] == "websocket":
                        await send(
                            {
                                "type": "websocket.close",
                                "code": 1008,
                                "reason": "browser origin is not allowed",
                            }
                        )
                    else:
                        await JSONResponse(
                            {"detail": "browser origin is not allowed"}, status_code=403
                        )(scope, receive, send)
                    return
        await self.app(scope, receive, send)
