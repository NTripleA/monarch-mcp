"""Live checks for the transaction rule tools against the real Monarch API.

Everything here is opt-in and uses the server's own saved session (never a password
login, which Monarch CAPTCHA-gates):

    MONARCH_RUN_INTEGRATION=1 uv run pytest tests/test_integration_rules.py -v

runs the read-only checks (list + preview). Creating a rule additionally needs

    MONARCH_RUN_RULE_WRITES=true

and creates one throwaway rule whose only criterion is "merchant contains
mcp-rule-test-<random>", with apply_to_existing_transactions=False, then deletes it and
confirms the deletion by re-reading the rule list. Other rules are checked unchanged.
MONARCH_RUN_RULE_STAGED_PROBE=true also creates (and deletes) a rule that renames the
merchant and hides from reports; the rename may leave a merchant record behind.

MONARCH_RULE_TEST_CATEGORY_ID picks the category the throwaway rule sets; by default
the first category in the account is used (the rule matches nothing, so it is harmless).

Nothing here reads real merchant names into the source: the positive preview check picks
a merchant from a recent transaction at run time.
"""

import asyncio
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from gql import gql
from gql.transport.exceptions import TransportError
from monarchmoney import LoginFailedException, RequestFailedException

import server

# conftest strips MONARCH_* variables and swaps in a temp session dir before every test,
# so capture what the live tests need at import time.
LIVE_ENABLED = os.environ.get("MONARCH_RUN_INTEGRATION") == "1"
RULE_WRITES_ENABLED = os.environ.get("MONARCH_RUN_RULE_WRITES") == "true"
STAGED_PROBE_ENABLED = os.environ.get("MONARCH_RUN_RULE_STAGED_PROBE") == "true"
CATEGORY_OVERRIDE = os.environ.get("MONARCH_RULE_TEST_CATEGORY_ID")
REAL_SESSION_DIR: Path = server.session_dir
REAL_SESSION_FILE: Path = server.session_file

pytestmark = pytest.mark.skipif(
    not LIVE_ENABLED, reason="Live-API tests are opt-in: set MONARCH_RUN_INTEGRATION=1 to run them"
)
requires_rule_writes = pytest.mark.skipif(
    not RULE_WRITES_ENABLED, reason="Rule writes require MONARCH_RUN_RULE_WRITES=true"
)
requires_staged_probe = pytest.mark.skipif(
    not STAGED_PROBE_ENABLED, reason="The staged-action create probe requires MONARCH_RUN_RULE_STAGED_PROBE=true"
)

# What a live rule read or delete can raise: auth/session failures, GraphQL/transport
# errors, and network trouble. Anything else is a real test failure.
LIVE_CALL_ERRORS = (
    ValueError,
    LoginFailedException,
    RequestFailedException,
    TransportError,
    aiohttp.ClientError,
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
)
VOLATILE_RULE_FIELDS = {"order", "recentApplicationCount", "lastAppliedAt"}
READ_ATTEMPTS = 5

# Test-only cleanup. Deliberately not a server tool: deleting rules is out of the product's scope.
DELETE_RULE_FOR_TEST = gql(
    """
    mutation Common_DeleteTransactionRule($id: ID!) {
      deleteTransactionRule(id: $id) {
        deleted
        errors { message code fieldErrors { field messages } }
      }
    }
    """
)


