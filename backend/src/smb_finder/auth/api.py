"""로그인·회원가입·관리자 사용자 관리 FastAPI router."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from .config import AuthSettings
from .models import (
    LoginRequest,
    RegisterRequest,
    SeeLisLoginRequest,
    UserEnvelope,
    UserResponse,
    UsersEnvelope,
    UserUpdateRequest,
)
from .service import AuthService
from .store import AuthStoreError


class _SeeLisAttemptLimiter:
    """식별자 원문을 보존하지 않는 process-local SeeLIS 로그인 시도 제한기."""

    def __init__(self, *, max_attempts: int = 5, lockout_seconds: int = 15 * 60, max_entries: int = 2048) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.max_entries = max_entries
        self._records: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(user_id: str, client_host: str) -> str:
        return hashlib.sha256(f"{client_host}\0{user_id.casefold()}".encode()).hexdigest()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            attempts, expires_at = record
            if expires_at <= now:
                self._records.pop(key, None)
                return
            if attempts >= self.max_attempts:
                raise AuthStoreError("seelis_login_rate_limited", "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.", 429)

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            attempts, expires_at = self._records.pop(key, (0, 0.0))
            if expires_at <= now:
                attempts = 0
            attempts += 1
            self._records[key] = (attempts, now + self.lockout_seconds)
            while len(self._records) > self.max_entries:
                self._records.popitem(last=False)

    def clear(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)


def create_auth_router(settings: AuthSettings, service: AuthService) -> APIRouter:
    """인증 service를 주입받아 HTTP 계약을 구성한다."""

    router = APIRouter(prefix="/api", tags=["authentication"])
    seelis_attempts = _SeeLisAttemptLimiter()

    def session_token(request: Request) -> str | None:
        return request.cookies.get(settings.auth_session_cookie_name)

    async def current_user(request: Request) -> UserResponse:
        try:
            return await run_in_threadpool(service.current_user, session_token(request))
        except AuthStoreError as exc:
            raise _http_error(exc) from exc

    @router.post(
        "/auth/register",
        response_model=UserEnvelope,
        status_code=status.HTTP_201_CREATED,
        operation_id="register_user",
        summary="회원가입",
    )
    async def register(request: RegisterRequest) -> UserEnvelope:
        try:
            user = await run_in_threadpool(service.register, request)
            return UserEnvelope(user=user)
        except AuthStoreError as exc:
            raise _http_error(exc) from exc

    @router.post(
        "/auth/login",
        response_model=UserEnvelope,
        operation_id="login_user",
        summary="로그인",
    )
    async def login(request: LoginRequest, response: Response) -> UserEnvelope:
        try:
            user, token = await run_in_threadpool(service.login, request.username, request.password)
        except AuthStoreError as exc:
            raise _http_error(exc) from exc
        response.set_cookie(
            settings.auth_session_cookie_name,
            token,
            httponly=True,
            secure=settings.auth_cookie_secure,
            samesite="lax",
            path="/",
        )
        return UserEnvelope(user=user)

    @router.post(
        "/auth/seelis-login",
        response_model=UserEnvelope,
        operation_id="login_user_with_seelis",
        summary="SeeLIS 계정 로그인",
    )
    async def seelis_login(payload: SeeLisLoginRequest, request: Request, response: Response) -> UserEnvelope:
        client_host = request.client.host if request.client else "unknown"
        attempt_key = seelis_attempts.key(payload.user_id, client_host)
        try:
            seelis_attempts.check(attempt_key)
            user, token = await run_in_threadpool(service.login_with_seelis, payload.user_id, payload.password)
        except AuthStoreError as exc:
            if exc.status_code == status.HTTP_401_UNAUTHORIZED:
                seelis_attempts.record_failure(attempt_key)
            raise _http_error(exc) from exc
        seelis_attempts.clear(attempt_key)
        response.set_cookie(
            settings.auth_session_cookie_name,
            token,
            httponly=True,
            secure=settings.auth_cookie_secure,
            samesite="lax",
            path="/",
        )
        return UserEnvelope(user=user)

    @router.get(
        "/auth/me",
        response_model=UserEnvelope,
        operation_id="get_current_user",
        summary="현재 로그인 사용자",
    )
    async def me(user: UserResponse = Depends(current_user)) -> UserEnvelope:
        return UserEnvelope(user=user)

    @router.post(
        "/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="logout_user",
        summary="로그아웃",
    )
    async def logout(request: Request, response: Response) -> Response:
        try:
            await run_in_threadpool(service.logout, session_token(request))
        except AuthStoreError as exc:
            raise _http_error(exc) from exc
        response.delete_cookie(
            settings.auth_session_cookie_name,
            httponly=True,
            secure=settings.auth_cookie_secure,
            samesite="lax",
            path="/",
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get(
        "/users",
        response_model=UsersEnvelope,
        operation_id="list_users",
        summary="사용자·서비스 권한 목록",
    )
    async def list_users(user: UserResponse = Depends(current_user)) -> UsersEnvelope:
        try:
            users, services = await run_in_threadpool(service.list_users, user)
            return UsersEnvelope(users=users, services=services)
        except AuthStoreError as exc:
            raise _http_error(exc) from exc

    @router.patch(
        "/users/{user_id}",
        response_model=UserEnvelope,
        operation_id="update_user_access",
        summary="사용자 역할·서비스 접근 권한 변경",
    )
    async def update_user(
        user_id: UUID,
        request: UserUpdateRequest,
        actor: UserResponse = Depends(current_user),
    ) -> UserEnvelope:
        try:
            user = await run_in_threadpool(service.update_user, actor, user_id, request)
            return UserEnvelope(user=user)
        except AuthStoreError as exc:
            raise _http_error(exc) from exc

    return router


def _http_error(exc: AuthStoreError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})
