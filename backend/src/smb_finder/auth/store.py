"""인증 schema를 소유하는 PostgreSQL 저장소."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .config import AuthSettings
from .models import ServiceSummary, UserResponse, UserUpdateRequest

_logger = logging.getLogger(__name__)


class AuthStoreError(RuntimeError):
    """내부 정보 없이 API로 변환할 수 있는 인증 저장소 오류."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class PostgresAuthStore:
    """기존 read-only adapter와 연결·schema를 공유하지 않는 인증 저장소."""

    def __init__(self, settings: AuthSettings, *, connect: Callable[..., Any] | None = None) -> None:
        self.settings = settings
        self._connect = connect or psycopg.connect
        self._ready = False

    @property
    def ready(self) -> bool:
        """schema 초기화 성공 여부를 반환한다."""

        return self._ready

    def close(self) -> None:
        """요청별 연결 저장소를 비준비 상태로 전환한다."""

        self._ready = False

    def initialize(self, *, initial_password_hash: str | None = None) -> None:
        """전용 schema와 표준 사용자·서비스·권한·session table을 idempotent하게 만든다."""

        if not self.settings.configured:
            self._ready = False
            return
        schema = sql.Identifier(self.settings.auth_db_schema)
        statements = [
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema),
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.users (
                    id uuid PRIMARY KEY,
                    username varchar(64) NOT NULL,
                    email varchar(254),
                    display_name varchar(100) NOT NULL,
                    password_hash text NOT NULL,
                    system_role varchar(32) NOT NULL DEFAULT 'user'
                        CHECK (system_role IN ('admin', 'user')),
                    is_superuser boolean NOT NULL DEFAULT false,
                    is_active boolean NOT NULL DEFAULT true,
                    all_services_access boolean NOT NULL DEFAULT false,
                    failed_login_attempts integer NOT NULL DEFAULT 0 CHECK (failed_login_attempts >= 0),
                    locked_until timestamptz,
                    last_login_at timestamptz,
                    password_changed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    deleted_at timestamptz
                )
                """
            ).format(schema),
            sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_uidx ON {}.users (lower(username))").format(
                schema
            ),
            sql.SQL(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uidx ON {}.users (lower(email)) "
                "WHERE email IS NOT NULL"
            ).format(schema),
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.services (
                    id uuid PRIMARY KEY,
                    service_key varchar(64) NOT NULL UNIQUE,
                    display_name varchar(100) NOT NULL,
                    description varchar(500) NOT NULL DEFAULT '',
                    is_active boolean NOT NULL DEFAULT true,
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            ).format(schema),
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.user_service_permissions (
                    user_id uuid NOT NULL REFERENCES {}.users(id) ON DELETE CASCADE,
                    service_id uuid NOT NULL REFERENCES {}.services(id) ON DELETE CASCADE,
                    can_access boolean NOT NULL DEFAULT true,
                    granted_by uuid REFERENCES {}.users(id) ON DELETE SET NULL,
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, service_id)
                )
                """
            ).format(schema, schema, schema, schema),
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {}.sessions (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL REFERENCES {}.users(id) ON DELETE CASCADE,
                    token_hash char(64) NOT NULL UNIQUE,
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at timestamptz NOT NULL,
                    last_seen_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    revoked_at timestamptz
                )
                """
            ).format(schema, schema),
            sql.SQL("CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON {}.sessions (user_id)").format(schema),
            sql.SQL("CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON {}.sessions (expires_at)").format(schema),
        ]
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor() as cursor:
                    for statement in statements:
                        cursor.execute(statement)
                    cursor.execute(
                        sql.SQL(
                            "INSERT INTO {}.services (id, service_key, display_name, description) "
                            "VALUES (%s, 'playground', 'Playground', '공유 문서 검색·대화 서비스') "
                            "ON CONFLICT (service_key) DO UPDATE SET "
                            "display_name = EXCLUDED.display_name, description = EXCLUDED.description, "
                            "updated_at = CURRENT_TIMESTAMP"
                        ).format(schema),
                        (uuid4(),),
                    )
                    if initial_password_hash:
                        cursor.execute(
                            sql.SQL(
                                """
                                INSERT INTO {}.users (
                                    id, username, email, display_name, password_hash, system_role,
                                    is_superuser, is_active, all_services_access
                                )
                                VALUES (%s, %s, %s, %s, %s, 'admin', true, true, true)
                                ON CONFLICT DO NOTHING
                                """
                            ).format(schema),
                            (
                                uuid4(),
                                self.settings.auth_initial_admin_username.strip() or "admin",
                                self.settings.auth_initial_admin_email.strip().casefold() or None,
                                self.settings.auth_initial_admin_display_name.strip() or "최고 관리자",
                                initial_password_hash,
                            ),
                        )
            self._ready = True
        except Exception as exc:
            self._ready = False
            _logger.warning("인증 PostgreSQL 초기화 실패: type=%s", type(exc).__name__)
            raise AuthStoreError("auth_unavailable", "인증 저장소를 사용할 수 없습니다.", 503) from exc

    def create_user(
        self,
        *,
        username: str,
        email: str,
        display_name: str,
        password_hash: str,
    ) -> UserResponse:
        """기본 권한이 없는 일반 사용자를 생성한다."""

        self._ensure_ready()
        user_id = uuid4()
        schema = sql.Identifier(self.settings.auth_db_schema)
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            """
                            INSERT INTO {}.users (id, username, email, display_name, password_hash)
                            VALUES (%s, %s, %s, %s, %s)
                            """
                        ).format(schema),
                        (user_id, username, email, display_name, password_hash),
                    )
        except psycopg.errors.UniqueViolation as exc:
            raise AuthStoreError("user_already_exists", "이미 사용 중인 사용자 이름 또는 이메일입니다.", 409) from exc
        except AuthStoreError:
            raise
        except Exception as exc:
            raise self._unavailable(exc) from exc
        user = self.get_user(user_id)
        if user is None:  # pragma: no cover - commit 직후 DB 이상 방어
            raise AuthStoreError("auth_unavailable", "생성된 사용자를 확인할 수 없습니다.", 503)
        return user

    def get_login_record(self, username: str) -> dict[str, Any] | None:
        """로그인 검증에 필요한 최소 내부 필드를 조회한다."""

        self._ensure_ready()
        query = sql.SQL(
            "SELECT id, password_hash, is_active, failed_login_attempts, locked_until "
            "FROM {}.users WHERE deleted_at IS NULL AND lower(username) = lower(%s)"
        ).format(sql.Identifier(self.settings.auth_db_schema))
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, (username.strip(),))
                    return cursor.fetchone()
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def record_login_failure(self, user_id: UUID) -> None:
        """실패 횟수를 올리고 5회부터 15분 잠금한다."""

        schema = sql.Identifier(self.settings.auth_db_schema)
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            """
                            UPDATE {}.users
                            SET failed_login_attempts = failed_login_attempts + 1,
                                locked_until = CASE WHEN failed_login_attempts + 1 >= 5
                                    THEN CURRENT_TIMESTAMP + INTERVAL '15 minutes' ELSE locked_until END,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """
                        ).format(schema),
                        (user_id,),
                    )
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def record_login_success(self, user_id: UUID) -> None:
        """성공 시 잠금 상태를 초기화하고 마지막 로그인 시각을 갱신한다."""

        schema = sql.Identifier(self.settings.auth_db_schema)
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL(
                            "UPDATE {}.users SET failed_login_attempts = 0, locked_until = NULL, "
                            "last_login_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = %s"
                        ).format(schema),
                        (user_id,),
                    )
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def create_session(self, user_id: UUID, token_hash: str, ttl_seconds: int) -> None:
        """원문 token 없이 server-side session을 저장한다."""

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("INSERT INTO {}.sessions (id, user_id, token_hash, expires_at) VALUES (%s, %s, %s, %s)").format(
                            sql.Identifier(self.settings.auth_db_schema)
                        ),
                        (uuid4(), user_id, token_hash, expires_at),
                    )
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def get_user_by_session(self, token_hash: str) -> UserResponse | None:
        """유효한 session과 활성 사용자를 한 번에 확인한다."""

        self._ensure_ready()
        schema = sql.Identifier(self.settings.auth_db_schema)
        query = sql.SQL(
            "SELECT u.* FROM {}.sessions AS session JOIN {}.users AS u ON u.id = session.user_id "
            "WHERE session.token_hash = %s AND session.revoked_at IS NULL "
            "AND session.expires_at > CURRENT_TIMESTAMP AND u.is_active IS TRUE AND u.deleted_at IS NULL"
        ).format(schema, schema)
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, (token_hash,))
                    row = cursor.fetchone()
                    if row:
                        cursor.execute(
                            sql.SQL("UPDATE {}.sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE token_hash = %s").format(
                                schema
                            ),
                            (token_hash,),
                        )
            return self.get_user(UUID(str(row["id"]))) if row else None
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def revoke_session(self, token_hash: str) -> None:
        """현재 session만 만료한다."""

        self._ensure_ready()
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("UPDATE {}.sessions SET revoked_at = CURRENT_TIMESTAMP WHERE token_hash = %s").format(
                            sql.Identifier(self.settings.auth_db_schema)
                        ),
                        (token_hash,),
                    )
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def get_user(self, user_id: UUID) -> UserResponse | None:
        """사용자와 직접 할당된 활성 서비스 key를 반환한다."""

        self._ensure_ready()
        row = self._fetch_users(user_id=user_id)
        return row[0] if row else None

    def list_users(self) -> tuple[list[UserResponse], list[ServiceSummary]]:
        """삭제되지 않은 사용자와 서비스 catalog를 반환한다."""

        self._ensure_ready()
        schema = sql.Identifier(self.settings.auth_db_schema)
        try:
            users = self._fetch_users()
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        sql.SQL(
                            "SELECT service_key AS key, display_name AS name, description, is_active "
                            "FROM {}.services ORDER BY display_name, service_key"
                        ).format(schema)
                    )
                    services = [ServiceSummary.model_validate(row) for row in cursor.fetchall()]
            return users, services
        except AuthStoreError:
            raise
        except Exception as exc:
            raise self._unavailable(exc) from exc

    def update_user(self, actor: UserResponse, target_id: UUID, request: UserUpdateRequest) -> UserResponse:
        """관리 권한·서비스 할당을 transaction에서 변경하고 lockout을 방지한다."""

        self._ensure_ready()
        schema = sql.Identifier(self.settings.auth_db_schema)
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        sql.SQL("SELECT * FROM {}.users WHERE id = %s AND deleted_at IS NULL FOR UPDATE").format(schema),
                        (target_id,),
                    )
                    target = cursor.fetchone()
                    if not target:
                        raise AuthStoreError("user_not_found", "사용자를 찾을 수 없습니다.", 404)
                    if actor.id == target_id and (
                        not request.is_active
                        or not request.is_superuser
                        or request.system_role != "admin"
                    ):
                        raise AuthStoreError("self_lockout_forbidden", "자기 자신의 관리자 권한을 해제할 수 없습니다.", 409)
                    if target["is_superuser"] and not actor.is_superuser:
                        raise AuthStoreError("superuser_required", "최고 관리자만 최고 관리자 계정을 변경할 수 있습니다.", 403)
                    if bool(target["is_superuser"]) != request.is_superuser and not actor.is_superuser:
                        raise AuthStoreError("superuser_required", "최고 관리자만 최고 관리자 권한을 변경할 수 있습니다.", 403)
                    if target["is_superuser"] and (not request.is_superuser or not request.is_active):
                        cursor.execute(
                            sql.SQL(
                                "SELECT count(*) AS count FROM {}.users WHERE is_superuser IS TRUE "
                                "AND is_active IS TRUE AND deleted_at IS NULL AND id <> %s"
                            ).format(schema),
                            (target_id,),
                        )
                        if int(cursor.fetchone()["count"]) == 0:
                            raise AuthStoreError("last_superuser_required", "마지막 활성 최고 관리자는 해제할 수 없습니다.", 409)

                    service_keys = [] if request.all_services_access else request.service_keys
                    if service_keys:
                        cursor.execute(
                            sql.SQL(
                                "SELECT service_key FROM {}.services "
                                "WHERE is_active IS TRUE AND service_key = ANY(%s)"
                            ).format(schema),
                            (service_keys,),
                        )
                        existing_keys = {row["service_key"] for row in cursor.fetchall()}
                        unknown = sorted(set(service_keys) - existing_keys)
                        if unknown:
                            raise AuthStoreError("unknown_service", "존재하지 않거나 비활성화된 서비스가 포함되어 있습니다.", 422)

                    cursor.execute(
                        sql.SQL(
                            "UPDATE {}.users SET system_role = %s, is_superuser = %s, is_active = %s, "
                            "all_services_access = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s"
                        ).format(schema),
                        (
                            request.system_role,
                            request.is_superuser,
                            request.is_active,
                            request.all_services_access,
                            target_id,
                        ),
                    )
                    cursor.execute(sql.SQL("DELETE FROM {}.user_service_permissions WHERE user_id = %s").format(schema), (target_id,))
                    if service_keys:
                        cursor.execute(
                            sql.SQL(
                                """
                                INSERT INTO {}.user_service_permissions
                                    (user_id, service_id, can_access, granted_by)
                                SELECT %s, id, true, %s FROM {}.services WHERE service_key = ANY(%s)
                                """
                            ).format(schema, schema),
                            (target_id, actor.id, service_keys),
                        )
                    if not request.is_active:
                        cursor.execute(
                            sql.SQL(
                                "UPDATE {}.sessions SET revoked_at = CURRENT_TIMESTAMP "
                                "WHERE user_id = %s AND revoked_at IS NULL"
                            ).format(schema),
                            (target_id,),
                        )
        except AuthStoreError:
            raise
        except Exception as exc:
            raise self._unavailable(exc) from exc
        user = self.get_user(target_id)
        if user is None:  # pragma: no cover
            raise AuthStoreError("user_not_found", "사용자를 찾을 수 없습니다.", 404)
        return user

    def _fetch_users(self, *, user_id: UUID | None = None) -> list[UserResponse]:
        schema = sql.Identifier(self.settings.auth_db_schema)
        where = sql.SQL("AND u.id = %s") if user_id else sql.SQL("")
        query = sql.SQL(
            """
            SELECT u.*,
                COALESCE(
                    (SELECT array_agg(s.service_key ORDER BY s.service_key)
                     FROM {}.user_service_permissions AS permission
                     JOIN {}.services AS s ON s.id = permission.service_id
                     WHERE permission.user_id = u.id AND permission.can_access IS TRUE AND s.is_active IS TRUE),
                    ARRAY[]::varchar[]
                ) AS service_keys
            FROM {}.users AS u
            WHERE u.deleted_at IS NULL {}
            ORDER BY u.created_at, u.username
            """
        ).format(schema, schema, schema, where)
        try:
            with self._connect(**self.settings.connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, (user_id,) if user_id else ())
                    return [self._row_to_user(row) for row in cursor.fetchall()]
        except Exception as exc:
            raise self._unavailable(exc) from exc

    @staticmethod
    def _row_to_user(row: dict[str, Any]) -> UserResponse:
        return UserResponse(
            id=row["id"],
            username=row["username"],
            email=row.get("email"),
            display_name=row["display_name"],
            system_role=row["system_role"],
            is_superuser=row["is_superuser"],
            is_active=row["is_active"],
            all_services_access=row["all_services_access"],
            service_keys=list(row.get("service_keys") or []),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_login_at=row.get("last_login_at"),
        )

    def _ensure_ready(self) -> None:
        if not self._ready:
            raise AuthStoreError("auth_unavailable", "인증 저장소가 구성되지 않았습니다.", 503)

    @staticmethod
    def _unavailable(exc: Exception) -> AuthStoreError:
        _logger.warning("인증 PostgreSQL 요청 실패: type=%s", type(exc).__name__)
        return AuthStoreError("auth_unavailable", "인증 저장소를 사용할 수 없습니다.", 503)
