"""Regression coverage for correctness fixes ported without the upstream SDK migration."""

import json
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest
from dateutil.relativedelta import relativedelta
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import JsonValue

import server

CATEGORIES: dict[str, JsonValue] = {
    "categories": [
        {"id": "cat_expense", "group": {"type": "expense"}},
        {"id": "cat_transfer", "group": {"type": "transfer"}},
        {"id": "cat_income", "group": {"type": "income"}},
    ]
}


def transaction(amount: float, category: str = "cat_expense", day: str = "2024-01-15") -> dict[str, JsonValue]:
    return {
        "id": "txn_example",
        "date": day,
        "amount": amount,
        "category": {"id": category, "name": category},
        "account": {"id": "acct_example", "displayName": "Example account"},
    }


def mock_reads(mock_api: AsyncMock, transactions: list[dict[str, JsonValue]], total: int | None = None) -> None:
    async def respond(method: str, **kwargs: object) -> object:
        if method == "get_transaction_categories":
            return CATEGORIES
        if method == "get_transactions":
            offset = int(str(kwargs.get("offset", 0)))
            limit = int(str(kwargs.get("limit", 1000)))
            page: dict[str, object] = {"results": transactions[offset : offset + limit]}
            if total is not None:
                page["totalCount"] = total
            return {"allTransactions": page}
        if method in ("get_accounts", "get_budgets", "get_cashflow"):
            return []
        raise AssertionError(f"Unexpected mocked method: {method}")

    mock_api.side_effect = respond


class TestCalendarPeriods:
    @pytest.mark.parametrize(
        ("value", "first", "last"),
        [
            ("2024-02", "2024-02-01", "2024-02-29"),
            ("2023-02", "2023-02-01", "2023-02-28"),
            ("January 2024", "2024-01-01", "2024-01-31"),
            ("2024", "2024-01-01", "2024-12-31"),
        ],
    )
    def test_partial_dates_cover_the_calendar_period(self, value: str, first: str, last: str) -> None:
        assert server.build_date_filter(value, value) == {"start_date": first, "end_date": last}
        assert server.build_date_filter(None, value) == {"start_date": first, "end_date": last}

    def test_previous_month_across_year_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class JanuaryDate(date):
            @classmethod
            def today(cls) -> date:
                return cls(2025, 1, 15)

        monkeypatch.setattr(server, "date", JanuaryDate)
        assert server.build_date_filter("last month", "last month") == {
            "start_date": "2024-12-01",
            "end_date": "2024-12-31",
        }
        assert server.build_date_filter(None, "last year") == {"start_date": "2024-01-01", "end_date": "2024-12-31"}
        assert server.build_date_filter("last month", None)["end_date"] == "2025-01-15"

    async def test_summary_reports_resolved_period(self, mock_api: AsyncMock) -> None:
        mock_reads(mock_api, [])
        result = await server.get_spending_summary("2024-02", "2024-02")
        assert (result.period.start, result.period.end) == ("2024-02-01", "2024-02-29")


