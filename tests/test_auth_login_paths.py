"""Tests for login paths: cookie auth, CAPTCHA handling, forced login, and TOTP-safe retries."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from monarchmoney import CaptchaRequiredException


class TestCaptchaDetection:
    """Monarch signals its CAPTCHA gate two different ways; both must be recognised."""

    def test_detects_captcha_exception(self):
        from server import is_captcha_error

        assert is_captcha_error(CaptchaRequiredException("Programmatic login is blocked by CAPTCHA."))

    def test_detects_captcha_in_generic_error(self):
        """HTTP 429 arrives as LoginFailedException, not CaptchaRequiredException."""
        from server import is_captcha_error

        assert is_captcha_error(Exception("CAPTCHA is required to proceed."))

    def test_ignores_unrelated_errors(self):
        from server import is_captcha_error

        assert not is_captcha_error(Exception("Your code was invalid, please try again."))


class TestRetryDelay:
    """A consumed TOTP code must never be replayed inside the same 30s window."""

    def test_password_login_uses_flat_delay(self):
        from server import seconds_until_retry

        assert seconds_until_retry(3, uses_mfa=False) == 3.0

    def test_mfa_login_waits_for_next_totp_window(self):
        from server import TOTP_PERIOD_SECONDS, seconds_until_retry

        delay = seconds_until_retry(3, uses_mfa=True)

        # Landing in the next window is the whole point.
        assert 1.0 <= delay <= TOTP_PERIOD_SECONDS + 1.0
        now = time.time()
        assert int((now + delay) / TOTP_PERIOD_SECONDS) > int(now / TOTP_PERIOD_SECONDS)


class TestCookieAuthentication:
    @pytest.mark.asyncio
    async def test_cookie_login_authenticates_and_saves_session(self, tmp_path):
        import server
        from server import AuthState, authenticate_with_cookies

        session_file = tmp_path / "session.pickle"
        mock_client = MagicMock()
        mock_client.login_with_cookies = AsyncMock()
        mock_client.save_session = MagicMock(side_effect=lambda path: session_file.write_bytes(b"x"))

        with patch("server.mm_client", mock_client), patch("server.session_file", session_file):
            await authenticate_with_cookies("session_id=abc; csrftoken=def")

        mock_client.login_with_cookies.assert_awaited_once_with("session_id=abc; csrftoken=def", save_session=False)
        assert server.auth_state == AuthState.AUTHENTICATED
        assert session_file.stat().st_mode & 0o777 == 0o600

    @pytest.mark.asyncio
    async def test_cookie_login_failure_reports_expiry_guidance(self, tmp_path):
        import server
        from server import AuthState, authenticate_with_cookies

        mock_client = MagicMock()
        mock_client.login_with_cookies = AsyncMock(side_effect=Exception("Missing required cookies: csrftoken"))

        with patch("server.mm_client", mock_client), patch("server.session_file", tmp_path / "session.pickle"):
            with pytest.raises(ValueError, match="Cookie authentication failed"):
                await authenticate_with_cookies("session_id=abc")

        assert server.auth_state == AuthState.FAILED

    @pytest.mark.asyncio
    async def test_cookies_take_precedence_over_password_login(self, tmp_path):
        from server import initialize_client

        env = {"MONARCH_COOKIES": "session_id=abc; csrftoken=def"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("server.session_file", tmp_path / "absent.pickle"),
            patch("server.MonarchMoney") as mock_cls,
            patch("server.authenticate_with_cookies", new=AsyncMock()) as mock_cookie_auth,
        ):
            mock_cls.return_value = MagicMock(login=AsyncMock())
            await initialize_client()

        mock_cookie_auth.assert_awaited_once_with("session_id=abc; csrftoken=def")
        mock_cls.return_value.login.assert_not_called()

    @pytest.mark.asyncio
    async def test_cookies_alone_satisfy_credential_check(self, tmp_path):
        """MONARCH_EMAIL/PASSWORD are not required when cookies are supplied."""
        from server import initialize_client

        with (
            patch.dict("os.environ", {"MONARCH_COOKIES": "session_id=a; csrftoken=b"}, clear=True),
            patch("server.session_file", tmp_path / "absent.pickle"),
            patch("server.MonarchMoney"),
            patch("server.authenticate_with_cookies", new=AsyncMock()),
        ):
            await initialize_client()  # must not raise ValueError


class TestTokenAuthentication:
    """Monarch's GraphQL API accepts tokens, not cookies, so the token path takes priority."""

    @pytest.mark.asyncio
    async def test_token_login_verifies_and_saves_session(self, tmp_path):
        import server
        from server import AuthState, authenticate_with_token

        session_file = tmp_path / "session.pickle"
        mock_client = MagicMock()
        mock_client.get_accounts = AsyncMock(return_value={"accounts": []})
        mock_client.save_session = MagicMock(side_effect=lambda path: session_file.write_bytes(b"x"))

        with (
            patch("server.MonarchMoney", return_value=mock_client) as mock_cls,
            patch("server.session_file", session_file),
        ):
            await authenticate_with_token("tok_abc123")

        # The Authorization header is only set by the constructor, not set_token().
        mock_cls.assert_called_once_with(token="tok_abc123")
        mock_client.get_accounts.assert_awaited_once()
        assert server.auth_state == AuthState.AUTHENTICATED
        assert session_file.stat().st_mode & 0o777 == 0o600

    @pytest.mark.asyncio
    async def test_token_failure_reports_refresh_guidance(self, tmp_path):
        import server
        from server import AuthState, authenticate_with_token

        mock_client = MagicMock(get_accounts=AsyncMock(side_effect=Exception("401, message='Unauthorized'")))

        with (
            patch("server.MonarchMoney", return_value=mock_client),
            patch("server.session_file", tmp_path / "session.pickle"),
        ):
            with pytest.raises(ValueError, match="Token authentication failed"):
                await authenticate_with_token("stale")

        assert server.auth_state == AuthState.FAILED

    @pytest.mark.asyncio
    async def test_token_takes_precedence_over_cookies_and_password(self, tmp_path):
        from server import initialize_client

        env = {
            "MONARCH_TOKEN": "tok_abc123",
            "MONARCH_COOKIES": "session_id=a; csrftoken=b",
            "MONARCH_EMAIL": "user@example.com",
            "MONARCH_PASSWORD": "pw",
        }
        with (
            patch.dict("os.environ", env, clear=True),
            patch("server.session_file", tmp_path / "absent.pickle"),
            patch("server.MonarchMoney") as mock_cls,
            patch("server.authenticate_with_token", new=AsyncMock()) as mock_token_auth,
            patch("server.authenticate_with_cookies", new=AsyncMock()) as mock_cookie_auth,
        ):
            mock_cls.return_value = MagicMock(login=AsyncMock())
            await initialize_client()

        mock_token_auth.assert_awaited_once_with("tok_abc123")
        mock_cookie_auth.assert_not_awaited()
        mock_cls.return_value.login.assert_not_called()

    @pytest.mark.asyncio
    async def test_token_alone_satisfies_credential_check(self, tmp_path):
        from server import initialize_client

        with (
            patch.dict("os.environ", {"MONARCH_TOKEN": "tok"}, clear=True),
            patch("server.session_file", tmp_path / "absent.pickle"),
            patch("server.MonarchMoney"),
            patch("server.authenticate_with_token", new=AsyncMock()),
        ):
            await initialize_client()  # must not raise ValueError


