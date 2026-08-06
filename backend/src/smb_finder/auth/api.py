"""로그인·회원가입·관리자 사용자 관리 FastAPI router."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from .config import AuthSettings
from .models import (
    LoginRequest,
    RegisterRequest,
    UserEnvelope,
    UserResponse,
    UsersEnvelope,
    UserUpdateRequest,
)
from .service import AuthService
from .store import AuthStoreError


def create_auth_router(settings: AuthSettings, service: AuthService) -> APIRouter:
    """인증 service를 주입받아 HTTP 계약을 구성한다."""

    router = APIRouter(prefix="/api", tags=["authentication"])

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
