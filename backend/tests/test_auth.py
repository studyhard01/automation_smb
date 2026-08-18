"""PostgreSQL 없이 인증·권한 API의 공개 계약과 보안 경계를 검증한다."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from smb_finder.auth.api import create_auth_router
from smb_finder.auth.config import AuthSettings
from smb_finder.auth.models import RegisterRequest, ServiceSummary, UserResponse, UserUpdateRequest
from smb_finder.auth.security import hash_password, hash_session_token, verify_password
from smb_finder.auth.service import AuthService
from smb_finder.auth.store import AuthStoreError, PostgresAuthStore


def _user(
    *,
    role: str = "user",
    superuser: bool = False,
    all_access: bool = False,
    department: str | None = None,
    department_code: str | None = None,
) -> UserResponse:
    now = datetime.now(timezone.utc)
    return UserResponse(
        id=uuid4(),
        username="admin" if role == "admin" else "member",
        email="synthetic@example.test",
        display_name="합성 사용자",
        department=department,
        department_code=department_code,
        system_role=role,
        is_superuser=superuser,
        is_active=True,
        all_services_access=all_access,
        service_keys=[] if all_access else ["playground"],
        created_at=now,
        updated_at=now,
    )


class FakeAuthRepository:
    """HTTP와 정책만 검증하는 인메모리 저장소."""

    def __init__(self, user: UserResponse, password: str = "synthetic-password") -> None:
        self.user = user
        self.password_hash = hash_password(password, iterations=10)
        self.sessions: set[str] = set()
        self.created_password_hash = ""
        self.failures = 0

    def create_user(self, *, username: str, email: str, display_name: str, password_hash: str) -> UserResponse:
        self.created_password_hash = password_hash
        self.user = self.user.model_copy(
            update={"username": username, "email": email, "display_name": display_name, "service_keys": []}
        )
        return self.user

    def get_login_record(self, username: str) -> dict[str, object] | None:
        if username != self.user.username:
            return None
        return {
            "id": self.user.id,
            "password_hash": self.password_hash,
            "is_active": self.user.is_active,
            "failed_login_attempts": self.failures,
            "locked_until": None,
        }

    def record_login_failure(self, _user_id: UUID) -> None:
        self.failures += 1

    def record_login_success(self, _user_id: UUID) -> None:
        self.failures = 0

    def create_session(self, _user_id: UUID, token_hash: str, _ttl_seconds: int) -> None:
        self.sessions.add(token_hash)

    def get_user_by_session(self, token_hash: str) -> UserResponse | None:
        return self.user if token_hash in self.sessions and self.user.is_active else None

    def revoke_session(self, token_hash: str) -> None:
        self.sessions.discard(token_hash)

    def get_user(self, user_id: UUID) -> UserResponse | None:
        return self.user if user_id == self.user.id else None

    def list_users(self) -> tuple[list[UserResponse], list[ServiceSummary]]:
        return [self.user], [ServiceSummary(key="playground", name="Playground")]

    def update_user(self, _actor: UserResponse, target_id: UUID, request: UserUpdateRequest) -> UserResponse:
        if target_id != self.user.id:
            raise AuthStoreError("user_not_found", "사용자를 찾을 수 없습니다.", 404)
        self.user = self.user.model_copy(
            update={
                "system_role": request.system_role,
                "is_superuser": request.is_superuser,
                "is_active": request.is_active,
                "all_services_access": request.all_services_access,
                "service_keys": [] if request.all_services_access else request.service_keys,
            }
        )
        return self.user


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object, parameters: object = None) -> None:
        rendered = statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        self.statements.append((rendered, parameters))


class _RecordingConnection:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self, **_kwargs: object) -> _RecordingCursor:
        return self._cursor


def _client(repository: FakeAuthRepository) -> TestClient:
    settings = AuthSettings(
        auth_session_cookie_name="test_session",
        auth_session_ttl_seconds=3600,
        auth_cookie_secure=False,
    )
    service = AuthService(repository, session_ttl_seconds=3600)
    app = FastAPI()
    app.include_router(create_auth_router(settings, service))
    return TestClient(app)


def test_password_hash_uses_pbkdf2_and_never_contains_plaintext() -> None:
    encoded = hash_password("synthetic-password", iterations=20)

    assert encoded.startswith("pbkdf2_sha256$20$")
    assert "synthetic-password" not in encoded
    assert verify_password("synthetic-password", encoded) is True
    assert verify_password("wrong-password", encoded) is False
    assert verify_password("synthetic-password", "invalid-format") is False


def test_repository_initialization_creates_normalized_auth_tables_and_seed_service() -> None:
    cursor = _RecordingCursor()
    settings = AuthSettings(
        auth_db_host="db.example.test",
        auth_db_name="synthetic",
        auth_db_user="synthetic-writer",
        auth_db_password="synthetic-secret",
    )
    repository = PostgresAuthStore(settings, connect=lambda **_kwargs: _RecordingConnection(cursor))

    repository.initialize(initial_password_hash="synthetic-password-hash")

    rendered = "\n".join(statement for statement, _parameters in cursor.statements)
    assert repository.ready is True
    assert 'CREATE SCHEMA IF NOT EXISTS "auth"' in rendered
    for table in ("users", "services", "user_service_permissions", "sessions"):
        assert f'CREATE TABLE IF NOT EXISTS "auth".{table}' in rendered
    assert "Playground" in rendered
    assert "auth_provider varchar(32) NOT NULL DEFAULT 'local'" in rendered
    assert "external_subject varchar(255)" in rendered
    assert "department varchar(100)" in rendered
    assert "department_code varchar(32)" in rendered
    assert "users_provider_subject_uidx" in rendered
    assert "synthetic-password-hash" not in rendered


def test_auth_settings_reuses_existing_credentials_only_with_explicit_opt_in() -> None:
    default = AuthSettings(
        llmops_db_host="canonical-db.example.test",
        dev_server="http://fallback.example.test",
        auth_db_name="synthetic",
        postgres_user="existing-user",
        postgres_password="existing-password",
        auth_db_use_llmops_credentials=False,
    )
    opted_in = default.model_copy(update={"auth_db_use_llmops_credentials": True})

    assert default.configured is False
    assert opted_in.configured is True
    assert opted_in.effective_host == "canonical-db.example.test"
    assert opted_in.effective_user == "existing-user"
    assert opted_in.effective_password == "existing-password"


def test_register_creates_deny_by_default_user_without_returning_password() -> None:
    repository = FakeAuthRepository(_user())
    service = AuthService(repository, session_ttl_seconds=3600)

    user = service.register(
        RegisterRequest(
            username="new.member",
            display_name="새 사용자",
            email="NEW@EXAMPLE.TEST",
            password="synthetic-new-password",
        )
    )

    assert user.system_role == "user"
    assert user.all_services_access is False
    assert user.service_keys == []
    assert repository.created_password_hash.startswith("pbkdf2_sha256$600000$")
    assert "synthetic-new-password" not in repository.created_password_hash
    assert "password" not in user.model_dump()


def test_login_me_logout_uses_httponly_same_site_server_session_cookie() -> None:
    repository = FakeAuthRepository(_user(role="admin", superuser=True, all_access=True))
    with _client(repository) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "synthetic-password"})

        assert login.status_code == 200
        assert login.json()["user"]["username"] == "admin"
        cookie = login.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert repository.sessions == {hash_session_token(client.cookies["test_session"])}
        assert client.get("/api/auth/me").status_code == 200

        logout = client.post("/api/auth/logout")

        assert logout.status_code == 204
        assert repository.sessions == set()
        assert client.get("/api/auth/me").status_code == 401


def test_user_management_is_admin_only_and_returns_service_catalog() -> None:
    member_repository = FakeAuthRepository(_user())
    with _client(member_repository) as client:
        client.post("/api/auth/login", json={"username": "member", "password": "synthetic-password"})
        denied = client.get("/api/users")

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "admin_required"

    admin_repository = FakeAuthRepository(
        _user(
            role="admin",
            superuser=True,
            all_access=True,
            department="합성 부서",
            department_code="SYN001",
        )
    )
    with _client(admin_repository) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "synthetic-password"})
        response = client.get("/api/users")

    assert response.status_code == 200
    assert response.json()["users"][0]["department"] == "합성 부서"
    assert response.json()["users"][0]["department_code"] == "SYN001"
    assert response.json()["services"] == [
        {"key": "playground", "name": "Playground", "description": "", "is_active": True}
    ]


def test_missing_auth_store_returns_service_unavailable_without_affecting_app() -> None:
    repository = FakeAuthRepository(_user())

    def unavailable(_username: str) -> dict[str, object] | None:
        raise AuthStoreError("auth_unavailable", "인증 저장소를 사용할 수 없습니다.", 503)

    repository.get_login_record = unavailable  # type: ignore[method-assign]
    with _client(repository) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "member", "password": "synthetic-password"},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "auth_unavailable"


def test_superuser_must_also_have_admin_role() -> None:
    with pytest.raises(ValidationError):
        UserUpdateRequest(
            system_role="user",
            is_superuser=True,
            is_active=True,
            all_services_access=True,
            service_keys=[],
        )