class TestSpendingPages:
    @pytest.mark.parametrize("total", [None, 1001])
    async def test_every_page_contributes_to_totals(self, mock_api: AsyncMock, total: int | None) -> None:
        mock_reads(mock_api, [transaction(-1.0) for _ in range(1001)], total)
        result = await server.get_spending_summary("2024-01-01", "2024-01-31")
        assert result.totals.expenses == 1001
        assert result.groups["cat_expense"].count == 1001
        pages = [call for call in mock_api.await_args_list if call.args[0] == "get_transactions"]
        assert [call.kwargs["offset"] for call in pages] == [0, 1000]

    @pytest.mark.parametrize("total", [None, 1005])
    async def test_empty_page_stops_when_total_is_missing_or_stale(
        self, mock_api: AsyncMock, total: int | None
    ) -> None:
        mock_reads(mock_api, [transaction(-1.0) for _ in range(1000)], total)
        result = await server.get_spending_summary()
        assert result.totals.expenses == 1000
        assert mock_api.await_count == 3  # categories, full page, empty page

    async def test_category_failure_does_not_return_unfiltered_summary(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = ValueError("Synthetic category failure")
        with pytest.raises(ValueError, match="category failure"):
            await server.get_spending_summary()


class TestTransfersAndForecasts:
    @pytest.mark.parametrize("tool", ["summary", "overview", "patterns"])
    async def test_transfers_are_excluded_from_income_and_spending(self, mock_api: AsyncMock, tool: str) -> None:
        transactions = [
            transaction(-5.0),
            transaction(10.0, "cat_income"),
            transaction(-100.0, "cat_transfer"),
            transaction(100.0, "cat_transfer"),
        ]
        mock_reads(mock_api, transactions, len(transactions))
        if tool == "summary":
            summary = await server.get_spending_summary()
            assert (summary.totals.expenses, summary.totals.income, summary.transfers_excluded) == (5, 10, 2)
            assert "cat_transfer" not in summary.groups
        elif tool == "overview":
            overview = await server.get_complete_financial_overview()
            assert isinstance(overview.transaction_summary, dict)
            assert overview.transaction_summary["total_expenses"] == 5
            assert overview.transaction_summary["total_income"] == 10
            assert overview.transaction_summary["transfers_excluded"] == 2
            assert isinstance(overview.transactions, list) and len(overview.transactions) == 4
        else:
            patterns = await server.analyze_spending_patterns(include_forecasting=False)
            assert patterns.monthly_trends == {
                "2024-01": {"expenses": 5, "income": 10, "net": 5, "transaction_count": 2}
            }
            assert isinstance(patterns.metadata, dict) and patterns.metadata["transfers_excluded"] == 2

    async def test_partial_analysis_reports_category_failure(self, mock_api: AsyncMock) -> None:
        async def respond(method: str, **kwargs: object) -> object:
            if method == "get_transaction_categories":
                raise ValueError("Synthetic category failure")
            return [transaction(-5.0)] if method == "get_transactions" else []

        mock_api.side_effect = respond
        overview = await server.get_complete_financial_overview()
        assert isinstance(overview.categories, dict) and "error" in overview.categories
        assert isinstance(overview.transaction_summary, dict)
        assert overview.transaction_summary["transfers_excluded"] is None
        patterns = await server.analyze_spending_patterns()
        assert "categories" in patterns.errors
        assert isinstance(patterns.metadata, dict) and patterns.metadata["transfers_excluded"] is None

    @pytest.mark.parametrize(("lookback", "total", "expected"), [(6, 5, 200), (3, 5, 150), (6, 5000, 200)])
    async def test_forecast_uses_recent_complete_months(
        self, mock_api: AsyncMock, monkeypatch: pytest.MonkeyPatch, lookback: int, total: int, expected: int
    ) -> None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return cls(2026, 6, 15)

        monkeypatch.setattr(server, "datetime", FixedDateTime)
        transactions = [
            transaction(-1000, day="2026-06-01"),
            transaction(-100, day="2026-05-01"),
            transaction(-200, day="2026-04-01"),
            transaction(-300, day="2026-03-20"),
            transaction(-4000, day="2026-02-01"),
        ]
        mock_reads(mock_api, transactions, total)
        result = await server.analyze_spending_patterns(lookback_months=lookback)
        assert isinstance(result.forecast, dict) and result.forecast["predicted_expenses"] == expected
        for call in mock_api.await_args_list:
            if call.args[0] in ("get_transactions", "get_budgets"):
                assert call.kwargs["start_date"] == (date(2026, 6, 15) - relativedelta(months=lookback)).isoformat()
                assert call.kwargs["end_date"] == "2026-06-15"

    async def test_refund_does_not_dilute_expense_average(self, mock_api: AsyncMock) -> None:
        mock_reads(mock_api, [transaction(-100), transaction(-50), transaction(30)], 3)
        result = await server.analyze_spending_patterns(include_forecasting=False)
        assert isinstance(result.category_analysis, dict)
        category = result.category_analysis["cat_expense"]
        assert isinstance(category, dict) and category["avg_amount"] == 75

    async def test_invalid_lookback_fails_before_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        auth = AsyncMock()
        monkeypatch.setattr(server, "ensure_authenticated", auth)
        with pytest.raises(ValueError, match="lookback_months"):
            await server.analyze_spending_patterns(lookback_months=0)
        auth.assert_not_awaited()


WRITE_CASES: list[tuple[str, dict[str, JsonValue], str, str]] = [
    (
        "create_transaction",
        {
            "amount": -1,
            "merchant_name": "Example Merchant",
            "account_id": "acct_example",
            "date": "2024-01-15",
            "category_id": "cat_expense",
        },
        "createTransaction",
        "transaction",
    ),
    (
        "update_transaction",
        {"transaction_id": "txn_example", "notes": "Example note"},
        "updateTransaction",
        "transaction",
    ),
    (
        "update_transaction_splits",
        {"transaction_id": "txn_example", "splits": []},
        "updateTransactionSplit",
        "transaction",
    ),
    ("set_budget_amount", {"category_id": "cat_expense", "amount": 0}, "updateOrCreateBudgetItem", "budgetItem"),
    (
        "create_manual_account",
        {"account_name": "Example Account", "account_type": "depository", "account_sub_type": "checking", "balance": 0},
        "createManualAccount",
        "account",
    ),
]


class TestMutationResponses:
    @pytest.mark.parametrize(("tool", "arguments", "key", "record"), WRITE_CASES)
    @pytest.mark.parametrize(
        "failure", ["missing_payload", "missing_record", "null_record", "empty_record", "rejected"]
    )
    async def test_rejected_or_malformed_writes_fail(
        self, mock_api: AsyncMock, tool: str, arguments: dict[str, JsonValue], key: str, record: str, failure: str
    ) -> None:
        payloads: dict[str, object] = {
            "missing_payload": {},
            "missing_record": {key: {}},
            "null_record": {key: {record: None}},
            "empty_record": {key: {record: {}}},
            "rejected": {key: {record: {"id": "record_example"}, "errors": [{"message": "Synthetic rejection"}]}},
        }
        mock_api.return_value = payloads[failure]
        with pytest.raises(ToolError):
            await server.mcp.call_tool(tool, arguments)
        assert mock_api.await_count == 1

    @pytest.mark.parametrize(("tool", "arguments", "key", "record"), WRITE_CASES)
    @pytest.mark.parametrize("errors", [None, [], {"message": None, "code": None, "fieldErrors": []}])
    async def test_valid_mutations_with_null_errors_succeed(
        self, mock_api: AsyncMock, tool: str, arguments: dict[str, JsonValue], key: str, record: str, errors: JsonValue
    ) -> None:
        mock_api.return_value = {
            key: {
                record: {"id": "record_example", "hasSplitTransactions": False, "splitTransactions": []},
                "errors": errors,
            }
        }
        await server.mcp.call_tool(tool, arguments)
        assert mock_api.await_count == 1

    async def test_bulk_rejections_are_not_counted_as_success(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = [
            {"updateTransaction": {"transaction": None, "errors": [{"message": "Synthetic rejection"}]}},
            {"updateTransaction": {"transaction": {"id": "txn_second"}, "errors": None}},
        ]
        result = await server.update_transactions_bulk(
            json.dumps(
                [
                    {"transaction_id": "txn_first", "notes": "Example"},
                    {"transaction_id": "txn_second", "notes": "Example"},
                ]
            )
        )
        assert (result.summary.succeeded, result.summary.failed) == (1, 1)


class TestInvalidAmounts:
    @pytest.mark.parametrize(("tool", "arguments", "key", "record"), WRITE_CASES)
    @pytest.mark.parametrize("amount", [float("nan"), float("inf"), float("-inf")])
    async def test_nonfinite_amounts_never_reach_auth_or_api(
        self,
        mock_api: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
        arguments: dict[str, JsonValue],
        key: str,
        record: str,
        amount: float,
    ) -> None:
        auth = AsyncMock()
        monkeypatch.setattr(server, "ensure_authenticated", auth)
        arguments = dict(arguments)
        if tool == "update_transaction_splits":
            arguments["splits"] = [{"amount": amount}]
        else:
            arguments["balance" if tool == "create_manual_account" else "amount"] = amount
        with pytest.raises(ToolError):
            await server.mcp.call_tool(tool, arguments)
        mock_api.assert_not_awaited()
        auth.assert_not_awaited()

    @pytest.mark.parametrize("amount", [0, float("nan"), float("inf"), float("-inf")])
    async def test_bad_bulk_amount_rejects_whole_batch(self, mock_api: AsyncMock, amount: float) -> None:
        updates = [
            {"transaction_id": "txn_first", "notes": "Example"},
            {"transaction_id": "txn_second", "amount": amount},
        ]
        with pytest.raises(ValueError):
            await server.update_transactions_bulk(json.dumps(updates))
        mock_api.assert_not_awaited()

    async def test_zero_update_fails_before_authentication(self, monkeypatch: pytest.MonkeyPatch) -> None:
        auth = AsyncMock()
        monkeypatch.setattr(server, "ensure_authenticated", auth)
        with pytest.raises(ValueError, match="amount cannot be 0"):
            await server.update_transaction(transaction_id="txn_example", amount=0)
        auth.assert_not_awaited()