@pytest.fixture
def live_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the server back at the real saved session and let it authenticate normally."""
    if not REAL_SESSION_FILE.exists():
        pytest.skip(f"No saved Monarch session at {REAL_SESSION_FILE}; provision one first")
    monkeypatch.setattr(server, "session_dir", REAL_SESSION_DIR)
    monkeypatch.setattr(server, "session_file", REAL_SESSION_FILE)
    server.mm_client = None
    server.auth_state = server.AuthState.NOT_INITIALIZED
    yield


def normalized(rule: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in rule.items() if key not in VOLATILE_RULE_FIELDS}


def snapshot(rules: list[Any]) -> dict[str, dict[str, Any]]:
    return {str(rule["id"]): normalized(rule) for rule in rules if isinstance(rule, dict)}


def carries_marker(rule: Any, marker: str) -> bool:
    if not isinstance(rule, dict):
        return False
    criteria = (rule.get("merchantNameCriteria") or []) + (rule.get("merchantCriteria") or [])
    return any(isinstance(c, dict) and c.get("value") == marker for c in criteria)


def marker_rules(rules: list[Any], marker: str) -> list[dict[str, Any]]:
    return [rule for rule in rules if carries_marker(rule, marker)]


def unchanged_problems(before: dict[str, dict[str, Any]], rules: list[Any]) -> list[str]:
    """Pre-existing rules that disappeared or changed. Rules created meanwhile are ignored."""
    after = snapshot(rules)
    problems = [f"rule {rule_id} is missing" for rule_id in before if rule_id not in after]
    problems += [
        f"rule {rule_id} changed" for rule_id, rule in before.items() if rule_id in after and after[rule_id] != rule
    ]
    return problems


async def harmless_category_id() -> str:
    if CATEGORY_OVERRIDE:
        return CATEGORY_OVERRIDE
    await server.ensure_authenticated()
    response = await server.api_call_with_retry("get_transaction_categories")
    categories = server.extract_list(response, "categories")
    assert categories, "The account has no categories to point the test rule at"
    return str(categories[0]["id"])


async def recent_merchant_name() -> str:
    await server.ensure_authenticated()
    response = await server.api_call_with_retry("get_transactions", limit=25)
    for transaction in server.extract_transactions_list(response):
        name = (transaction.get("merchant") or {}).get("name")
        if isinstance(name, str) and len(name.strip()) >= 3:
            return name.strip()
    pytest.skip("No recent transaction with a merchant name to preview against")


async def delete_rule_for_test(rule_id: str) -> str | None:
    """Try to delete a rule. Returns a diagnostic instead of raising: a delete can succeed
    on Monarch's side and still fail on the wire, so only a re-read decides the outcome."""
    assert server.mm_client is not None
    try:
        result = await asyncio.wait_for(
            server.mm_client.gql_call(
                operation="Common_DeleteTransactionRule", graphql_query=DELETE_RULE_FOR_TEST, variables={"id": rule_id}
            ),
            timeout=server.WRITE_TIMEOUT_SECONDS,
        )
    except LIVE_CALL_ERRORS as e:
        return f"delete request failed: {server.safe_error_fields(e)}"
    payload = result.get("deleteTransactionRule") if isinstance(result, dict) else None
    if isinstance(payload, dict) and payload.get("errors") is not None:
        return "delete payload carried errors"
    return None


async def clean_up(marker: str, rule_id: str | None, before: dict[str, dict[str, Any]]) -> None:
    """Delete every trace of the test rule, judged only by re-reading the rule list.

    Fails loudly, naming the marker, if cleanup can't be confirmed.
    """
    diagnostics: list[str] = []
    deleted: set[str] = set()
    if rule_id is not None:
        deleted.add(rule_id)
        if (problem := await delete_rule_for_test(rule_id)) is not None:
            diagnostics.append(problem)

    for attempt in range(READ_ATTEMPTS):
        try:
            rules = await server._fetch_transaction_rules()
        except LIVE_CALL_ERRORS as e:
            diagnostics.append(f"rule list unreadable: {server.safe_error_fields(e)}")
        else:
            remaining = marker_rules(rules, marker)
            if not remaining:
                problems = unchanged_problems(before, rules)
                assert not problems, f"Pre-existing rules were affected: {problems}"
                return
            if len(remaining) > 1:
                ids = [str(rule["id"]) for rule in remaining]
                pytest.fail(
                    f"Found {len(remaining)} rules carrying test marker {marker!r} ({ids}); refusing to guess. "
                    "Manual cleanup in Monarch may be required."
                )
            candidate = str(remaining[0]["id"])
            if candidate not in deleted:
                # Covers a create whose ID we never received (ambiguous failure, or no-ID success).
                deleted.add(candidate)
                if (problem := await delete_rule_for_test(candidate)) is not None:
                    diagnostics.append(problem)
        await asyncio.sleep(attempt + 1)

    pytest.fail(
        f"Could not confirm cleanup of the test rule with merchant criterion {marker!r} "
        f"(rule id: {rule_id or 'unknown'}; delete attempted for: {sorted(deleted) or 'none'}). "
        f"Diagnostics: {diagnostics or 'none'}. Manual cleanup in Monarch may be required."
    )


