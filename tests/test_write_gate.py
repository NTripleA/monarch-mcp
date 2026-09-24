"""Write kill switch, local-only tools, annotations, and write-safety validation."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from mcp.server.fastmcp.exceptions import ToolError

import server

ALL_TOOLS = {tool.name for tool in server.mcp._tool_manager.list_tools()}

# Minimal valid arguments for calling each write tool through the MCP dispatcher.
WRITE_ARGS: dict[str, dict[str, Any]] = {
    "create_transaction": {
        "amount": -12.34,
        "merchant_name": "Corner Deli",
        "account_id": "acc_1",
        "date": "2024-01-15",
        "category_id": "cat_1",
    },
    "update_transaction": {"transaction_id": "txn_1", "notes": "memo"},
    "update_transactions_bulk": {"updates": json.dumps([{"transaction_id": "txn_1", "notes": "memo"}])},
    "update_transaction_splits": {"transaction_id": "txn_1", "splits": [{"amount": -1.0}]},
    "set_budget_amount": {"category_id": "cat_1", "amount": 100.0},
    "create_manual_account": {"account_name": "Savings", "account_type": "depository", "account_sub_type": "savings"},
    "refresh_accounts": {},
}


async def listed_tools() -> set[str]:
    return {tool.name for tool in await server.mcp.list_tools()}


class TestWriteToolInventory:
    def test_write_tools_are_all_registered(self) -> None:
        assert server.WRITE_TOOLS <= ALL_TOOLS
        assert server.LOCAL_ONLY_TOOLS <= ALL_TOOLS

    def test_every_non_readonly_tool_is_gated(self) -> None:
        """Adding a mutating tool without adding it to the gate must fail this test."""
        for tool in server.mcp._tool_manager.list_tools():
            annotations = tool.annotations
            assert annotations is not None, f"{tool.name} has no annotations"
            if annotations.readOnlyHint is not True:
                assert tool.name in server.WRITE_TOOLS | server.LOCAL_ONLY_TOOLS, tool.name
            else:
                assert tool.name not in server.WRITE_TOOLS, tool.name

    def test_every_write_tool_has_an_in_function_guard(self) -> None:
        import inspect

        for name in server.WRITE_TOOLS | server.LOCAL_ONLY_TOOLS:
            source = inspect.getsource(getattr(server, name))
            assert f'require_tool_enabled("{name}")' in source, name

    def test_write_args_cover_the_gate(self) -> None:
        assert set(WRITE_ARGS) == server.WRITE_TOOLS

    @pytest.mark.parametrize(
        "name",
        ["update_transaction", "update_transactions_bulk", "update_transaction_splits", "set_budget_amount"],
    )
    def test_record_replacing_tools_are_destructive(self, name: str) -> None:
        annotations = server.mcp._tool_manager.get_tool(name).annotations
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is True

    @pytest.mark.parametrize("name", ["create_transaction", "create_manual_account"])
    def test_create_only_tools_are_not_destructive(self, name: str) -> None:
        annotations = server.mcp._tool_manager.get_tool(name).annotations
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is False


class TestTransportDefaults:
    def test_http_defaults_to_writes_off(self) -> None:
        server.configure_runtime("http")
        assert server.RUNTIME.writes_enabled is False

    def test_stdio_defaults_to_writes_on(self) -> None:
        server.configure_runtime("stdio")
        assert server.RUNTIME.writes_enabled is True

    @pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("off", False), ("FALSE", False)])
    def test_explicit_setting_wins(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
        monkeypatch.setenv("MONARCH_ENABLE_WRITES", value)
        server.configure_runtime("http")
        assert server.RUNTIME.writes_enabled is expected
        server.configure_runtime("stdio")
        assert server.RUNTIME.writes_enabled is expected

    def test_unrecognised_value_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONARCH_ENABLE_WRITES", "maybe")
        with pytest.raises(ValueError, match="MONARCH_ENABLE_WRITES"):
            server.configure_runtime("http")

    @pytest.mark.parametrize("value", ["0", "101", "abc"])
    def test_bulk_limit_is_validated(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("MONARCH_MAX_BULK_UPDATES", value)
        with pytest.raises(ValueError, match="MONARCH_MAX_BULK_UPDATES"):
            server.configure_runtime("http")


class TestToolListing:
    @pytest.mark.asyncio
    async def test_stdio_default_lists_everything(self) -> None:
        assert await listed_tools() == ALL_TOOLS

    @pytest.mark.asyncio
    async def test_writes_disabled_hides_every_write_tool(self) -> None:
        server.RUNTIME.writes_enabled = False
        listed = await listed_tools()
        assert listed.isdisjoint(server.WRITE_TOOLS)
        assert listed == ALL_TOOLS - server.WRITE_TOOLS
        assert "monarch_auth_status" in listed
        assert "get_transactions" in listed

    @pytest.mark.asyncio
    async def test_http_hides_browser_auth_even_with_writes_on(self) -> None:
        server.RUNTIME.transport = "http"
        server.RUNTIME.writes_enabled = True
        listed = await listed_tools()
        assert "authenticate_browser_session" not in listed
        assert listed >= server.WRITE_TOOLS

    @pytest.mark.asyncio
    async def test_http_default_inventory(self) -> None:
        server.configure_runtime("http")
        listed = await listed_tools()
        assert listed == ALL_TOOLS - server.WRITE_TOOLS - server.LOCAL_ONLY_TOOLS
        assert len(listed) == 15


class TestDispatchGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(server.WRITE_TOOLS))
    async def test_cached_direct_write_call_is_rejected(self, name: str, mock_api: AsyncMock) -> None:
        server.RUNTIME.writes_enabled = False
        with pytest.raises(ToolError, match="MONARCH_ENABLE_WRITES=false"):
            await server.mcp.call_tool(name, WRITE_ARGS[name])
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(server.WRITE_TOOLS))
    async def test_in_function_guard_also_blocks(self, name: str, mock_api: AsyncMock) -> None:
        server.RUNTIME.writes_enabled = False
        with pytest.raises(server.ToolDisabledError):
            await getattr(server, name)(**WRITE_ARGS[name])
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_browser_auth_is_blocked_over_http(self) -> None:
        server.RUNTIME.transport = "http"
        server.RUNTIME.writes_enabled = True
        with pytest.raises(ToolError, match="only available when the server runs locally over stdio"):
            await server.mcp.call_tool("authenticate_browser_session", {})
        with pytest.raises(server.ToolDisabledError):
            await server.authenticate_browser_session()

    @pytest.mark.asyncio
    async def test_browser_auth_never_starts_capture_server_over_http(self, monkeypatch) -> None:
        server.RUNTIME.transport = "http"
        started = MagicMock()
        monkeypatch.setattr(server.browser_auth, "CookieCaptureServer", started)
        with pytest.raises(ToolError):
            await server.mcp.call_tool("authenticate_browser_session", {})
        started.assert_not_called()

    @pytest.mark.asyncio
    async def test_reads_still_work_with_writes_disabled(self, mock_api: AsyncMock) -> None:
        server.RUNTIME.transport = "http"
        server.RUNTIME.writes_enabled = False
        mock_api.return_value = {"accounts": [{"id": "acc_1"}]}
        result = await server.mcp.call_tool("get_accounts", {})
        assert isinstance(result, tuple) or result

    @pytest.mark.asyncio
    async def test_unknown_write_argument_is_rejected_before_any_call(self, mock_api: AsyncMock) -> None:
        with pytest.raises(ToolError, match="unknown argument"):
            await server.mcp.call_tool("update_transaction", {"transaction_id": "txn_1", "categoryId": "cat_2"})
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_update_is_rejected(self, mock_api: AsyncMock) -> None:
        with pytest.raises(ValueError, match="No fields to update"):
            await server.update_transaction(transaction_id="txn_1")
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_split_leg_unknown_field_is_rejected(self, mock_api: AsyncMock) -> None:
        with pytest.raises(ToolError):
            await server.mcp.call_tool(
                "update_transaction_splits", {"transaction_id": "txn_1", "splits": [{"amount": -1, "cat": "x"}]}
            )
        mock_api.assert_not_called()


class TestBulkSafety:
    @pytest.mark.asyncio
    async def test_oversized_batch_rejected_before_any_mutation(self, mock_api: AsyncMock) -> None:
        server.RUNTIME.max_bulk_updates = 25
        batch = [{"transaction_id": f"txn_{i}", "notes": "n"} for i in range(26)]
        with pytest.raises(ValueError, match="limit is 25"):
            await server.update_transactions_bulk(json.dumps(batch))
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_at_the_limit_is_accepted(self, mock_api: AsyncMock) -> None:
        server.RUNTIME.max_bulk_updates = 3
        batch = [{"transaction_id": f"txn_{i}", "notes": "n"} for i in range(3)]
        result = await server.update_transactions_bulk(json.dumps(batch))
        assert result.summary.succeeded == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("bad_item", "fragment"),
        [
            ({"transaction_id": "txn_x", "categoryId": "cat"}, "Extra inputs are not permitted"),
            ({"transaction_id": "txn_x", "hide_from_reports": "false"}, "hide_from_reports"),
            ({"transaction_id": "txn_x", "amount": True}, "amount must be a number"),
            ({"transaction_id": "txn_x", "amount": "12.00"}, "amount must be a number"),
            ({"transaction_id": "txn_x", "date": "01/15/2024"}, "YYYY-MM-DD"),
            ({"transaction_id": 123, "notes": "n"}, "transaction_id"),
            ({"transaction_id": "txn_x"}, "no fields to update"),
            ("not-an-object", "must be an object"),
        ],
    )
    async def test_one_invalid_item_rejects_the_whole_batch(
        self, mock_api: AsyncMock, bad_item: object, fragment: str
    ) -> None:
        batch = [{"transaction_id": "txn_ok", "notes": "fine"}, bad_item]
        with pytest.raises(ValueError, match="nothing was changed") as info:
            await server.update_transactions_bulk(json.dumps(batch))
        assert fragment in str(info.value)
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_ids_rejected(self, mock_api: AsyncMock) -> None:
        batch = [{"transaction_id": "txn_1", "notes": "a"}, {"transaction_id": "txn_1", "notes": "b"}]
        with pytest.raises(ValueError, match="repeated"):
            await server.update_transactions_bulk(json.dumps(batch))
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_auth_failure_skips_not_yet_started_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "BULK_UPDATE_CONCURRENCY", 1)
        monkeypatch.setattr(server, "ensure_authenticated", AsyncMock())
        api = AsyncMock(side_effect=server.SessionUnavailableError("session expired"))
        monkeypatch.setattr(server, "api_call_with_retry", api)

        batch = [{"transaction_id": f"txn_{i}", "notes": "n"} for i in range(4)]
        result = await server.update_transactions_bulk(json.dumps(batch))

        assert api.await_count == 1
        assert result.summary.failed == 1
        assert result.summary.skipped == 3


class TestWriteRetrySafety:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            asyncio.TimeoutError(),
            aiohttp.ServerDisconnectedError(),
            server.TransportServerError("bad gateway", 502),
        ],
    )
    async def test_ambiguous_create_failure_is_not_retried(self, mock_api: AsyncMock, error: Exception) -> None:
        mock_api.side_effect = error
        with pytest.raises(server.WriteOutcomeUnknownError, match="may already have been created") as info:
            await server.create_transaction(**WRITE_ARGS["create_transaction"])
        assert "Do NOT retry blindly" in str(info.value)
        assert mock_api.await_count == 1

    @pytest.mark.asyncio
    async def test_ambiguous_update_failure_says_reread(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = asyncio.TimeoutError()
        with pytest.raises(server.WriteOutcomeUnknownError, match="Re-read the record"):
            await server.update_transaction(transaction_id="txn_1", notes="memo")
        assert mock_api.await_count == 1

    @pytest.mark.asyncio
    async def test_writes_allow_at_most_one_auth_retry(self, mock_api: AsyncMock) -> None:
        mock_api.return_value = {"createTransaction": {}}
        await server.create_transaction(**WRITE_ARGS["create_transaction"])
        assert mock_api.await_args.kwargs["max_retries"] == 1

    @pytest.mark.asyncio
    async def test_write_auth_rejection_retries_once_with_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONARCH_EMAIL", "user@example.com")
        monkeypatch.setenv("MONARCH_PASSWORD", "pw")
        method = AsyncMock(side_effect=[Exception("401 Unauthorized"), Exception("401 Unauthorized")])
        server.mm_client = MagicMock(create_transaction=method)
        monkeypatch.setattr(server, "clear_session", MagicMock())
        monkeypatch.setattr(server, "ensure_authenticated", AsyncMock())
        monkeypatch.setattr(server.asyncio, "sleep", AsyncMock())

        with pytest.raises(Exception, match="401"):
            await server.create_transaction(**WRITE_ARGS["create_transaction"])
        assert method.await_count == 2

    def test_validation_errors_are_not_ambiguous(self) -> None:
        assert not server.is_ambiguous_write_failure(ValueError("bad input"))
        assert not server.is_ambiguous_write_failure(server.TransportServerError("forbidden", 403))
        assert server.is_ambiguous_write_failure(server.TransportServerError("oops", 500))

    def test_amount_like_numbers_are_not_auth_errors(self) -> None:
        assert not server.is_auth_error(Exception("amount 1403.50 exceeds limit"))
        assert server.is_auth_error(Exception("403 Forbidden"))
        assert server.is_auth_error(server.TransportServerError("x", 401))
        assert not server.is_auth_error(server.TransportServerError("Unauthorized-looking text", 500))
