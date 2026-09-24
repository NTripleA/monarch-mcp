"""Streamable HTTP transport: protocol flow, concurrency, Host/Origin checks, health,
body limits, and cleanup. Uses in-process ASGI (plus one real-socket smoke test) and
mocks for every Monarch call.
"""

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn

import http_app
import server

PUBLIC_HOST = "monarch.example.test"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "Host": PUBLIC_HOST,
}


def make_settings(**overrides: Any) -> http_app.HttpSettings:
    values: dict[str, Any] = {"public_hosts": (PUBLIC_HOST,), "max_body_bytes": 64 * 1024}
    values.update(overrides)
    return http_app.HttpSettings(**values)


@contextlib.asynccontextmanager
async def running_app(settings: http_app.HttpSettings | None = None) -> AsyncIterator[httpx.AsyncClient]:
    app = http_app.create_http_app(server.mcp._mcp_server, settings or make_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{PUBLIC_HOST}") as client:
            client.app = app  # type: ignore[attr-defined]
            yield client


def rpc(method: str, request_id: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


INITIALIZE_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test-client", "version": "1.0"},
}


def parse_response(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    events = [line[len("data: ") :] for line in response.text.splitlines() if line.startswith("data: ")]
    assert events, response.text
    return json.loads(events[-1])


async def call(client: httpx.AsyncClient, message: dict[str, Any]) -> dict[str, Any]:
    return parse_response(await client.post("/mcp", json=message, headers=MCP_HEADERS))


class TestProtocolFlow:
    @pytest.mark.asyncio
    async def test_initialize_list_and_call(self, mock_api: AsyncMock) -> None:
        server.configure_runtime("http")
        mock_api.return_value = {"categories": [{"id": "cat_1", "name": "Groceries"}]}

        async with running_app() as client:
            init = await call(client, rpc("initialize", 1, INITIALIZE_PARAMS))
            assert init["result"]["serverInfo"]["name"] == "monarch-money"
            assert "mcp-session-id" not in {k.lower() for k in init}  # stateless

            listed = await call(client, rpc("tools/list", 2))
            names = {tool["name"] for tool in listed["result"]["tools"]}
            assert "monarch_auth_status" in names
            assert names.isdisjoint(server.WRITE_TOOLS)
            assert "authenticate_browser_session" not in names

            result = await call(client, rpc("tools/call", 3, {"name": "get_transaction_categories", "arguments": {}}))
            assert result["result"]["isError"] is False
            assert result["result"]["structuredContent"]["count"] == 1

            status = await call(client, rpc("tools/call", 4, {"name": "monarch_auth_status", "arguments": {}}))
            assert status["result"]["structuredContent"]["transport"] == "http"
            assert status["result"]["structuredContent"]["writes_enabled"] is False

    @pytest.mark.asyncio
    async def test_cached_write_and_browser_auth_calls_are_rejected(self, mock_api: AsyncMock) -> None:
        server.configure_runtime("http")
        async with running_app() as client:
            write = await call(
                client,
                rpc(
                    "tools/call", 1, {"name": "update_transaction", "arguments": {"transaction_id": "t", "notes": "n"}}
                ),
            )
            assert write["result"]["isError"] is True
            assert "MONARCH_ENABLE_WRITES=false" in write["result"]["content"][0]["text"]

            browser = await call(
                client, rpc("tools/call", 2, {"name": "authenticate_browser_session", "arguments": {}})
            )
            assert browser["result"]["isError"] is True
            assert "stdio" in browser["result"]["content"][0]["text"]
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_and_delete_are_not_served(self) -> None:
        async with running_app() as client:
            assert (await client.get("/mcp", headers=MCP_HEADERS)).status_code == 405
            assert (await client.delete("/mcp", headers=MCP_HEADERS)).status_code == 405


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_two_concurrent_clients(self, mock_api: AsyncMock) -> None:
        server.configure_runtime("http")
        categories = [{"id": "cat_1", "name": "Groceries", "group": {"name": "Food"}}]

        async def slow_api(method: str, *args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.01)
            return {"categories": categories}

        mock_api.side_effect = slow_api

        async def session(client: httpx.AsyncClient, verbose: bool, base_id: int) -> list[dict[str, Any]]:
            responses = [await call(client, rpc("initialize", base_id, INITIALIZE_PARAMS))]
            for i in range(3):
                responses.append(await call(client, rpc("tools/list", base_id + 10 + i)))
                responses.append(
                    await call(
                        client,
                        rpc(
                            "tools/call",
                            base_id + 20 + i,
                            {"name": "get_transaction_categories", "arguments": {"verbose": verbose}},
                        ),
                    )
                )
            return responses

        async with running_app() as client_a, running_app() as client_b:
            compact, verbose = await asyncio.gather(session(client_a, False, 100), session(client_b, True, 200))

        for responses, expect_verbose, base in ((compact, False, 100), (verbose, True, 200)):
            assert all(base <= r["id"] < base + 100 for r in responses), "a response crossed clients"
            calls = [r for r in responses if "structuredContent" in r.get("result", {})]
            assert len(calls) == 3
            for r in calls:
                assert r["result"]["structuredContent"]["verbose"] is expect_verbose

    @pytest.mark.asyncio
    async def test_stateless_requests_leave_nothing_behind(self, mock_api: AsyncMock) -> None:
        mock_api.return_value = {"categories": []}
        async with running_app() as client:
            manager = client.app.state.session_manager  # type: ignore[attr-defined]
            # Warm-up: the first SSE response starts sse_starlette's one-per-process
            # shutdown watcher, which is not per-request state.
            await call(client, rpc("tools/list", 0))
            await asyncio.sleep(0.05)
            baseline = len(asyncio.all_tasks())
            for i in range(1, 11):
                await call(client, rpc("tools/list", i))
            await asyncio.sleep(0.05)
            assert manager._server_instances == {}
            assert len(asyncio.all_tasks()) <= baseline


class TestHostAndOrigin:
    @pytest.mark.asyncio
    async def test_unknown_host_rejected(self) -> None:
        async with running_app() as client:
            response = await client.post(
                "/mcp", json=rpc("tools/list", 1), headers={**MCP_HEADERS, "Host": "evil.example"}
            )
            assert response.status_code == 421

    @pytest.mark.asyncio
    async def test_foreign_origin_rejected(self) -> None:
        async with running_app() as client:
            response = await client.post(
                "/mcp", json=rpc("tools/list", 1), headers={**MCP_HEADERS, "Origin": "https://evil.example"}
            )
            assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_public_origin_and_loopback_host_allowed(self) -> None:
        async with running_app() as client:
            ok = await client.post(
                "/mcp", json=rpc("tools/list", 1), headers={**MCP_HEADERS, "Origin": f"https://{PUBLIC_HOST}"}
            )
            assert ok.status_code == 200
            local = await client.post(
                "/mcp", json=rpc("tools/list", 2), headers={**MCP_HEADERS, "Host": "127.0.0.1:8004"}
            )
            assert local.status_code == 200

    @pytest.mark.asyncio
    async def test_public_host_rejected_when_not_configured(self) -> None:
        async with running_app(make_settings(public_hosts=())) as client:
            response = await client.post("/mcp", json=rpc("tools/list", 1), headers=MCP_HEADERS)
            assert response.status_code == 421

    def test_settings_come_from_environment(self) -> None:
        settings = http_app.HttpSettings.from_env(
            {
                "MONARCH_HTTP_HOST": "0.0.0.0",
                "MONARCH_HTTP_PORT": "8000",
                "MONARCH_ALLOWED_HOSTS": "monarch.example.test, other.example.test",
                "MONARCH_ALLOWED_ORIGINS": "https://extra.example.test",
            }
        )
        assert settings.host == "0.0.0.0"
        assert settings.public_hosts == ("monarch.example.test", "other.example.test")
        security = http_app.build_security_settings(settings)
        assert security.enable_dns_rebinding_protection is True
        assert "https://monarch.example.test" in security.allowed_origins
        assert "https://extra.example.test" in security.allowed_origins

    def test_generic_http_defaults_to_loopback(self) -> None:
        settings = http_app.HttpSettings.from_env({})
        assert settings.host == "127.0.0.1"
        assert settings.port == 8000
        assert settings.public_hosts == ()


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthz_is_minimal_and_independent_of_auth(self, monkeypatch) -> None:
        server.configure_runtime("http")
        server.auth_state = server.AuthState.FAILED
        guard = AsyncMock(side_effect=AssertionError("health must not touch auth"))
        monkeypatch.setattr(server, "ensure_authenticated", guard)

        async with running_app() as client:
            response = await client.get("/healthz", headers={"Host": "anything.example"})

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response.content == b'{"status":"ok"}'
        assert "server" not in response.headers
        guard.assert_not_called()


class TestBodyLimits:
    @pytest.mark.asyncio
    async def test_declared_oversized_body_rejected(self) -> None:
        async with running_app(make_settings(max_body_bytes=1024)) as client:
            big = rpc("tools/list", 1, {"padding": "x" * 4096})
            response = await client.post("/mcp", json=big, headers=MCP_HEADERS)
            assert response.status_code == 413

    @pytest.mark.asyncio
    async def test_chunked_oversized_body_rejected(self) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(8):
                yield b"x" * 512

        async with running_app(make_settings(max_body_bytes=1024)) as client:
            response = await client.post("/mcp", content=chunks(), headers=MCP_HEADERS)
            assert "content-length" not in {k.lower() for k in response.request.headers}
            assert response.status_code == 413

    @pytest.mark.asyncio
    async def test_body_within_limit_passes(self) -> None:
        async with running_app(make_settings(max_body_bytes=1024)) as client:
            response = await client.post("/mcp", json=rpc("tools/list", 1), headers=MCP_HEADERS)
            assert response.status_code == 200


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestRealSocket:
    @pytest.mark.asyncio
    async def test_sdk_client_over_real_http(self, mock_api: AsyncMock) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        server.configure_runtime("http")
        mock_api.return_value = {"categories": [{"id": "cat_1", "name": "Groceries"}]}
        port = free_port()
        app = http_app.create_http_app(server.mcp._mcp_server, make_settings(port=port))
        uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
        serve_task = asyncio.create_task(uv.serve())
        try:
            for _ in range(100):
                if uv.started:
                    break
                await asyncio.sleep(0.02)

            async def use(verbose: bool) -> Any:
                async with (
                    streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("get_transaction_categories", {"verbose": verbose})
                    return tools, result

            (tools_a, result_a), (tools_b, result_b) = await asyncio.gather(use(False), use(True))
            assert {t.name for t in tools_a.tools} == {t.name for t in tools_b.tools}
            assert result_a.structuredContent["verbose"] is False
            assert result_b.structuredContent["verbose"] is True
        finally:
            uv.should_exit = True
            await asyncio.wait_for(serve_task, timeout=10)