async def verify_created(marker: str, rule_id: str, before: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Required round-trip verification, with a bounded wait for read-after-write delay."""
    diagnostics: list[str] = []
    for attempt in range(READ_ATTEMPTS):
        try:
            rules = await server._fetch_transaction_rules()
        except LIVE_CALL_ERRORS as e:
            diagnostics.append(f"rule list unreadable: {server.safe_error_fields(e)}")
        else:
            found = marker_rules(rules, marker)
            if len(found) == 1:
                assert found[0]["id"] == rule_id, "The rule carrying the marker has a different ID than create returned"
                problems = unchanged_problems(before, rules)
                assert not problems, f"Pre-existing rules were affected: {problems}"
                return found[0]
            assert len(found) <= 1, f"{len(found)} rules carry the test marker; expected exactly one"
            diagnostics.append("test rule not visible yet")
        await asyncio.sleep(attempt + 1)
    pytest.fail(f"Created rule {rule_id} could not be verified by reading it back: {diagnostics}")


class TestLiveRuleReads:
    async def test_list_rules(self, live_session: None) -> None:
        result = await server.get_transaction_rules()
        assert result.count == len(result.rules)
        for rule in result.rules:
            assert isinstance(rule, dict) and rule.get("id")

    async def test_preview_unique_marker_matches_nothing(self, live_session: None) -> None:
        # If Monarch ignored merchantNameCriteria, this would match every transaction.
        marker = "mcp-rule-test-" + secrets.token_hex(8)
        category_id = await harmless_category_id()
        result = await server.preview_transaction_rule(
            merchant_criteria=[server.RuleTextCriterion(operator="contains", value=marker)],
            set_category_id=category_id,
        )
        assert result.total_count == 0
        assert result.matches == []

    async def test_preview_contains_filters_by_merchant(self, live_session: None) -> None:
        merchant = await recent_merchant_name()
        category_id = await harmless_category_id()
        result = await server.preview_transaction_rule(
            merchant_criteria=[server.RuleTextCriterion(operator="contains", value=merchant)],
            set_category_id=category_id,
        )
        assert result.total_count >= 1
        for match in result.matches:
            assert isinstance(match, dict)
            assert (match.get("newCategory") or {}).get("id") == category_id

    async def test_preview_staged_actions(self, live_session: None) -> None:
        merchant = await recent_merchant_name()
        new_name = "mcp-rule-probe-" + secrets.token_hex(4)
        result = await server.preview_transaction_rule(
            merchant_criteria=[server.RuleTextCriterion(operator="contains", value=merchant)],
            set_merchant_name=new_name,
            hide_from_reports=True,
        )
        assert result.total_count >= 1
        for match in result.matches:
            assert isinstance(match, dict)
            assert match.get("newName") == new_name
            assert match.get("newHideFromReports") is True


async def create_verify_and_clean_up(marker: str, **tool_args: Any) -> dict[str, Any]:
    """Create a rule matching only ``marker``, verify the round trip, and always clean up."""
    await server.ensure_authenticated()
    existing = await server._fetch_transaction_rules()
    assert not marker_rules(existing, marker)
    before = snapshot(existing)

    create_attempted = False
    created_rule_id: str | None = None
    try:
        create_attempted = True
        result = await server.create_transaction_rule(
            merchant_criteria=[server.RuleTextCriterion(operator="contains", value=marker)],
            apply_to_existing_transactions=False,
            **tool_args,
        )
        created_rule_id = result.rule_id
        assert result.apply_to_existing_requested is False
        return await verify_created(marker, created_rule_id, before)
    finally:
        if create_attempted:
            await clean_up(marker, created_rule_id, before)


@requires_rule_writes
class TestLiveRuleCreate:
    async def test_create_contains_rule_round_trip(self, live_session: None) -> None:
        marker = "mcp-rule-test-" + secrets.token_hex(8)
        category_id = await harmless_category_id()
        stored = await create_verify_and_clean_up(marker, set_category_id=category_id)
        assert stored["merchantNameCriteria"] == [{"operator": "contains", "value": marker}]
        assert (stored.get("setCategoryAction") or {}).get("id") == category_id

    @requires_staged_probe
    async def test_create_rule_with_staged_actions(self, live_session: None) -> None:
        # Setting a merchant name may leave a merchant record behind in Monarch after the
        # rule is deleted, which is why this probe has its own opt-in.
        marker = "mcp-rule-test-" + secrets.token_hex(8)
        new_name = marker + "-renamed"
        stored = await create_verify_and_clean_up(marker, set_merchant_name=new_name, hide_from_reports=True)
        assert (stored.get("setMerchantAction") or {}).get("name") == new_name
        assert stored.get("setHideFromReportsAction") is True
