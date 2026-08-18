"""SeeLIS 계정 인증을 외부 시스템에 위임하는 최소 정보 클라이언트."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .config import AuthSettings
from .models import EMAIL_PATTERN
from .store import AuthStoreError

_logger = logging.getLogger(__name__)
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


@dataclass(frozen=True, slots=True)
class SeeLisIdentity:
    """외부 토큰을 제거하고 로컬 계정 연결에 필요한 값만 보존한다."""

    subject: str
    username: str
    display_name: str
    email: str | None
    department: str | None
    department_code: str | None


class SeeLisClient:
    """TLS 검증과 제한 시간을 강제하는 재사용 가능한 SeeLIS 인증 클라이언트."""

    def __init__(self, settings: AuthSettings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()
        # httpx의 INFO request log에는 대상 URL이 포함되므로 인증 클라이언트 사용 시 비활성화한다.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    def close(self) -> None:
        """소유한 연결 풀을 닫고 다음 수명주기에서 다시 만들 수 있게 한다."""

        with self._lock:
            if self._client is not None and self._owns_client:
                self._client.close()
                self._client = None

    def authenticate(self, user_id: str, password: str) -> SeeLisIdentity:
        """토큰 발급과 userinfo 검증이 모두 성공한 최소 사용자 식별자를 반환한다."""

        started = time.perf_counter()
        outcome = "success"
        try:
            self._validate_configuration()
            access_token, display_name, email, department, department_code = self._request_access_token(user_id, password)
            subject = self._request_userinfo(access_token, user_id)
            return SeeLisIdentity(
                subject=subject,
                username=user_id,
                display_name=display_name or user_id,
                email=email,
                department=department,
                department_code=department_code,
            )
        except AuthStoreError as exc:
            outcome = exc.code
            raise
        finally:
            self._log_timing("total", started, outcome)

    def _request_access_token(
        self,
        user_id: str,
        password: str,
    ) -> tuple[str, str | None, str | None, str | None, str | None]:
        started = time.perf_counter()
        outcome = "success"
        try:
            response = self._get_client().post(
                self.settings.seelis_login_token_api_url.strip(),
                headers={
                    self.settings.seelis_login_token_api_url_key.strip(): self.settings.seelis_login_token_api_url_value
                },
                json={"userId": user_id, "pswd": password},
                timeout=self.settings.seelis_login_token_api_timeout_ms / 1000,
            )
            if response.status_code != 201:
                raise AuthStoreError("invalid_credentials", "SeeLIS 사용자 ID 또는 비밀번호가 올바르지 않습니다.", 401)
            payload = self._response_json(response)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise self._invalid_response()
            access_token = self._required_text(result, "accessToken", max_length=16_384)
            display_name = self._optional_text(result, "userNm", max_length=100)
            department = self._optional_text(result, "deptNm", max_length=100)
            department_code = self._optional_text(result, "deptCd", max_length=32)
            primary_email = self._optional_text(result, "emalAddr", max_length=254)
            alternate_email = self._optional_text(result, "extnEmalAddr", max_length=254)
            email = primary_email or alternate_email
            if email is not None:
                email = email.casefold()
                if not EMAIL_PATTERN.fullmatch(email):
                    raise self._invalid_response()
            return access_token, display_name, email, department, department_code
        except AuthStoreError as exc:
            outcome = exc.code
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            outcome = "seelis_unavailable"
            raise AuthStoreError("seelis_unavailable", "SeeLIS 인증 서비스를 사용할 수 없습니다.", 503) from exc
        finally:
            self._log_timing("token", started, outcome)

    def _request_userinfo(self, access_token: str, user_id: str) -> str:
        started = time.perf_counter()
        outcome = "success"
        try:
            header_value = self.settings.seelis_login_keycloak_url_value.replace("{token}", access_token)
            response = self._get_client().get(
                self.settings.seelis_login_keycloak_url.strip(),
                headers={self.settings.seelis_login_keycloak_url_key.strip(): header_value},
                timeout=self.settings.seelis_login_keycloak_timeout_ms / 1000,
            )
            if response.status_code != 200:
                raise AuthStoreError("invalid_credentials", "SeeLIS 사용자 인증을 확인할 수 없습니다.", 401)
            payload = self._response_json(response)
            subject = self._required_text(payload, "sub", max_length=255)
            preferred_username = self._optional_text(payload, "preferred_username", max_length=64)
            if preferred_username is not None and preferred_username.casefold() != user_id.casefold():
                raise AuthStoreError("external_identity_mismatch", "SeeLIS 사용자 인증을 확인할 수 없습니다.", 401)
            return subject
        except AuthStoreError as exc:
            outcome = exc.code
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            outcome = "seelis_unavailable"
            raise AuthStoreError("seelis_unavailable", "SeeLIS 인증 서비스를 사용할 수 없습니다.", 503) from exc
        finally:
            self._log_timing("userinfo", started, outcome)

    def _validate_configuration(self) -> None:
        if not self.settings.seelis_login_configured:
            raise AuthStoreError("seelis_not_configured", "SeeLIS 로그인이 구성되지 않았습니다.", 503)
        urls = (self.settings.seelis_login_token_api_url, self.settings.seelis_login_keycloak_url)
        header_names = (
            self.settings.seelis_login_token_api_url_key,
            self.settings.seelis_login_keycloak_url_key,
        )
        if any(urlsplit(value.strip()).scheme.casefold() != "https" for value in urls):
            raise AuthStoreError("seelis_not_configured", "SeeLIS 로그인이 구성되지 않았습니다.", 503)
        if any(not _HEADER_NAME_PATTERN.fullmatch(value.strip()) for value in header_names):
            raise AuthStoreError("seelis_not_configured", "SeeLIS 로그인이 구성되지 않았습니다.", 503)
        if self.settings.seelis_login_keycloak_url_value.count("{token}") != 1:
            raise AuthStoreError("seelis_not_configured", "SeeLIS 로그인이 구성되지 않았습니다.", 503)

    def _get_client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(verify=True, follow_redirects=False)
            return self._client

    @staticmethod
    def _response_json(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SeeLisClient._invalid_response() from exc
        if not isinstance(payload, dict):
            raise SeeLisClient._invalid_response()
        return payload

    @staticmethod
    def _required_text(payload: dict[str, object], key: str, *, max_length: int) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise SeeLisClient._invalid_response()
        normalized = value.strip()
        if not normalized or len(normalized) > max_length:
            raise SeeLisClient._invalid_response()
        return normalized

    @staticmethod
    def _optional_text(payload: dict[str, object], key: str, *, max_length: int) -> str | None:
        value = payload.get(key)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise SeeLisClient._invalid_response()
        normalized = value.strip()
        if not normalized or len(normalized) > max_length:
            raise SeeLisClient._invalid_response()
        return normalized

    @staticmethod
    def _invalid_response() -> AuthStoreError:
        return AuthStoreError("seelis_invalid_response", "SeeLIS 인증 응답 형식이 올바르지 않습니다.", 502)

    @staticmethod
    def _log_timing(stage: str, started: float, outcome: str) -> None:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _logger.info("SeeLIS 인증: stage=%s elapsed_ms=%.1f outcome=%s", stage, elapsed_ms, outcome)