class TestForcedLogin:
    @pytest.mark.asyncio
    async def test_force_login_leaves_a_usable_client(self, tmp_path):
        """clear_session() nulls mm_client; forced login must still have a client to log in with."""
        import server
        from server import AuthState, initialize_client

        session_file = tmp_path / "session.pickle"
        session_file.write_bytes(b"stale")

        env = {
            "MONARCH_EMAIL": "user@example.com",
            "MONARCH_PASSWORD": "pw",
            "MONARCH_FORCE_LOGIN": "true",
        }
        mock_client = MagicMock(login=AsyncMock(), save_session=MagicMock())

        with (
            patch.dict("os.environ", env, clear=True),
            patch("server.session_file", session_file),
            patch("server.MonarchMoney", return_value=mock_client),
        ):
            await initialize_client()

        mock_client.login.assert_awaited_once()
        assert server.auth_state == AuthState.AUTHENTICATED


class TestCaptchaAbortsLogin:
    @pytest.mark.asyncio
    async def test_captcha_stops_retrying_immediately(self, tmp_path):
        """Retrying a CAPTCHA gate cannot succeed and escalates the block."""
        import server
        from server import AuthState, initialize_client

        env = {"MONARCH_EMAIL": "user@example.com", "MONARCH_PASSWORD": "pw"}
        mock_client = MagicMock(login=AsyncMock(side_effect=Exception("CAPTCHA is required to proceed.")))

        with (
            patch.dict("os.environ", env, clear=True),
            patch("server.session_file", tmp_path / "absent.pickle"),
            patch("server.MonarchMoney", return_value=mock_client),
        ):
            with pytest.raises(ValueError, match="MONARCH_COOKIES"):
                await initialize_client()

        assert mock_client.login.await_count == 1
        assert server.auth_state == AuthState.FAILED
