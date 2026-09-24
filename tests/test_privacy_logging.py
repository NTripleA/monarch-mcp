"""Financial data and credentials must never reach logs, error text, or diagnostics."""

import json
import logging
import pickle
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import server

# Obviously-fake values standing in for sensitive data.
MERCHANT = "Corner Deli"
TXN_ID = "txn_123456"
ACCOUNT_ID = "acc_987654"
CATEGORY_ID = "cat_555111"
AMOUNT = -12.34
NOTES = "private memo about a doctor visit"
SEARCH = "pharmacy refill"
COOKIE_HEADER = "session_id=sessSECRET123; csrftoken=csrfSECRET456; cf_clearance=cfSECRET789"
TOKEN = "tokSECRETabcdef123456"
EMAIL = "someone@example.com"

SENSITIVE_STRINGS = [MERCHANT, TXN_ID, ACCOUNT_ID, CATEGORY_ID, "12.34", NOTES, SEARCH]
CREDENTIAL_STRINGS = ["sessSECRET123", "csrfSECRET456", "cfSECRET789", TOKEN, EMAIL]


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def assert_absent(text: str, needles: list[str]) -> None:
    leaked = [needle for needle in needles if needle in text]
    assert not leaked, f"leaked: {leaked}"


def all_log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records) + caplog.text


class TestToolLoggingCarriesNoFinancialData:
    @pytest.mark.asyncio
    async def test_write_arguments_never_logged(self, logs, mock_api: AsyncMock) -> None:
        mock_api.return_value = {"createTransaction": {"transaction": {"id": TXN_ID, "amount": AMOUNT}}}
        await server.create_transaction(
            amount=AMOUNT,
            merchant_name=MERCHANT,
            account_id=ACCOUNT_ID,
            date="2024-01-15",
            category_id=CATEGORY_ID,
            notes=NOTES,
        )
        await server.update_transaction(transaction_id=TXN_ID, notes=NOTES, amount=AMOUNT, category_id=CATEGORY_ID)
        await server.update_transactions_bulk(
            json.dumps([{"transaction_id": TXN_ID, "merchant_name": MERCHANT, "amount": AMOUNT}])
        )
        await server.set_budget_amount(category_id=CATEGORY_ID, amount=AMOUNT)
        assert_absent(all_log_text(logs), SENSITIVE_STRINGS)

    @pytest.mark.asyncio
    async def test_read_arguments_and_results_never_logged(self, logs, mock_api: AsyncMock) -> None:
        txn = {"id": TXN_ID, "amount": AMOUNT, "merchant": {"name": MERCHANT}, "notes": NOTES}
        mock_api.return_value = {"allTransactions": {"results": [txn]}}
        await server.search_transactions(query=SEARCH, account_id=ACCOUNT_ID, category_id=CATEGORY_ID)
        await server.get_transactions(account_id=ACCOUNT_ID)
        mock_api.return_value = {"accounts": [{"id": ACCOUNT_ID, "displayBalance": AMOUNT}]}
        await server.get_accounts()
        text = all_log_text(logs)
        assert_absent(text, SENSITIVE_STRINGS)
        assert '"result_count": 1' in text  # coarse counts are still reported

    @pytest.mark.asyncio
    async def test_errors_log_only_type_and_category(self, logs, mock_api: AsyncMock) -> None:
        mock_api.side_effect = Exception(f"GraphQL error: merchant {MERCHANT} amount {AMOUNT} for {TXN_ID}")
        with pytest.raises(Exception, match="GraphQL error"):
            await server.get_transaction_splits(transaction_id=TXN_ID)
        text = all_log_text(logs)
        assert_absent(text, SENSITIVE_STRINGS)
        assert '"error_type": "Exception"' in text

    @pytest.mark.asyncio
    async def test_usage_history_keeps_no_arguments(self, mock_api: AsyncMock) -> None:
        server.usage_patterns.clear()
        mock_api.return_value = {"allTransactions": {"results": []}}
        await server.search_transactions(query=SEARCH)
        entry = server.usage_patterns["search_transactions"][0]
        assert set(entry) == {"session_id", "tool_name", "timestamp", "status", "execution_time", "result_size"}
        assert_absent(repr(entry), [SEARCH])

    def test_usage_history_is_bounded(self) -> None:
        server.usage_patterns.clear()
        for _ in range(server.USAGE_HISTORY_PER_TOOL + 50):
            server._record_usage("get_accounts", "success", 0.01, 10)
        assert len(server.usage_patterns["get_accounts"]) == server.USAGE_HISTORY_PER_TOOL


