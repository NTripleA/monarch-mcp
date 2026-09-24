"""Session-only authentication: the remote (Pi) path with a provisioned session.pickle and
no Monarch credentials, plus safe storage of session files.

All tests use a temp session directory (see conftest) and mocks only.
"""

import os
import pickle
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server


def write_session(data: object, mode: int = 0o600) -> Path:
    server.session_file.write_bytes(pickle.dumps(data))
    server.session_file.chmod(mode)
    return server.session_file


def bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def fresh_auth() -> None:
    server.mm_client = None
    server.auth_state = server.AuthState.NOT_INITIALIZED
    server.auth_lock = None


class _Gadget:
    def __reduce__(self) -> tuple[Callable[..., Any], tuple[str]]:
        return (os.system, ("echo pwned",))


class TestCachedSessionNeedsNoCredentials:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", ["stdio", "http"])
    async def test_token_session_loads_with_zero_env_credentials(self, transport: str, fresh_auth: None) -> None:
        server.RUNTIME.transport = transport
        write_session({"token": "tok_provisioned", "auth_mode": "token"})
        assert not any(os.getenv(name) for name in server.CREDENTIAL_ENV_VARS)

        await server.ensure_authenticated()

        assert server.auth_state == server.AuthState.AUTHENTICATED
        assert server.mm_client is not None
        assert server.mm_client.token == "tok_provisioned"

    @pytest.mark.asyncio
    async def test_cookie_session_loads_via_public_set_cookies(self, fresh_auth: None) -> None:
        server.RUNTIME.transport = "http"
        cookies = {"session_id": "sess_value", "csrftoken": "csrf_value", "cf_clearance": "cf_value"}
        write_session({"token": None, "auth_mode": "cookie", "cookies": cookies})

        await server.ensure_authenticated()

        assert server.mm_client is not None
        assert server.mm_client._auth_mode == "cookie"
        assert server.mm_client._cookies == cookies

    @pytest.mark.asyncio
    async def test_http_mode_ignores_credential_env_for_login(self, fresh_auth: None, monkeypatch) -> None:
        server.RUNTIME.transport = "http"
        monkeypatch.setenv("MONARCH_EMAIL", "user@example.com")
        monkeypatch.setenv("MONARCH_PASSWORD", "pw")
        monkeypatch.setenv("MONARCH_TOKEN", "tok_env")

        with patch("server.MonarchMoney") as mm_class, pytest.raises(server.SessionUnavailableError):
            await server.ensure_authenticated()

        mm_class.return_value.login.assert_not_called()
        mm_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_configure_runtime_http_strips_credential_env(self, monkeypatch) -> None:
        monkeypatch.setenv("MONARCH_PASSWORD", "pw-should-be-dropped")
        monkeypatch.setenv("MONARCH_MFA_SECRET", "totp-should-be-dropped")
        server.configure_runtime("http")
        assert "MONARCH_PASSWORD" not in os.environ
        assert "MONARCH_MFA_SECRET" not in os.environ
        assert server.fallback_credentials() == []


