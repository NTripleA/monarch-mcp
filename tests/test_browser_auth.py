"""Tests for browser-based re-authentication (cookie capture + the MCP tool)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aiohttp import ClientConnectorError, ClientSession

from browser_auth import CookieCaptureServer, extract_cookies_from_browsers, missing_required_cookies

FULL_COOKIES = "session_id=abc123; csrftoken=def456; cf_clearance=xyz; __cf_bm=bm"


class TestCookieValidation:
    def test_accepts_full_cookie_header(self):
        assert missing_required_cookies(FULL_COOKIES) == []

    def test_reports_each_missing_cookie(self):
        assert missing_required_cookies("cf_clearance=xyz") == ["session_id", "csrftoken"]
        assert missing_required_cookies("session_id=a; cf_clearance=x") == ["csrftoken"]

    def test_ignores_malformed_pairs(self):
        assert missing_required_cookies("garbage; session_id=a; csrftoken=b") == []


class TestCookieExtraction:
    def test_returns_none_when_dependency_missing(self):
        """The `browser` extra is optional -- absence must degrade, not raise."""
        with patch("browser_auth._extract_with_rookiepy", side_effect=ImportError):
            with patch("browser_auth._extract_with_browser_cookie3", side_effect=ImportError):
                assert extract_cookies_from_browsers() is None

    def test_returns_none_when_cookies_incomplete(self):
        with patch("browser_auth._extract_with_rookiepy", return_value={"cf_clearance": "x"}):
            with patch("browser_auth._extract_with_browser_cookie3", side_effect=ImportError):
                assert extract_cookies_from_browsers() is None

    def test_builds_header_from_store(self):
        found = {"session_id": "a", "csrftoken": "b", "cf_clearance": "c"}
        with patch("browser_auth._extract_with_rookiepy", return_value=found):
            result = extract_cookies_from_browsers()

        assert result is not None
        assert missing_required_cookies(result) == []
        assert "cf_clearance=c" in result


@pytest_asyncio.fixture
async def capture():
    server = CookieCaptureServer()
    await server.start()
    yield server
    await server.stop()


class TestCaptureServer:
    pytestmark = pytest.mark.asyncio

    async def test_serves_page_on_valid_token(self, capture):
        async with ClientSession() as http, http.get(capture.url) as resp:
            assert resp.status == 200
            assert "Connect Monarch Money" in await resp.text()

    async def test_rejects_wrong_token(self, capture):
        bad = capture.url.rsplit("/", 1)[0] + "/not-the-token"
        async with ClientSession() as http, http.get(bad) as resp:
            assert resp.status == 404

    async def test_submit_captures_cookies(self, capture):
        async with ClientSession() as http:
            async with http.post(f"{capture.url}/submit", data={"cookies": FULL_COOKIES}) as resp:
                assert resp.status == 200

        cookie_string, method = await capture.wait(timeout=1)
        assert cookie_string == FULL_COOKIES
        assert method == "pasted"

    async def test_submit_rejects_incomplete_header(self, capture):
        async with ClientSession() as http:
            async with http.post(f"{capture.url}/submit", data={"cookies": "cf_clearance=x"}) as resp:
                body = await resp.text()

        assert "session_id" in body and "csrftoken" in body
        with pytest.raises(asyncio.TimeoutError):
            await capture.wait(timeout=0.05)

    async def test_token_is_single_use(self, capture):
        async with ClientSession() as http:
            async with http.post(f"{capture.url}/submit", data={"cookies": FULL_COOKIES}) as first:
                assert first.status == 200
            async with http.post(f"{capture.url}/submit", data={"cookies": FULL_COOKIES}) as second:
                assert "already used" in await second.text()

    async def test_detect_captures_without_paste(self, capture):
        with patch("browser_auth.extract_cookies_from_browsers", return_value=FULL_COOKIES):
            async with ClientSession() as http, http.post(f"{capture.url}/detect") as resp:
                assert resp.status == 200

        cookie_string, method = await capture.wait(timeout=1)
        assert cookie_string == FULL_COOKIES
        assert method == "auto-detected"

    async def test_detect_failure_falls_back_to_page(self, capture):
        with patch("browser_auth.extract_cookies_from_browsers", return_value=None):
            async with ClientSession() as http, http.post(f"{capture.url}/detect") as resp:
                assert "Could not read a Monarch session" in await resp.text()

    async def test_wait_times_out_without_input(self, capture):
        with pytest.raises(asyncio.TimeoutError):
            await capture.wait(timeout=0.05)

    async def test_server_stops_listening_after_stop(self):
        server = CookieCaptureServer()
        url = await server.start()
        await server.stop()

        with pytest.raises(ClientConnectorError):
            async with ClientSession() as http, http.get(url):
                pass


def make_ctx(action="accept", elicit_error=None):
    ctx = MagicMock()
    if elicit_error is not None:
        ctx.elicit_url = AsyncMock(side_effect=elicit_error)
    else:
        ctx.elicit_url = AsyncMock(return_value=MagicMock(action=action))
    ctx.session.send_elicit_complete = AsyncMock()
    return ctx


class TestBrowserAuthTool:
    pytestmark = pytest.mark.asyncio

    async def test_successful_sign_in_saves_session(self):
        from server import authenticate_browser_session

        ctx = make_ctx()
        with (
            patch("server.browser_auth.CookieCaptureServer") as mock_cls,
            patch("server.authenticate_with_cookies", new=AsyncMock()) as mock_auth,
            patch("server.mm_client", MagicMock()),
        ):
            mock_cls.return_value = MagicMock(
                start=AsyncMock(return_value="http://127.0.0.1:1/auth/tok"),
                wait=AsyncMock(return_value=(FULL_COOKIES, "pasted")),
                stop=AsyncMock(),
            )
            result = await authenticate_browser_session(ctx)

        mock_auth.assert_awaited_once_with(FULL_COOKIES)
        ctx.session.send_elicit_complete.assert_awaited_once()
        assert result.authenticated is True
        assert result.method == "pasted"

    async def test_result_never_contains_credentials(self):
        """Captured cookies must not reach structured output."""
        from server import authenticate_browser_session

        with (
            patch("server.browser_auth.CookieCaptureServer") as mock_cls,
            patch("server.authenticate_with_cookies", new=AsyncMock()),
            patch("server.mm_client", MagicMock()),
        ):
            mock_cls.return_value = MagicMock(
                start=AsyncMock(return_value="http://127.0.0.1:1/auth/tok"),
                wait=AsyncMock(return_value=(FULL_COOKIES, "auto-detected")),
                stop=AsyncMock(),
            )
            result = await authenticate_browser_session(make_ctx())

        serialized = result.model_dump_json()
        for secret in ("abc123", "def456", "xyz", "cf_clearance", "session_id"):
            assert secret not in serialized

    @pytest.mark.parametrize("action", ["decline", "cancel"])
    async def test_declined_elicitation_changes_nothing(self, action):
        from server import authenticate_browser_session

        with (
            patch("server.browser_auth.CookieCaptureServer") as mock_cls,
            patch("server.authenticate_with_cookies", new=AsyncMock()) as mock_auth,
        ):
            mock_cls.return_value = MagicMock(
                start=AsyncMock(return_value="http://127.0.0.1:1/auth/tok"),
                wait=AsyncMock(),
                stop=AsyncMock(),
            )
            result = await authenticate_browser_session(make_ctx(action=action))

        mock_auth.assert_not_awaited()
        assert result.authenticated is False
        assert result.method == action

    async def test_falls_back_to_opening_browser_when_elicitation_unsupported(self):
        from server import authenticate_browser_session

        ctx = make_ctx(elicit_error=RuntimeError("elicitation not supported"))
        with (
            patch("server.browser_auth.CookieCaptureServer") as mock_cls,
            patch("server.authenticate_with_cookies", new=AsyncMock()),
            patch("server.webbrowser.open", return_value=True) as mock_open,
            patch("server.mm_client", MagicMock()),
        ):
            mock_cls.return_value = MagicMock(
                start=AsyncMock(return_value="http://127.0.0.1:1/auth/tok"),
                wait=AsyncMock(return_value=(FULL_COOKIES, "pasted")),
                stop=AsyncMock(),
            )
            result = await authenticate_browser_session(ctx)

        mock_open.assert_called_once_with("http://127.0.0.1:1/auth/tok")
        ctx.session.send_elicit_complete.assert_not_awaited()
        assert result.authenticated is True

    async def test_timeout_reports_url_and_stops_server(self):
        from server import authenticate_browser_session

        stop = AsyncMock()
        with (
            patch("server.browser_auth.CookieCaptureServer") as mock_cls,
            patch("server.authenticate_with_cookies", new=AsyncMock()) as mock_auth,
        ):
            mock_cls.return_value = MagicMock(
                start=AsyncMock(return_value="http://127.0.0.1:1/auth/tok"),
                wait=AsyncMock(side_effect=asyncio.TimeoutError),
                stop=stop,
            )
            result = await authenticate_browser_session(make_ctx())

        mock_auth.assert_not_awaited()
        stop.assert_awaited_once()
        assert result.authenticated is False
        assert result.method == "timeout"
        assert "http://127.0.0.1:1/auth/tok" in result.message
