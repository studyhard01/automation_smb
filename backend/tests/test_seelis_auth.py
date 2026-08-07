"""SeeLIS 계정 로그인 backend의 외부 호출·저장·HTTP 보안 계약을 검증한다."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from smb_finder.auth.api import create_auth_router
from smb_finder.auth.config import AuthSettings
from smb_finder.auth.models import SeeLisLoginRequest, ServiceSummary, UserResponse, UserUpdateRequest
from smb_finder.auth.seelis import SeeLisClient, SeeLisIdentity
from smb_finder.auth.service import AuthService
from smb_finder.auth.store import EXTERNAL_PASSWORD_SENTINEL, AuthStoreError, PostgresAuthStore


def _settings(**updates: object) -> AuthSettings:
    values: dict[str, object] = {
        "_env_file": None,
        "auth_session_cookie_name": "test_session",
        "auth_session_ttl_seconds": 3600,
        "auth_cookie_secure": False,
        "seelis_login_token_api_url": "https://login.example.test/token",
        "seelis_login_token_api_url_key": "x-api-key",
        "seelis_login_token_api_url_value": "synthetic-api-key",
        "seelis_login_keycloak_url": "https://identity.example.test/userinfo",
        "seelis_login_keycloak_url_key": "Authorization",
        "seelis_login_keycloak_url_value": "Bearer {token}",
    }
    values.update(updates)
    return AuthSettings(**values)


def _user(*, active: bool = True) -> UserResponse:
    now = datetime.now(timezone.utc)
    return UserResponse(
        id=uuid4(),
        username="seelis.member",
        email="member@example.test",
        display_name="합성 SeeLIS 사용자",
        system_role="user",
        is_superuser=False,
        is_active=active,
        all_services_access=False,
        service_keys=[],
        created_at=now,
        updated_at=now,
        last_login_at=now,
    )


class _FakeRepository:
    def __init__(self, user: UserResponse | None = None) -> None:
        self.user = user or _user()
        self.sessions: set[str] = set()
        self.upsert_calls = 0
        self.upsert_values: dict[str, object] = {}
        self.upsert_error: AuthStoreError | None = None

    def upsert_seelis_user(self, **values: object) -> UserResponse:
        self.upsert_calls += 1
        self.upsert_values = values
        if self.upsert_error:
            raise self.upsert_error
        return self.user

    def create_session(self, _user_id: UUID, token_hash: str, _ttl_seconds: int) -> None:
        self.sessions.add(token_hash)

    def get_user_by_session(self, token_hash: str) -> UserResponse | None:
        return self.user if token_hash in self.sessions else None

    def revoke_session(self, token_hash: str) -> None:
        self.sessions.discard(token_hash)

    def get_user(self, user_id: UUID) -> UserResponse | None:
        return self.user if self.user.id == user_id else None

    def create_user(self, **_values: object) -> UserResponse:  # pragma: no cover - protocol 보완
        return self.user

    def get_login_record(self, _username: str) -> dict[str, object] | None:  # pragma: no cover
        return None

    def record_login_failure(self, _user_id: UUID) -> None:  # pragma: no cover
        return None

    def record_login_success(self, _user_id: UUID) -> None:  # pragma: no cover
        return None

    def list_users(self) -> tuple[list[UserResponse], list[ServiceSummary]]:  # pragma: no cover
        return [self.user], []

    def update_user(
        self, _actor: UserResponse, _target_id: UUID, _request: UserUpdateRequest
    ) -> UserResponse:  # pragma: no cover
        return self.user


class _FakeAuthenticator:
    def __init__(self, error: AuthStoreError | None = None) -> None:
        self.error = error
        self.calls = 0

    def authenticate(self, user_id: str, _password: str) -> SeeLisIdentity:
        self.calls += 1
        if self.error:
            raise self.error
        return SeeLisIdentity(
            subject="synthetic-subject",
            username=user_id,
            display_name="합성 SeeLIS 사용자",
            email="member@example.test",
            department="합성 부서",
            department_code="SYN001",
        )


def _api_client(repository: _FakeRepository, authenticator: _FakeAuthenticator) -> TestClient:
    settings = _settings()
    service = AuthService(repository, session_ttl_seconds=3600, seelis_authenticator=authenticator)
    app = FastAPI()
    app.include_router(create_auth_router(settings, service))
    return TestClient(app)


def test_seelis_settings_fail_closed_until_all_six_values_are_present() -> None:
    assert AuthSettings(_env_file=None).seelis_login_configured is False
    assert _settings().seelis_login_configured is True
    assert _settings(seelis_login_keycloak_url="").seelis_login_configured is False
    assert _settings(seelis_login_token_api_url_value="   ").seelis_login_configured is False
    assert _settings().seelis_login_token_api_timeout_ms == 30_000
    assert _settings().seelis_login_keycloak_timeout_ms == 10_000


def test_seelis_user_id_does_not_invent_local_account_character_rules() -> None:
    request = SeeLisLoginRequest.model_validate({"userId": "member@example.test", "pswd": " synthetic password "})

    assert request.user_id == "member@example.test"
    assert request.password == " synthetic password "


def test_seelis_client_calls_token_then_userinfo_and_returns_only_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token":
            assert request.headers["x-api-key"] == "synthetic-api-key"
            assert request.content == b'{"userId":"seelis.member","pswd":"synthetic-password"}'
            return httpx.Response(
                201,
                json={
                    "result": {
                        "accessToken": "synthetic-access-token",
                        "refreshToken": "must-not-be-used",
                        "userNm": "합성 SeeLIS 사용자",
                        "deptNm": " 합성 부서 ",
                        "deptCd": " SYN001 ",
                        "emalAddr": "MEMBER@EXAMPLE.TEST",
                    }
                },
            )
        assert request.headers["Authorization"] == "Bearer synthetic-access-token"
        return httpx.Response(200, json={"sub": "synthetic-subject", "preferred_username": "SEELIS.MEMBER"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    identity = SeeLisClient(_settings(), client=http_client).authenticate("seelis.member", "synthetic-password")

    assert [request.url.path for request in requests] == ["/token", "/userinfo"]
    assert identity == SeeLisIdentity(
        subject="synthetic-subject",
        username="seelis.member",
        display_name="합성 SeeLIS 사용자",
        email="member@example.test",
        department="합성 부서",
        department_code="SYN001",
    )
    assert "token" not in identity.__dataclass_fields__


@pytest.mark.parametrize(
    ("token_response", "userinfo_response", "expected_status", "expected_code"),
    [
        (httpx.Response(401, text="upstream detail"), None, 401, "invalid_credentials"),
        (httpx.Response(201, text="not-json"), None, 502, "seelis_invalid_response"),
        (httpx.Response(201, json={"result": {}}), None, 502, "seelis_invalid_response"),
        (
            httpx.Response(201, json={"result": {"accessToken": "synthetic-token"}}),
            httpx.Response(401, text="userinfo rejected"),
            401,
            "invalid_credentials",
        ),
        (
            httpx.Response(201, json={"result": {"accessToken": "synthetic-token"}}),
            httpx.Response(200, json={}),
            502,
            "seelis_invalid_response",
        ),
        (
            httpx.Response(201, json={"result": {"accessToken": "synthetic-token"}}),
            httpx.Response(200, json={"sub": "subject", "preferred_username": "different.user"}),
            401,
            "external_identity_mismatch",
        ),
    ],
)
def test_seelis_client_normalizes_upstream_failures(
    token_response: httpx.Response,
    userinfo_response: httpx.Response | None,
    expected_status: int,
    expected_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return token_response if request.url.path == "/token" else userinfo_response  # type: ignore[return-value]

    client = SeeLisClient(_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(AuthStoreError) as raised:
        client.authenticate("seelis.member", "synthetic-password")

    assert raised.value.status_code == expected_status
    assert raised.value.code == expected_code
    assert "upstream" not in raised.value.message


def test_seelis_client_maps_missing_config_and_timeout_to_503() -> None:
    missing = SeeLisClient(AuthSettings(_env_file=None))
    with pytest.raises(AuthStoreError, match="구성되지") as missing_error:
        missing.authenticate("seelis.member", "synthetic-password")
    assert missing_error.value.status_code == 503

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    unavailable = SeeLisClient(_settings(), client=httpx.Client(transport=httpx.MockTransport(timeout)))
    with pytest.raises(AuthStoreError) as timeout_error:
        unavailable.authenticate("seelis.member", "synthetic-password")
    assert timeout_error.value.status_code == 503
    assert timeout_error.value.code == "seelis_unavailable"


def test_seelis_failures_do_not_leak_password_key_token_url_or_response(caplog: pytest.LogCaptureFixture) -> None:
    secrets = (
        "private-password",
        "private-api-key",
        "private-access-token",
        "private-response-body",
        "https://private.example.test/token",
    )
    settings = _settings(
        seelis_login_token_api_url=secrets[4],
        seelis_login_token_api_url_value=secrets[1],
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"result": {"accessToken": secrets[2], "userNm": {"bad": secrets[3]}}})

    client = SeeLisClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with caplog.at_level(logging.INFO), pytest.raises(AuthStoreError) as raised:
        client.authenticate("seelis.member", secrets[0])

    exposed = caplog.text + raised.value.message
    assert all(secret not in exposed for secret in secrets)
    assert "stage=token" in caplog.text
    assert "stage=total" in caplog.text


class _ScriptedCursor:
    def __init__(self, fetchone_values: list[dict[str, Any] | None]) -> None:
        self.fetchone_values = fetchone_values
        self.statements: list[tuple[str, object]] = []

    def __enter__(self) -> _ScriptedCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object, parameters: object = None) -> None:
        rendered = statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        self.statements.append((rendered, parameters))

    def fetchone(self) -> dict[str, Any] | None:
        return self.fetchone_values.pop(0)


class _ScriptedConnection:
    def __init__(self, cursor: _ScriptedCursor) -> None:
        self.cursor_instance = cursor

    def __enter__(self) -> _ScriptedConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self, *, row_factory: object = None) -> _ScriptedCursor:
        assert row_factory in (None, dict_row)
        return self.cursor_instance


def test_store_creates_external_user_with_no_permissions_and_unusable_password() -> None:
    cursor = _ScriptedCursor([None, None])
    repository = PostgresAuthStore(
        _settings(
            auth_db_host="db.example.test",
            auth_db_name="synthetic",
            auth_db_user="writer",
            auth_db_password="secret",
        ),
        connect=lambda **_kwargs: _ScriptedConnection(cursor),
    )
    repository._ready = True
    returned = _user()
    repository.get_user = lambda _user_id: returned  # type: ignore[method-assign]

    user = repository.upsert_seelis_user(
        subject="synthetic-subject",
        username="seelis.member",
        email="member@example.test",
        display_name="합성 SeeLIS 사용자",
        department="합성 부서",
        department_code="SYN001",
    )

    rendered = "\n".join(statement for statement, _parameters in cursor.statements)
    normalized_sql = " ".join(rendered.split())
    parameters = [parameters for statement, parameters in cursor.statements if "INSERT INTO" in statement]
    assert user == returned
    assert "'seelis'" in rendered
    assert "'user', false, true, false" in normalized_sql
    assert "user_service_permissions" not in rendered
    assert parameters and EXTERNAL_PASSWORD_SENTINEL in parameters[0]
    assert "합성 부서" in parameters[0]
    assert "SYN001" in parameters[0]


def test_store_updates_existing_external_department_without_changing_permissions() -> None:
    returned = _user()
    cursor = _ScriptedCursor([{"id": returned.id, "username": returned.username, "is_active": True}])
    repository = PostgresAuthStore(
        _settings(
            auth_db_host="db.example.test",
            auth_db_name="synthetic",
            auth_db_user="writer",
            auth_db_password="secret",
        ),
        connect=lambda **_kwargs: _ScriptedConnection(cursor),
    )
    repository._ready = True
    repository.get_user = lambda _user_id: returned  # type: ignore[method-assign]

    user = repository.upsert_seelis_user(
        subject="synthetic-subject",
        username="seelis.member",
        email="member@example.test",
        display_name="변경된 표시 이름",
        department="변경된 합성 부서",
        department_code="SYN002",
    )

    updates = [
        (statement, parameters)
        for statement, parameters in cursor.statements
        if statement.lstrip().startswith("UPDATE")
    ]
    assert user == returned
    assert len(updates) == 1
    assert "department = %s" in updates[0][0]
    assert "department_code = %s" in updates[0][0]
    assert "system_role" not in updates[0][0]
    assert "all_services_access" not in updates[0][0]
    assert updates[0][1] == (
        "변경된 표시 이름",
        "member@example.test",
        "변경된 합성 부서",
        "SYN002",
        returned.id,
    )


@pytest.mark.parametrize(
    ("fetchone_values", "expected_code", "expected_status"),
    [
        ([{"id": uuid4(), "username": "seelis.member", "is_active": False}], "account_inactive", 403),
        ([None, {"auth_provider": "local"}], "external_username_collision", 409),
    ],
)
def test_store_rejects_inactive_external_user_and_local_username_collision(
    fetchone_values: list[dict[str, Any] | None], expected_code: str, expected_status: int
) -> None:
    cursor = _ScriptedCursor(fetchone_values)
    repository = PostgresAuthStore(
        _settings(
            auth_db_host="db.example.test",
            auth_db_name="synthetic",
            auth_db_user="writer",
            auth_db_password="secret",
        ),
        connect=lambda **_kwargs: _ScriptedConnection(cursor),
    )
    repository._ready = True

    with pytest.raises(AuthStoreError) as raised:
        repository.upsert_seelis_user(
            subject="synthetic-subject",
            username="seelis.member",
            email="member@example.test",
            display_name="합성 SeeLIS 사용자",
            department="합성 부서",
            department_code="SYN001",
        )

    assert raised.value.code == expected_code
    assert raised.value.status_code == expected_status


def test_local_login_query_filters_auth_provider() -> None:
    cursor = _ScriptedCursor([None])
    repository = PostgresAuthStore(
        _settings(
            auth_db_host="db.example.test",
            auth_db_name="synthetic",
            auth_db_user="writer",
            auth_db_password="secret",
        ),
        connect=lambda **_kwargs: _ScriptedConnection(cursor),
    )
    repository._ready = True

    assert repository.get_login_record("seelis.member") is None
    assert "auth_provider = 'local'" in cursor.statements[0][0]


def test_seelis_api_success_returns_existing_envelope_and_httponly_service_cookie() -> None:
    repository = _FakeRepository()
    authenticator = _FakeAuthenticator()
    with _api_client(repository, authenticator) as client:
        response = client.post(
            "/api/auth/seelis-login",
            json={"userId": "seelis.member", "pswd": "synthetic-password"},
        )

        assert response.status_code == 200
        assert response.json()["user"]["username"] == "seelis.member"
        assert set(response.json()) == {"user"}
        assert "httponly" in response.headers["set-cookie"].casefold()
        assert "samesite=lax" in response.headers["set-cookie"].casefold()
        assert client.get("/api/auth/me").status_code == 200
    assert repository.upsert_calls == 1
    assert repository.upsert_values["department"] == "합성 부서"
    assert repository.upsert_values["department_code"] == "SYN001"


def test_seelis_api_does_not_mutate_store_when_external_auth_fails() -> None:
    repository = _FakeRepository()
    authenticator = _FakeAuthenticator(
        AuthStoreError("invalid_credentials", "SeeLIS 사용자 ID 또는 비밀번호가 올바르지 않습니다.", 401)
    )
    with _api_client(repository, authenticator) as client:
        response = client.post(
            "/api/auth/seelis-login",
            json={"userId": "seelis.member", "pswd": "synthetic-password"},
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"
    assert repository.upsert_calls == 0
    assert repository.sessions == set()


def test_seelis_api_rejects_missing_input_and_limits_repeated_failures() -> None:
    repository = _FakeRepository()
    authenticator = _FakeAuthenticator(AuthStoreError("invalid_credentials", "인증 실패", 401))
    with _api_client(repository, authenticator) as client:
        missing = client.post("/api/auth/seelis-login", json={"userId": "seelis.member"})
        failures = [
            client.post(
                "/api/auth/seelis-login",
                json={"userId": "seelis.member", "pswd": "synthetic-password"},
            )
            for _ in range(6)
        ]

    assert missing.status_code == 422
    assert [response.status_code for response in failures] == [401, 401, 401, 401, 401, 429]
    assert authenticator.calls == 5


@pytest.mark.parametrize(
    ("store_error", "expected_status"),
    [
        (AuthStoreError("external_username_collision", "자동 연결 불가", 409), 409),
        (AuthStoreError("account_inactive", "비활성화된 계정입니다.", 403), 403),
    ],
)
def test_seelis_api_preserves_store_collision_and_inactive_contracts(
    store_error: AuthStoreError, expected_status: int
) -> None:
    repository = _FakeRepository()
    repository.upsert_error = store_error
    with _api_client(repository, _FakeAuthenticator()) as client:
        response = client.post(
            "/api/auth/seelis-login",
            json={"userId": "seelis.member", "pswd": "synthetic-password"},
        )

    assert response.status_code == expected_status
    assert repository.sessions == set()