class TestMissingOrExpiredSession:
    @pytest.mark.asyncio
    async def test_missing_session_fails_cleanly_without_login(self, fresh_auth: None) -> None:
        server.RUNTIME.transport = "http"
        with patch("server.MonarchMoney") as mm_class:
            with pytest.raises(server.SessionUnavailableError, match="provision"):
                await server.ensure_authenticated()
            # A second call inside the failure window must not retry anything.
            with pytest.raises(server.SessionUnavailableError):
                await server.ensure_authenticated()
        mm_class.assert_not_called()
        assert server.auth_state == server.AuthState.FAILED

    @pytest.mark.asyncio
    async def test_expired_session_is_not_deleted_and_not_looped(self, fresh_auth: None) -> None:
        server.RUNTIME.transport = "http"
        path = write_session({"token": "tok_expired"})
        await server.ensure_authenticated()

        client = MagicMock()
        client.get_accounts = AsyncMock(side_effect=Exception("401, message='Unauthorized'"))
        client.get_subscription_details = AsyncMock(side_effect=Exception("401 Unauthorized"))
        server.mm_client = client

        with patch("server.asyncio.sleep") as sleep, patch("server.MonarchMoney") as mm_class:
            with pytest.raises(server.SessionUnavailableError, match="session.pickle"):
                await server.api_call_with_retry("get_accounts")
            sleep.assert_not_called()
            mm_class.assert_not_called()

        assert client.get_accounts.await_count == 1
        assert path.exists(), "a provisioned session must never be deleted"
        assert server.auth_state == server.AuthState.FAILED

        # Further calls fail fast until the file changes -- no traffic to Monarch.
        with pytest.raises(server.SessionUnavailableError):
            await server.ensure_authenticated()
        assert client.get_accounts.await_count == 1

    @pytest.mark.asyncio
    async def test_fresh_session_file_is_picked_up_without_restart(self, fresh_auth: None) -> None:
        server.RUNTIME.transport = "http"
        path = write_session({"token": "tok_old"})
        await server.ensure_authenticated()
        server.mark_auth_failed("expired", "auth")

        write_session({"token": "tok_new"})
        bump_mtime(path)

        await server.ensure_authenticated()
        assert server.auth_state == server.AuthState.AUTHENTICATED
        assert server.mm_client is not None and server.mm_client.token == "tok_new"

    @pytest.mark.asyncio
    async def test_rejected_call_reloads_a_replaced_file_once(self, fresh_auth: None) -> None:
        server.RUNTIME.transport = "http"
        path = write_session({"token": "tok_old"})
        await server.ensure_authenticated()
        old_client = MagicMock(get_accounts=AsyncMock(side_effect=Exception("401 Unauthorized")))
        server.mm_client = old_client

        write_session({"token": "tok_new"})
        bump_mtime(path)

        fresh = MagicMock(get_accounts=AsyncMock(return_value={"accounts": []}))
        with patch("server.client_from_session", return_value=fresh) as build:
            assert await server.api_call_with_retry("get_accounts") == {"accounts": []}
        build.assert_called_once()
        assert build.call_args.args[0].token == "tok_new"

    @pytest.mark.asyncio
    async def test_misclassified_error_does_not_lock_out_the_server(self, fresh_auth: None) -> None:
        """An error that merely mentions 403 is re-checked before the session is declared dead."""
        server.RUNTIME.transport = "http"
        write_session({"token": "tok_ok"})
        await server.ensure_authenticated()
        client = MagicMock(
            get_accounts=AsyncMock(side_effect=Exception("Unauthorized field access in query")),
            get_subscription_details=AsyncMock(return_value={"subscription": {}}),
        )
        server.mm_client = client

        with pytest.raises(Exception, match="Unauthorized field access"):
            await server.api_call_with_retry("get_accounts")
        assert server.auth_state == server.AuthState.AUTHENTICATED

    @pytest.mark.asyncio
    async def test_stdio_without_credentials_keeps_existing_guidance(self, fresh_auth: None) -> None:
        with pytest.raises(ValueError, match="MONARCH_EMAIL and MONARCH_PASSWORD"):
            await server.initialize_client()


class TestSessionFileSafety:
    def test_malicious_pickle_is_refused_without_executing(self, tmp_path: Path) -> None:
        write_session({"token": _Gadget()})
        with patch("os.system") as system, pytest.raises(server.SessionFileError, match="corrupt"):
            server.read_session_file(server.session_file)
        system.assert_not_called()

    @pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
    def test_writable_by_others_is_refused(self, mode: int) -> None:
        write_session({"token": "tok"}, mode=mode)
        with pytest.raises(server.SessionFileError, match="writable by other users"):
            server.read_session_file(server.session_file)

    def test_readable_by_others_is_tightened_to_0600(self) -> None:
        path = write_session({"token": "tok"}, mode=0o644)
        server.read_session_file(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_symlink_is_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real.pickle"
        real.write_bytes(pickle.dumps({"token": "tok"}))
        real.chmod(0o600)
        server.session_file.symlink_to(real)
        with pytest.raises(server.SessionFileError):
            server.read_session_file(server.session_file)

    @pytest.mark.parametrize(
        "payload",
        [
            ["not", "a", "dict"],
            {"token": "tok", "surprise": "field"},
            {"token": ""},
            {"auth_mode": "cookie", "cookies": {"session_id": "only-one"}},
            {"token": "tok", "auth_mode": "magic"},
            {"token": 12345},
        ],
    )
    def test_unexpected_schema_is_refused(self, payload: object) -> None:
        write_session(payload)
        with pytest.raises(server.SessionFileError):
            server.read_session_file(server.session_file)

    def test_session_dir_is_created_0700_and_tightened(self, tmp_path: Path) -> None:
        loose = tmp_path / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        server.secure_session_dir(loose)
        assert stat.S_IMODE(loose.stat().st_mode) == 0o700

        created = tmp_path / "new" / "state"
        server.secure_session_dir(created)
        assert stat.S_IMODE(created.stat().st_mode) == 0o700

    def test_persist_session_is_atomic_and_0600(self) -> None:
        old_umask = os.umask(0o022)  # a permissive umask must not leak into the file mode
        try:
            client = server.MonarchMoney(token="tok_persisted")
            server.persist_session(client)
        finally:
            os.umask(old_umask)

        path = server.session_file
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert server.read_session_file(path).token == "tok_persisted"
        leftovers = [p.name for p in path.parent.iterdir() if p.name != "session.pickle"]
        assert leftovers == []

    def test_library_written_session_round_trips(self) -> None:
        """Files produced by MonarchMoney.save_session are accepted by the restricted loader."""
        client = server.MonarchMoney()
        client.set_cookies({"session_id": "a", "csrftoken": "b", "cf_clearance": "c"})
        client.save_session(str(server.session_file))
        server.session_file.chmod(0o600)

        stored = server.read_session_file(server.session_file)
        assert stored.auth_mode == "cookie"
        assert stored.cookies == {"session_id": "a", "csrftoken": "b", "cf_clearance": "c"}
