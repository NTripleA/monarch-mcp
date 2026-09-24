"""Streamable HTTP transport for remote deployments.

The app serves two routes:

- ``/mcp``: MCP Streamable HTTP in stateless mode. One StreamableHTTPSessionManager
  per app, started once by the app lifespan; in stateless mode it creates a fresh
  transport for every request and tears it down when the response completes, so no
  per-client state outlives a request.
- ``GET /healthz``: returns only ``{"status": "ok"}``. It never touches Monarch or the
  session, so the endpoint stays healthy while the Monarch session is expired.

Host/Origin validation (DNS-rebinding protection) is always on. Public hostnames come
from the environment, never from code. Request bodies are capped before they reach
the MCP layer.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_MAX_BODY_BYTES = 1024 * 1024

# Loopback requests are always accepted: a DNS-rebinding attack makes a browser send
# the attacker's hostname, never a loopback one.
LOOPBACK_HOSTS = ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*")
LOOPBACK_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer") from e
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class HttpSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    public_hosts: tuple[str, ...] = ()
    extra_origins: tuple[str, ...] = ()
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    json_response: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> HttpSettings:
        json_response = env.get("MONARCH_HTTP_JSON_RESPONSE", "").strip().lower()
        if json_response not in ("", "true", "false"):
            raise ValueError("MONARCH_HTTP_JSON_RESPONSE must be true or false")
        return cls(
            host=env.get("MONARCH_HTTP_HOST", "").strip() or DEFAULT_HOST,
            port=_positive_int(env, "MONARCH_HTTP_PORT", DEFAULT_PORT),
            public_hosts=_csv(env.get("MONARCH_ALLOWED_HOSTS")),
            extra_origins=_csv(env.get("MONARCH_ALLOWED_ORIGINS")),
            max_body_bytes=_positive_int(env, "MONARCH_HTTP_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES),
            json_response=json_response == "true",
        )


def build_security_settings(settings: HttpSettings) -> TransportSecuritySettings:
    """DNS-rebinding protection: loopback plus the configured public hostnames only."""
    hosts = list(LOOPBACK_HOSTS)
    origins = list(LOOPBACK_ORIGINS)
    for host in settings.public_hosts:
        hosts += [host, f"{host}:*"]
        origins.append(f"https://{host}")
    origins += settings.extra_origins
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


class BodyLimitMiddleware:
    """Reject request bodies over a byte limit before the MCP layer parses them.

    Checks Content-Length up front, and also buffers (up to the limit) and counts
    chunked bodies that declare no length, then replays the buffered body downstream.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                await PlainTextResponse("Invalid Content-Length", status_code=400)(scope, receive, send)
                return
            if length > self.max_body_bytes:
                await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
                return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body_bytes:
                await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class _MCPEndpoint:
    """ASGI endpoint handing /mcp requests to the app's single session manager.

    Only POST is served. A stateless server never pushes server-initiated messages, so
    a GET SSE stream would only hold an idle connection open through the tunnel, and
    there is no session to DELETE. The spec allows 405 for both.
    """

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] != "POST":
            response = PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "POST"})
            await response(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_http_app(server: Server, settings: HttpSettings) -> Starlette:
    """Build the ASGI app. Each call owns exactly one session manager, run once by its lifespan."""
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=settings.json_response,
        security_settings=build_security_settings(settings),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[
            Route(MCP_PATH, endpoint=_MCPEndpoint(manager)),
            Route(HEALTH_PATH, endpoint=healthz, methods=["GET"]),
        ],
        middleware=[Middleware(BodyLimitMiddleware, max_body_bytes=settings.max_body_bytes)],
        lifespan=lifespan,
    )
    app.state.session_manager = manager
    return app


async def serve(server: Server, settings: HttpSettings) -> None:
    config = uvicorn.Config(
        create_http_app(server, settings),
        host=settings.host,
        port=settings.port,
        # Leave logging to the root handler, which strips data from library records.
        log_config=None,
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        timeout_graceful_shutdown=10,
    )
    await uvicorn.Server(config).serve()