class TestRedactionBackstop:
    def test_financial_keys_redacted_even_for_numbers(self) -> None:
        event = server.redact_sensitive(
            None, "info", {"event": "x", "amount": AMOUNT, "transaction_id": TXN_ID, "merchant": MERCHANT}
        )
        assert event["amount"] == event["transaction_id"] == event["merchant"] == "<redacted>"

    def test_credentials_scrubbed_from_free_text(self) -> None:
        text = server.scrub_text(f"failed with Cookie: {COOKIE_HEADER} and Authorization: Token {TOKEN} for {EMAIL}")
        assert_absent(text, CREDENTIAL_STRINGS)

    def test_bare_cookie_pairs_scrubbed(self) -> None:
        assert_absent(server.scrub_text(f"bad cookies {COOKIE_HEADER}"), CREDENTIAL_STRINGS)

    def test_third_party_records_lose_tracebacks_and_messages(self, logs) -> None:
        library = logging.getLogger("mcp.server.fastmcp.server")
        try:
            raise RuntimeError(f"upstream said {MERCHANT} {TXN_ID} {COOKIE_HEADER}")
        except RuntimeError:
            library.exception(f"Error reading resource accounts://{ACCOUNT_ID}/holdings")
        library.warning(f"odd header {COOKIE_HEADER}")
        # The sanitizer runs on the stream handler; check what it would emit.
        sanitizer = server.ThirdPartyLogSanitizer()
        for record in logs.records:
            sanitizer.filter(record)
            rendered = logging.Formatter().format(record)
            assert_absent(rendered, SENSITIVE_STRINGS + CREDENTIAL_STRINGS + [ACCOUNT_ID])


class TestCredentialsNeverSurface:
    @pytest.mark.asyncio
    async def test_tool_error_text_to_client_is_scrubbed(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = Exception(f"rejected request with Cookie: {COOKIE_HEADER} Token {TOKEN}")
        with pytest.raises(ToolError) as info:
            await server.mcp.call_tool("get_accounts", {})
        assert_absent(str(info.value), CREDENTIAL_STRINGS)

    @pytest.mark.asyncio
    async def test_cookie_auth_failure_message_has_no_cookie_values(self, logs, monkeypatch) -> None:
        client = server.MonarchMoney()
        server.mm_client = client
        monkeypatch.setattr(
            client, "login_with_cookies", AsyncMock(side_effect=Exception(f"server echoed {COOKIE_HEADER}"))
        )
        with pytest.raises(ValueError) as info:
            await server.authenticate_with_cookies(COOKIE_HEADER)
        assert_absent(str(info.value), CREDENTIAL_STRINGS)
        assert_absent(all_log_text(logs), CREDENTIAL_STRINGS)

    @pytest.mark.asyncio
    async def test_session_values_never_logged_on_load(self, logs) -> None:
        server.session_file.write_bytes(
            pickle.dumps({"token": TOKEN, "auth_mode": "cookie", "cookies": server_cookie_dict()})
        )
        server.session_file.chmod(0o600)
        server.mm_client = None
        server.auth_state = server.AuthState.NOT_INITIALIZED
        server.RUNTIME.transport = "http"
        await server.ensure_authenticated()
        assert_absent(all_log_text(logs), CREDENTIAL_STRINGS)


def server_cookie_dict() -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in COOKIE_HEADER.split("; "))


class TestAuthStatusIsSafe:
    @pytest.mark.asyncio
    async def test_status_exposes_no_secrets_or_paths(self) -> None:
        server.session_file.write_bytes(pickle.dumps({"token": TOKEN, "auth_mode": "token"}))
        server.session_file.chmod(0o600)
        server.RUNTIME.transport = "http"
        server.RUNTIME.writes_enabled = False

        result = await server.monarch_auth_status()
        payload = result.model_dump_json()

        assert_absent(payload, CREDENTIAL_STRINGS + [str(server.session_dir), "session.pickle"])
        assert result.session_file_present is True
        assert result.session_file_permissions_ok is True
        assert result.writes_enabled is False
        assert result.transport == "http"
        assert result.credential_mode == "session_only"
        assert result.verified is None
        assert set(result.model_dump()) == {
            "authenticated",
            "auth_state",
            "session_file_present",
            "session_file_permissions_ok",
            "session_age_seconds",
            "session_modified_at",
            "credential_mode",
            "writes_enabled",
            "transport",
            "verified",
            "last_failure_category",
            "hint",
        }

    @pytest.mark.asyncio
    async def test_status_without_session_points_at_reprovisioning(self) -> None:
        server.RUNTIME.transport = "http"
        server.mm_client = None
        server.auth_state = server.AuthState.NOT_INITIALIZED
        result = await server.monarch_auth_status()
        assert result.session_file_present is False
        assert result.authenticated is False
        assert result.hint is not None and "provision" in result.hint

    @pytest.mark.asyncio
    async def test_verify_discards_the_payload(self, mock_api: AsyncMock) -> None:
        mock_api.return_value = {"subscription": {"id": "sub_1", "paymentSource": EMAIL}}
        result = await server.monarch_auth_status(verify=True)
        assert result.verified is True
        assert_absent(result.model_dump_json(), [EMAIL, "sub_1"])

    @pytest.mark.asyncio
    async def test_verify_failure_reports_only_a_category(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = Exception(f"401 Unauthorized for {EMAIL}")
        result = await server.monarch_auth_status(verify=True)
        assert result.verified is False
        assert result.last_failure_category == "auth"
        assert_absent(result.model_dump_json(), [EMAIL])
