"""Tests for the transaction rule tools: get_transaction_rules, preview_transaction_rule,
and create_transaction_rule.

Uses the ``mock_api`` fixture (see conftest.py) so tests are offline: it patches
``ensure_authenticated`` and ``api_call_with_retry``. Every rule call goes through
``api_call_with_retry("gql_call", operation=..., graphql_query=..., variables=...)``.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from pydantic import ValidationError

import server

LIST_OP = "GetTransactionRules"
PREVIEW_OP = "Common_PreviewTransactionRule"
CREATE_OP = "Common_CreateTransactionRuleMutationV2"

DELI = server.RuleTextCriterion(operator="contains", value="Corner Deli")

STORED_RULE: dict[str, Any] = {
    "id": "rule_001",
    "order": 0,
    "merchantCriteriaUseOriginalStatement": False,
    "merchantCriteria": None,
    "merchantNameCriteria": [{"operator": "contains", "value": "Corner Deli"}],
    "originalStatementCriteria": None,
    "amountCriteria": None,
    "categoryIds": None,
    "accountIds": None,
    "setCategoryAction": {"id": "cat_001", "name": "Groceries", "icon": "x"},
    "recentApplicationCount": 0,
    "lastAppliedAt": None,
}

# A rule made in the Monarch app, with criteria and actions this server can't create.
RICH_RULE: dict[str, Any] = {
    "id": "rule_002",
    "order": 1,
    "originalStatementCriteria": [{"operator": "eq", "value": "SQ *CORNER DELI"}],
    "criteriaOwnerUserIds": ["user_001"],
    "addTagsAction": [{"id": "tag_001", "name": "Work", "color": "#000"}],
    "linkGoalAction": {"id": "goal_001", "name": "Trip"},
    "splitTransactionsAction": {
        "amountType": "percentage",
        "splitsInfo": [{"categoryId": "cat_001", "amount": 50}, {"categoryId": "cat_002", "amount": 50}],
    },
}


def gql_dispatch(
    by_operation: dict[str, Any], calls: list[tuple[str, dict[str, Any]]] | None = None
) -> Callable[..., Any]:
    """api_call_with_retry side_effect keyed by GraphQL operation name.

    A value that is an exception is raised; a list is consumed one item per call.
    """
    remaining = {op: list(value) if isinstance(value, list) else value for op, value in by_operation.items()}

    def _side_effect(method_name: str, *args: Any, **kwargs: Any) -> Any:
        assert method_name == "gql_call"
        operation = kwargs["operation"]
        if calls is not None:
            calls.append((operation, kwargs["variables"]))
        value = remaining[operation]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    return _side_effect


def created(rule_id: str | None = "rule_001", errors: Any = None) -> dict[str, Any]:
    return {
        "createTransactionRuleV2": {
            "transactionRule": {"id": rule_id} if rule_id is not None else None,
            "errors": errors,
        }
    }


def rules_list(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"transactionRules": list(rules)}


def sent_input(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    [variables] = [v for op, v in calls if op == CREATE_OP]
    return variables["input"]


# ---------------------------------------------------------------------------
# get_transaction_rules
# ---------------------------------------------------------------------------


class TestGetTransactionRules:
    async def test_lists_rules_via_gql_call(self, mock_api: AsyncMock) -> None:
        mock_api.return_value = rules_list(STORED_RULE, RICH_RULE)
        result = await server.get_transaction_rules()
        assert result.count == 2
        assert [r["id"] for r in result.rules] == ["rule_001", "rule_002"]
        mock_api.assert_awaited_once()
        assert mock_api.await_args.args == ("gql_call",)
        assert mock_api.await_args.kwargs["operation"] == LIST_OP
        assert mock_api.await_args.kwargs["variables"] == {}

    async def test_rich_rules_are_preserved_not_truncated(self, mock_api: AsyncMock) -> None:
        mock_api.return_value = rules_list(RICH_RULE)
        result = await server.get_transaction_rules()
        assert result.rules == [RICH_RULE]

    async def test_query_requests_full_rule_shape(self) -> None:
        from graphql import print_ast

        query = print_ast(server.GET_TRANSACTION_RULES.document)
        for field in (
            "merchantNameCriteria",
            "originalStatementCriteria",
            "criteriaOwnerUserIds",
            "addTagsAction",
            "linkSavingsGoalAction",
            "splitTransactionsAction",
            "lastAppliedAt",
        ):
            assert field in query

    @pytest.mark.parametrize(
        "response",
        [
            {"somethingElse": []},
            {"transactionRules": None},
            {"transactionRules": {"id": "rule_001"}},
            {"transactionRules": "rule_001"},
            [STORED_RULE],
            None,
        ],
    )
    async def test_malformed_response_raises_instead_of_reporting_no_rules(
        self, mock_api: AsyncMock, response: Any
    ) -> None:
        # get_transaction_rules is how callers check an ambiguous create; "0 rules" would invite a duplicate.
        mock_api.return_value = response
        with pytest.raises(ValueError, match="invalid transaction-rules response"):
            await server.get_transaction_rules()

    async def test_empty_rule_list_is_valid(self, mock_api: AsyncMock) -> None:
        mock_api.return_value = rules_list()
        result = await server.get_transaction_rules()
        assert result.rules == []
        assert result.count == 0


# ---------------------------------------------------------------------------
# Input building, shared by preview and create
# ---------------------------------------------------------------------------


def build(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "merchant_criteria": None,
        "original_statement_criteria": None,
        "amount_criteria": None,
        "account_ids": None,
        "category_ids": None,
        "set_category_id": None,
        "set_merchant_name": None,
        "hide_from_reports": None,
    }
    args.update(overrides)
    return server.build_rule_input(**args)


class TestBuildRuleInput:
    @pytest.mark.parametrize("operator", ["contains", "eq"])
    def test_merchant_criteria_use_merchant_name_field(self, operator: str) -> None:
        rule = build(
            merchant_criteria=[server.RuleTextCriterion(operator=operator, value="Corner Deli")],
            set_category_id="cat_001",
        )
        assert rule == {
            "merchantNameCriteria": [{"operator": operator, "value": "Corner Deli"}],
            "setCategoryAction": "cat_001",
        }
        assert "merchantCriteria" not in rule

    @pytest.mark.parametrize("operator", ["contains", "eq"])
    def test_statement_criteria(self, operator: str) -> None:
        rule = build(
            original_statement_criteria=[server.RuleTextCriterion(operator=operator, value="SQ *CORNER DELI")],
            set_category_id="cat_001",
        )
        assert rule["originalStatementCriteria"] == [{"operator": operator, "value": "SQ *CORNER DELI"}]

    def test_operator_defaults_to_contains(self) -> None:
        assert server.RuleTextCriterion(value="Corner Deli").operator == "contains"

    def test_between_amount_sends_value_range_and_null_value(self) -> None:
        rule = build(
            amount_criteria=server.RuleAmountCriterion(operator="between", lower=10, upper=50),
            set_category_id="cat_001",
        )
        assert rule["amountCriteria"] == {
            "operator": "between",
            "isExpense": True,
            "value": None,
            "valueRange": {"lower": 10, "upper": 50},
        }

    @pytest.mark.parametrize("operator", ["gt", "lt", "eq"])
    def test_single_value_amount_sends_null_range(self, operator: str) -> None:
        rule = build(
            amount_criteria=server.RuleAmountCriterion(operator=operator, value=20, is_expense=False),
            set_category_id="cat_001",
        )
        assert rule["amountCriteria"] == {"operator": operator, "isExpense": False, "value": 20, "valueRange": None}

    def test_filters_are_included_when_given(self) -> None:
        rule = build(
            merchant_criteria=[DELI], account_ids=["acc_001"], category_ids=["cat_002"], set_category_id="cat_001"
        )
        assert rule["accountIds"] == ["acc_001"]
        assert rule["categoryIds"] == ["cat_002"]

    def test_unset_and_empty_lists_are_omitted(self) -> None:
        rule = build(
            merchant_criteria=[DELI],
            original_statement_criteria=[],
            account_ids=[],
            category_ids=[],
            set_category_id="cat_001",
        )
        assert rule == {
            "merchantNameCriteria": [{"operator": "contains", "value": "Corner Deli"}],
            "setCategoryAction": "cat_001",
        }

    def test_hide_from_reports_false_is_a_real_action(self) -> None:
        rule = build(merchant_criteria=[DELI], hide_from_reports=False)
        assert rule["setHideFromReportsAction"] is False

    def test_merchant_rename_is_sent_as_a_name(self) -> None:
        rule = build(merchant_criteria=[DELI], set_merchant_name="Corner Deli")
        assert rule["setMerchantAction"] == "Corner Deli"


class TestRuleValidation:
    def test_requires_a_criterion(self) -> None:
        with pytest.raises(ValueError, match="needs a merchant, original-statement, or amount criterion"):
            build(set_category_id="cat_001")

    def test_empty_primary_criteria_count_as_absent(self) -> None:
        with pytest.raises(ValueError, match="needs a merchant"):
            build(merchant_criteria=[], original_statement_criteria=[], set_category_id="cat_001")

    def test_account_and_category_filters_alone_are_not_enough(self) -> None:
        with pytest.raises(ValueError, match="not enough on their own"):
            build(account_ids=["acc_001"], category_ids=["cat_002"], set_category_id="cat_001")

    def test_requires_an_action(self) -> None:
        with pytest.raises(ValueError, match="at least one action"):
            build(merchant_criteria=[DELI])

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_category_action_is_rejected(self, blank: str) -> None:
        with pytest.raises(ValueError, match="set_category_id must not be blank"):
            build(merchant_criteria=[DELI], set_category_id=blank)

    def test_blank_merchant_rename_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="set_merchant_name must not be blank"):
            build(merchant_criteria=[DELI], set_merchant_name=" ")

    @pytest.mark.parametrize("field", ["account_ids", "category_ids"])
    def test_blank_ids_inside_lists_are_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must not contain blank IDs"):
            build(merchant_criteria=[DELI], set_category_id="cat_001", **{field: ["id_001", " "]})

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_blank_criterion_value_is_rejected(self, blank: str) -> None:
        with pytest.raises(ValidationError):
            server.RuleTextCriterion(value=blank)

    def test_unknown_criterion_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            server.RuleTextCriterion.model_validate({"value": "Corner Deli", "case_sensitive": True})

    def test_unknown_operator_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            server.RuleTextCriterion.model_validate({"operator": "startswith", "value": "Corner"})

    @pytest.mark.parametrize(
        "fields",
        [
            {"operator": "gt", "value": -5},
            {"operator": "between", "lower": -1, "upper": 5},
            {"operator": "between", "lower": 1},
            {"operator": "between", "upper": 5},
            {"operator": "between", "lower": 1, "upper": 5, "value": 3},
            {"operator": "eq"},
            {"operator": "lt", "value": 5, "lower": 1},
            {"operator": "gt", "value": 5, "upper": 9},
            {"operator": "between", "lower": 50, "upper": 10},
            {"operator": "eq", "value": 5, "direction": "out"},
            {"operator": "eq", "value": float("inf")},
            {"operator": "eq", "value": 5, "is_expense": "false"},
            {"operator": "eq", "value": True},
            {"operator": "eq", "value": False},
            {"operator": "gt", "value": "20"},
            {"operator": "between", "lower": "1", "upper": 5},
            {"operator": "between", "lower": 1, "upper": "5"},
            {"operator": "between", "lower": False, "upper": 5},
            {"operator": "between", "lower": 1, "upper": True},
        ],
    )
    def test_invalid_amount_criteria(self, fields: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            server.RuleAmountCriterion.model_validate(fields)

    @pytest.mark.parametrize(
        "fields",
        [
            {"operator": "eq", "value": 20},
            {"operator": "gt", "value": 20.5},
            {"operator": "lt", "value": 0},
            {"operator": "between", "lower": 10, "upper": 50},
        ],
    )
    def test_real_numbers_are_valid_amounts(self, fields: dict[str, Any]) -> None:
        server.RuleAmountCriterion.model_validate(fields)

    @pytest.mark.parametrize("value", ["true", 1, 0, "false"])
    def test_non_strict_hide_from_reports_is_rejected(self, value: Any) -> None:
        with pytest.raises(ValueError, match="hide_from_reports must be true or false"):
            build(merchant_criteria=[DELI], hide_from_reports=value)


# ---------------------------------------------------------------------------
# preview_transaction_rule
# ---------------------------------------------------------------------------


class TestPreviewTransactionRule:
    async def test_sends_rule_and_offset(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch(
            {PREVIEW_OP: {"transactionRulePreview": {"totalCount": 0, "results": []}}}, calls
        )
        await server.preview_transaction_rule(merchant_criteria=[DELI], set_category_id="cat_001", offset=30)
        assert calls == [
            (
                PREVIEW_OP,
                {
                    "rule": {
                        "merchantNameCriteria": [{"operator": "contains", "value": "Corner Deli"}],
                        "setCategoryAction": "cat_001",
                    },
                    "offset": 30,
                },
            )
        ]

    async def test_returns_matches_and_total(self, mock_api: AsyncMock) -> None:
        match = {
            "newName": None,
            "newCategory": {"id": "cat_001", "name": "Groceries"},
            "transaction": {"id": "txn_123", "date": "2024-01-15", "amount": -12.5},
        }
        mock_api.side_effect = gql_dispatch(
            {PREVIEW_OP: {"transactionRulePreview": {"totalCount": 42, "results": [match]}}}
        )
        result = await server.preview_transaction_rule(merchant_criteria=[DELI], set_category_id="cat_001")
        assert result.total_count == 42
        assert result.matches == [match]

    async def test_preview_never_sends_apply_to_existing(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch(
            {PREVIEW_OP: {"transactionRulePreview": {"totalCount": 0, "results": []}}}, calls
        )
        await server.preview_transaction_rule(merchant_criteria=[DELI], set_category_id="cat_001")
        assert "applyToExistingTransactions" not in calls[0][1]["rule"]

    async def test_negative_offset_is_rejected_before_any_call(self, mock_api: AsyncMock) -> None:
        with pytest.raises(ValueError, match="offset"):
            await server.preview_transaction_rule(merchant_criteria=[DELI], set_category_id="cat_001", offset=-1)
        mock_api.assert_not_called()

    async def test_invalid_rule_is_rejected_before_any_call(self, mock_api: AsyncMock) -> None:
        with pytest.raises(ValueError, match="at least one action"):
            await server.preview_transaction_rule(merchant_criteria=[DELI])
        mock_api.assert_not_called()

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"transactionRulePreview": None},
            {"transactionRulePreview": {}},
            {"transactionRulePreview": {"totalCount": 0}},
            {"transactionRulePreview": {"totalCount": 0, "results": None}},
            {"transactionRulePreview": {"totalCount": 0, "results": {"id": "txn_123"}}},
            {"transactionRulePreview": {"results": []}},
            {"transactionRulePreview": {"totalCount": None, "results": []}},
            {"transactionRulePreview": {"totalCount": "0", "results": []}},
            {"transactionRulePreview": {"totalCount": 0.0, "results": []}},
            {"transactionRulePreview": {"totalCount": False, "results": []}},
        ],
    )
    async def test_malformed_response_is_an_error(self, mock_api: AsyncMock, response: dict[str, Any]) -> None:
        # Preview is the safety check before creating; it must not fabricate a zero-match result.
        mock_api.side_effect = gql_dispatch({PREVIEW_OP: response})
        with pytest.raises(ValueError, match="invalid rule preview response"):
            await server.preview_transaction_rule(merchant_criteria=[DELI], set_category_id="cat_001")

    async def test_total_count_is_not_derived_from_results(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({PREVIEW_OP: {"transactionRulePreview": {"totalCount": 42, "results": []}}})
        result = await server.preview_transaction_rule(merchant_criteria=[DELI], set_category_id="cat_001", offset=60)
        assert result.total_count == 42
        assert result.matches == []


# ---------------------------------------------------------------------------
# create_transaction_rule
# ---------------------------------------------------------------------------


async def create(**overrides: Any) -> server.CreateRuleResult:
    args: dict[str, Any] = {"merchant_criteria": [DELI], "set_category_id": "cat_001"}
    args.update(overrides)
    return await server.create_transaction_rule(**args)


class TestCreateTransactionRuleInput:
    async def test_sends_exact_mutation_input(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(), LIST_OP: rules_list(STORED_RULE)}, calls)
        await create()
        assert sent_input(calls) == {
            "merchantNameCriteria": [{"operator": "contains", "value": "Corner Deli"}],
            "setCategoryAction": "cat_001",
            "applyToExistingTransactions": False,
        }

    async def test_apply_to_existing_false_is_sent_explicitly_by_default(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(), LIST_OP: rules_list(STORED_RULE)}, calls)
        result = await create()
        assert sent_input(calls)["applyToExistingTransactions"] is False
        assert result.apply_to_existing_requested is False

    async def test_apply_to_existing_true_when_requested(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(), LIST_OP: rules_list(STORED_RULE)}, calls)
        result = await create(apply_to_existing_transactions=True)
        assert sent_input(calls)["applyToExistingTransactions"] is True
        assert result.apply_to_existing_requested is True

    async def test_empty_filters_are_omitted(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(), LIST_OP: rules_list(STORED_RULE)}, calls)
        await create(account_ids=[], category_ids=[], original_statement_criteria=[])
        assert set(sent_input(calls)) == {"merchantNameCriteria", "setCategoryAction", "applyToExistingTransactions"}

    async def test_uses_write_call_with_single_auth_retry(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(), LIST_OP: rules_list(STORED_RULE)})
        await create()
        create_call = mock_api.await_args_list[0]
        assert create_call.kwargs["operation"] == CREATE_OP
        assert create_call.kwargs["max_retries"] == 1

    async def test_invalid_rule_is_rejected_before_any_call(self, mock_api: AsyncMock) -> None:
        with pytest.raises(ValueError, match="needs a merchant"):
            await server.create_transaction_rule(account_ids=["acc_001"], set_category_id="cat_001")
        mock_api.assert_not_called()


class TestCreateTransactionRuleOutcomes:
    async def test_success_with_read_back(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch(
            {CREATE_OP: created("rule_001"), LIST_OP: rules_list(RICH_RULE, STORED_RULE)}
        )
        result = await create()
        assert result.rule_id == "rule_001"
        assert result.rule == STORED_RULE
        assert result.message == "Created rule rule_001."

    @pytest.mark.parametrize(
        "read_error",
        [
            asyncio.TimeoutError(),
            aiohttp.ServerDisconnectedError(),
            server.TransportServerError("bad gateway", 502),
            server.SessionUnavailableError("session rejected"),
        ],
    )
    async def test_read_back_failure_does_not_fail_the_create(self, mock_api: AsyncMock, read_error: Exception) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001"), LIST_OP: read_error})
        result = await create()
        assert result.rule_id == "rule_001"
        assert result.rule is None
        assert result.message.startswith("Created rule rule_001;")
        assert "read-back verification was unavailable" in result.message

    @pytest.mark.parametrize("read_error", [RuntimeError("unexpected"), KeyError("transactionRules"), TypeError("bad")])
    async def test_unexpected_read_back_error_does_not_fail_the_create(
        self, monkeypatch: pytest.MonkeyPatch, mock_api: AsyncMock, read_error: Exception
    ) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001")})
        monkeypatch.setattr(server, "_fetch_transaction_rules", AsyncMock(side_effect=read_error))
        result = await create()
        assert result.rule_id == "rule_001"
        assert result.rule is None
        assert result.message == (
            "Created rule rule_001; read-back verification was unavailable or the rule is not visible yet."
        )

    async def test_read_back_does_not_swallow_cancellation(
        self, monkeypatch: pytest.MonkeyPatch, mock_api: AsyncMock
    ) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001")})
        monkeypatch.setattr(server, "_fetch_transaction_rules", AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await create()

    async def test_mutation_errors_are_not_swallowed_by_the_read_back_boundary(self, mock_api: AsyncMock) -> None:
        # The broad catch covers only the read-back; a failing write still propagates.
        mock_api.side_effect = RuntimeError("upstream API failure")
        with pytest.raises(RuntimeError, match="upstream API failure"):
            await create()

    async def test_malformed_read_back_list_is_still_success(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001"), LIST_OP: {"somethingElse": []}})
        result = await create()
        assert result.rule_id == "rule_001"
        assert result.rule is None
        assert "read-back verification was unavailable" in result.message

    async def test_rule_not_yet_visible_is_still_success(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001"), LIST_OP: rules_list(RICH_RULE)})
        result = await create()
        assert result.rule_id == "rule_001"
        assert result.rule is None
        assert "not visible yet" in result.message

    async def test_error_message_is_a_rejection(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch(
            {CREATE_OP: created(None, {"message": "Rule is invalid", "code": "BAD", "fieldErrors": None})}
        )
        with pytest.raises(ValueError, match="Monarch rejected the rule creation: Rule is invalid"):
            await create()
        assert mock_api.await_count == 1

    async def test_field_errors_are_a_rejection(self, mock_api: AsyncMock) -> None:
        errors = {
            "message": None,
            "code": None,
            "fieldErrors": [{"field": "setCategoryAction", "messages": ["Unknown"]}],
        }
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(None, errors)})
        with pytest.raises(ValueError, match="setCategoryAction: Unknown"):
            await create()

    async def test_all_null_errors_object_is_a_rejection(self, mock_api: AsyncMock) -> None:
        # Monarch's silent refusal: a non-null errors object with nothing in it.
        errors = {"message": None, "code": None, "fieldErrors": None}
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001", errors)})
        with pytest.raises(ValueError, match="Monarch rejected the rule creation: no reason given"):
            await create()

    async def test_errors_list_with_an_entry_is_a_rejection(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(None, [{"message": "Duplicate rule"}])})
        with pytest.raises(ValueError, match="Duplicate rule"):
            await create()

    async def test_empty_errors_list_is_not_a_rejection(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001", []), LIST_OP: rules_list(STORED_RULE)})
        result = await create()
        assert result.rule_id == "rule_001"

    async def test_no_errors_and_no_id_is_an_unknown_outcome(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(None)})
        with pytest.raises(server.WriteOutcomeUnknownError, match="may already have been created") as info:
            await create()
        assert "get_transaction_rules" in str(info.value)
        assert mock_api.await_count == 1

    async def test_unreadable_payload_is_an_unknown_outcome(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = gql_dispatch({CREATE_OP: {"unexpected": True}})
        with pytest.raises(server.WriteOutcomeUnknownError, match="get_transaction_rules"):
            await create()

    @pytest.mark.parametrize(
        "error",
        [asyncio.TimeoutError(), aiohttp.ServerDisconnectedError(), server.TransportServerError("bad gateway", 502)],
    )
    async def test_ambiguous_transport_failure(self, mock_api: AsyncMock, error: Exception) -> None:
        mock_api.side_effect = error
        with pytest.raises(server.WriteOutcomeUnknownError, match="may already have been created") as info:
            await create()
        message = str(info.value)
        assert "Do NOT retry blindly" in message
        assert "get_transaction_rules" in message
        assert "search_transactions" not in message
        assert mock_api.await_count == 1

    async def test_non_ambiguous_failure_propagates(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = server.TransportServerError("bad request", 400)
        with pytest.raises(server.TransportServerError):
            await create()


class TestWriteCallHintIsOptIn:
    async def test_existing_create_callers_keep_their_wording(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = asyncio.TimeoutError()
        with pytest.raises(server.WriteOutcomeUnknownError) as info:
            await server.write_call("create_transaction", creates=True, description="The transaction")
        assert str(info.value) == (
            "The transaction may already have been created: the request to Monarch failed (TimeoutError) "
            "after it was sent. Do NOT retry blindly -- first query (e.g. search_transactions / "
            "get_transactions / get_accounts) to check whether it exists, and only retry if it does not."
        )

    async def test_existing_update_callers_keep_their_wording(self, mock_api: AsyncMock) -> None:
        mock_api.side_effect = asyncio.TimeoutError()
        with pytest.raises(server.WriteOutcomeUnknownError) as info:
            await server.write_call("update_transaction", creates=False, description="The update")
        assert str(info.value) == (
            "The update may or may not have been applied: the request to Monarch failed (TimeoutError) "
            "after it was sent. Re-read the record to check its current state before sending the update again."
        )


class TestStrictBooleans:
    @pytest.mark.parametrize("value", ["true", "false", 1, 0])
    async def test_apply_to_existing_rejects_non_bool_via_dispatcher(self, mock_api: AsyncMock, value: Any) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await server.mcp.call_tool(
                "create_transaction_rule",
                {
                    "merchant_criteria": [{"value": "Corner Deli"}],
                    "set_category_id": "cat_001",
                    "apply_to_existing_transactions": value,
                },
            )
        mock_api.assert_not_called()

    @pytest.mark.parametrize("value", ["true", 1])
    async def test_apply_to_existing_rejects_non_bool_direct_call(self, mock_api: AsyncMock, value: Any) -> None:
        with pytest.raises(ValueError, match="apply_to_existing_transactions must be true or false"):
            await create(apply_to_existing_transactions=value)
        mock_api.assert_not_called()

    @pytest.mark.parametrize("tool", ["create_transaction_rule", "preview_transaction_rule"])
    @pytest.mark.parametrize("value", ["true", "false", 1, 0])
    async def test_hide_from_reports_rejects_non_bool_via_dispatcher(
        self, mock_api: AsyncMock, tool: str, value: Any
    ) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            await server.mcp.call_tool(
                tool, {"merchant_criteria": [{"value": "Corner Deli"}], "hide_from_reports": value}
            )
        mock_api.assert_not_called()

    async def test_real_booleans_are_accepted_via_dispatcher(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(), LIST_OP: rules_list(STORED_RULE)}, calls)
        await server.mcp.call_tool(
            "create_transaction_rule",
            {
                "merchant_criteria": [{"value": "Corner Deli"}],
                "hide_from_reports": False,
                "apply_to_existing_transactions": False,
            },
        )
        assert sent_input(calls)["setHideFromReportsAction"] is False
        assert sent_input(calls)["applyToExistingTransactions"] is False


class TestRuleToolsViaDispatcher:
    async def test_create_rejects_unknown_arguments(self, mock_api: AsyncMock) -> None:
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="unknown argument"):
            await server.mcp.call_tool(
                "create_transaction_rule",
                {"merchant_criteria": [{"value": "Corner Deli"}], "set_category_id": "cat_001", "priority": 1},
            )
        mock_api.assert_not_called()

    async def test_create_accepts_json_criteria(self, mock_api: AsyncMock) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        mock_api.side_effect = gql_dispatch({CREATE_OP: created(), LIST_OP: rules_list(STORED_RULE)}, calls)
        await server.mcp.call_tool(
            "create_transaction_rule",
            {"merchant_criteria": [{"operator": "contains", "value": "Corner Deli"}], "set_category_id": "cat_001"},
        )
        assert sent_input(calls)["merchantNameCriteria"] == [{"operator": "contains", "value": "Corner Deli"}]


class TestRuleLoggingPrivacy:
    async def test_create_logs_no_rule_values(self, mock_api: AsyncMock, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        mock_api.side_effect = gql_dispatch({CREATE_OP: created("rule_001"), LIST_OP: asyncio.TimeoutError()})
        await create()
        logged = "\n".join(record.getMessage() for record in caplog.records) + caplog.text
        assert "creating_transaction_rule" in logged
        assert "transaction_rule_read_back_failed" in logged
        for secret in ("Corner Deli", "cat_001", "rule_001"):
            assert secret not in logged
