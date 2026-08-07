"""비밀번호 검증과 deny-by-default 권한 정책을 담당하는 인증 서비스."""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Protocol
from uuid import UUID

from .models import RegisterRequest, ServiceSummary, UserResponse, UserUpdateRequest
from .seelis import SeeLisIdentity
from .security import hash_password, hash_session_token, new_session_token, verify_password
from .store import AuthStoreError

_logger = logging.getLogger(__name__)


class AuthRepository(Protocol):
    """단위 테스트에서 PostgreSQL 없이 검증하기 위한 저장소 경계."""

    def create_user(self, *, username: str, email: str, display_name: str, password_hash: str) -> UserResponse: ...
    def get_login_record(self, username: str) -> dict[str, object] | None: ...
    def upsert_seelis_user(
        self,
        *,
        subject: str,
        username: str,
        email: str | None,
        display_name: str,
        department: str | None,
        department_code: str | None,
    ) -> UserResponse: ...
    def record_login_failure(self, user_id: UUID) -> None: ...
    def record_login_success(self, user_id: UUID) -> None: ...
    def create_session(self, user_id: UUID, token_hash: str, ttl_seconds: int) -> None: ...
    def get_user_by_session(self, token_hash: str) -> UserResponse | None: ...
    def revoke_session(self, token_hash: str) -> None: ...
    def get_user(self, user_id: UUID) -> UserResponse | None: ...
    def list_users(self) -> tuple[list[UserResponse], list[ServiceSummary]]: ...
    def update_user(self, actor: UserResponse, target_id: UUID, request: UserUpdateRequest) -> UserResponse: ...


class SeeLisAuthenticator(Protocol):
    """SeeLIS 외부 인증 구현을 service 테스트에서 대체하는 경계."""

    def authenticate(self, user_id: str, password: str) -> SeeLisIdentity: ...


class AuthService:
    """Frontend와 DB 사이의 인증·권한 정책 계층."""

    def __init__(
        self,
        repository: AuthRepository,
        *,
        session_ttl_seconds: int,
        seelis_authenticator: SeeLisAuthenticator | None = None,
    ) -> None:
        self.repository = repository
        self.session_ttl_seconds = session_ttl_seconds
        self.seelis_authenticator = seelis_authenticator

    def register(self, request: RegisterRequest) -> UserResponse:
        """권한 없는 활성 일반 사용자로 가입한다."""

        return self.repository.create_user(
            username=request.username,
            email=request.email,
            display_name=request.display_name,
            password_hash=hash_password(request.password),
        )

    def login(self, username: str, password: str) -> tuple[UserResponse, str]:
        """계정 상태와 비밀번호를 확인하고 원문 session token을 한 번 반환한다."""

        record = self.repository.get_login_record(username)
        if record is None:
            verify_password(password, _dummy_password_hash())
            raise AuthStoreError("invalid_credentials", "사용자 이름 또는 비밀번호가 올바르지 않습니다.", 401)
        user_id = UUID(str(record["id"]))
        locked_until = record.get("locked_until")
        if locked_until is not None:
            from datetime import datetime, timezone

            if locked_until > datetime.now(timezone.utc):
                raise AuthStoreError("account_locked", "로그인 실패가 반복되어 잠시 잠겼습니다.", 423)
        if not verify_password(password, str(record["password_hash"])):
            self.repository.record_login_failure(user_id)
            raise AuthStoreError("invalid_credentials", "사용자 이름 또는 비밀번호가 올바르지 않습니다.", 401)
        if not bool(record["is_active"]):
            raise AuthStoreError("account_inactive", "비활성화된 계정입니다.", 403)
        self.repository.record_login_success(user_id)
        token = new_session_token()
        self.repository.create_session(user_id, hash_session_token(token), self.session_ttl_seconds)
        user = self.repository.get_user(user_id)
        if user is None:  # pragma: no cover
            raise AuthStoreError("auth_unavailable", "로그인한 사용자를 확인할 수 없습니다.", 503)
        return user, token

    def login_with_seelis(self, user_id: str, password: str) -> tuple[UserResponse, str]:
        """SeeLIS 검증을 모두 마친 뒤에만 로컬 사용자와 자체 session을 만든다."""

        started = time.perf_counter()
        outcome = "success"
        try:
            if self.seelis_authenticator is None:
                raise AuthStoreError("seelis_not_configured", "SeeLIS 로그인이 구성되지 않았습니다.", 503)
            identity = self.seelis_authenticator.authenticate(user_id, password)
            user = self.repository.upsert_seelis_user(
                subject=identity.subject,
                username=identity.username,
                email=identity.email,
                display_name=identity.display_name,
                department=identity.department,
                department_code=identity.department_code,
            )
            token = new_session_token()
            self.repository.create_session(user.id, hash_session_token(token), self.session_ttl_seconds)
            return user, token
        except AuthStoreError as exc:
            outcome = exc.code
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            _logger.info("SeeLIS 로그인: stage=total elapsed_ms=%.1f outcome=%s", elapsed_ms, outcome)

    def current_user(self, token: str | None) -> UserResponse:
        """cookie의 session으로 현재 활성 사용자를 반환한다."""

        if not token:
            raise AuthStoreError("authentication_required", "로그인이 필요합니다.", 401)
        user = self.repository.get_user_by_session(hash_session_token(token))
        if user is None:
            raise AuthStoreError("invalid_session", "로그인 세션이 만료되었거나 유효하지 않습니다.", 401)
        return user

    def logout(self, token: str | None) -> None:
        """cookie가 있으면 해당 session을 취소한다."""

        if token:
            self.repository.revoke_session(hash_session_token(token))

    def list_users(self, actor: UserResponse) -> tuple[list[UserResponse], list[ServiceSummary]]:
        """관리자에게만 사용자 목록을 공개한다."""

        self._require_admin(actor)
        return self.repository.list_users()

    def update_user(self, actor: UserResponse, target_id: UUID, request: UserUpdateRequest) -> UserResponse:
        """관리자 확인 뒤 DB의 transaction 안전 정책으로 변경한다."""

        self._require_admin(actor)
        return self.repository.update_user(actor, target_id, request)

    @staticmethod
    def _require_admin(user: UserResponse) -> None:
        if not user.is_active or (user.system_role != "admin" and not user.is_superuser):
            raise AuthStoreError("admin_required", "관리자 권한이 필요합니다.", 403)


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """존재하지 않는 사용자도 동일한 PBKDF2 작업을 수행하도록 만든다."""

    return hash_password("not-a-real-user-password")
