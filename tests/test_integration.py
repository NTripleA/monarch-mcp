"""Integration tests that verify actual Monarch Money API connectivity.

These hit the live Monarch API and perform real login attempts, so they require an
explicit opt-in as well as credentials. Without MONARCH_RUN_INTEGRATION=1 they are
skipped even when a .env file is present -- otherwise a plain `uv run pytest` (or
scripts/ci.py) silently fires login attempts, and repeated failures escalate Monarch's
CAPTCHA gate.

Credentials must be supplied through the environment; this module deliberately does
not read .env (server.py does that, at its entry point only).

To run integration tests:
    MONARCH_RUN_INTEGRATION=1 MONARCH_EMAIL=... MONARCH_PASSWORD=... \
        uv run pytest tests/test_integration.py -v

Or source your .env explicitly first:
    set -a; source .env; set +a
    MONARCH_RUN_INTEGRATION=1 uv run pytest tests/test_integration.py -v
"""

import os

import pytest
import pytest_asyncio
from monarchmoney import MonarchMoney

# Skip unless explicitly opted in AND credentials are available. These tests do not
# read .env -- only the server entry point does -- so credentials must be passed
# explicitly, and the opt-in flag is required on top of that.
INTEGRATION_ENABLED = os.environ.get("MONARCH_RUN_INTEGRATION") == "1"

CREDENTIALS_AVAILABLE = all(
    [
        os.environ.get("MONARCH_EMAIL"),
        os.environ.get("MONARCH_PASSWORD"),
    ]
)

if not INTEGRATION_ENABLED:
    SKIP_REASON = "Live-API tests are opt-in: set MONARCH_RUN_INTEGRATION=1 to run them"
elif not CREDENTIALS_AVAILABLE:
    SKIP_REASON = "Monarch Money credentials not available (set MONARCH_EMAIL and MONARCH_PASSWORD)"
else:
    SKIP_REASON = ""

pytestmark = pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON)


@pytest_asyncio.fixture
async def authenticated_client() -> MonarchMoney:
    """Create and authenticate a MonarchMoney client."""
    mm = MonarchMoney()
    await mm.login(
        os.environ["MONARCH_EMAIL"],
        os.environ["MONARCH_PASSWORD"],
        mfa_secret_key=os.environ.get("MONARCH_MFA_SECRET"),
    )
    return mm


class TestMonarchAPIConnectivity:
    """Integration tests for Monarch Money API connectivity."""

    @pytest.mark.asyncio
    async def test_authentication(self) -> None:
        """Test that we can authenticate with Monarch Money."""
        mm = MonarchMoney()
        await mm.login(
            os.environ["MONARCH_EMAIL"],
            os.environ["MONARCH_PASSWORD"],
            mfa_secret_key=os.environ.get("MONARCH_MFA_SECRET"),
        )
        # If we get here without exception, auth worked
        assert mm is not None

    @pytest.mark.asyncio
    async def test_get_accounts(self, authenticated_client: MonarchMoney) -> None:
        """Test that we can fetch accounts."""
        accounts = await authenticated_client.get_accounts()
        assert isinstance(accounts, dict)
        assert "accounts" in accounts
        assert isinstance(accounts["accounts"], list)

    @pytest.mark.asyncio
    async def test_get_transactions(self, authenticated_client: MonarchMoney) -> None:
        """Test that we can fetch transactions."""
        transactions = await authenticated_client.get_transactions(limit=5)
        assert transactions is not None

    @pytest.mark.asyncio
    async def test_get_budgets(self, authenticated_client: MonarchMoney) -> None:
        """Test that we can fetch budgets."""
        budgets = await authenticated_client.get_budgets()
        assert budgets is not None


class TestHealthCheck:
    """Quick health check to verify API is working."""

    @pytest.mark.asyncio
    async def test_api_health(self, authenticated_client: MonarchMoney) -> None:
        """Comprehensive health check - tests auth, accounts, transactions, budgets."""
        # Test accounts
        accounts = await authenticated_client.get_accounts()
        account_count = len(accounts.get("accounts", []))
        assert account_count > 0, "Expected at least one account"

        # Test transactions
        transactions = await authenticated_client.get_transactions(limit=5)
        assert transactions is not None, "Expected transactions response"

        # Test budgets
        budgets = await authenticated_client.get_budgets()
        assert budgets is not None, "Expected budgets response"

        print(f"\n✅ Health check passed: {account_count} accounts found")
