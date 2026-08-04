"""PostgreSQL ACL/Revision 확인 뒤 MinIO Artifact를 읽기 전용으로 반환한다."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlparse

import boto3
import psycopg
from botocore.config import Config
from psycopg import sql
from psycopg.rows import dict_row

from .config import Settings
from .models import ArtifactViewResponse, StoreConnectionState


class LlmopsArtifactError(RuntimeError):
    """내부 URI·Credential을 포함하지 않는 Artifact 오류."""

    def __init__(self, code: str, message: str, elapsed_ms: float = 0.0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.elapsed_ms = elapsed_ms


class LlmopsArtifactReader:
    """활성 Revision의 preview/canonical Object에 stat/get만 수행한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        s3_client: Any | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect or psycopg.connect
        self._client = s3_client or boto3.client(
            "s3",
            endpoint_url=settings.minio_endpoint.strip(),
            aws_access_key_id=settings.llmops_minio_access_key,
            aws_secret_access_key=settings.llmops_minio_secret_key,
            use_ssl=settings.minio_secure,
            config=Config(
                connect_timeout=max(0.1, settings.llmops_minio_connect_timeout_ms / 1000),
                read_timeout=max(0.1, settings.llmops_minio_read_timeout_ms / 1000),
                retries={"max_attempts": 0},
                signature_version="s3v4",
            ),
        )

    def read(
        self,
        doc_id: str,
        revision_id: str,
        artifact_type: Literal["preview", "canonical"],
    ) -> ArtifactViewResponse:
        """활성 문서 pair에 연결된 작은 Text Artifact를 읽는다."""

        started = time.perf_counter()
        if artifact_type not in {"preview", "canonical"}:
            raise LlmopsArtifactError("unsupported_artifact_type", "지원하지 않는 문서 보기 형식입니다.")
        object_row = self._find_object(doc_id, revision_id, artifact_type, started)
        if object_row is None:
            raise LlmopsArtifactError(
                "artifact_not_found",
                "선택한 활성 문서의 보기 파일을 찾지 못했습니다.",
                round((time.perf_counter() - started) * 1000, 1),
            )
        bucket, key = self._parse_object_uri(str(object_row["object_uri"]))
        try:
            metadata = self._client.head_object(Bucket=bucket, Key=key)
            size_bytes = int(metadata.get("ContentLength") or object_row.get("size_bytes") or 0)
            if size_bytes > self._settings.llmops_artifact_max_bytes:
                raise LlmopsArtifactError(
                    "artifact_too_large",
                    "문서 보기 파일이 허용 크기를 초과했습니다.",
                    round((time.perf_counter() - started) * 1000, 1),
                )
            response = self._client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                raw = body.read(self._settings.llmops_artifact_max_bytes + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except LlmopsArtifactError:
            raise
        except Exception as exc:
            raise LlmopsArtifactError(
                "artifact_store_unavailable",
                "문서 보기 저장소에서 파일을 읽을 수 없습니다.",
                round((time.perf_counter() - started) * 1000, 1),
            ) from exc
        if len(raw) > self._settings.llmops_artifact_max_bytes:
            raise LlmopsArtifactError(
                "artifact_too_large",
                "문서 보기 파일이 허용 크기를 초과했습니다.",
                round((time.perf_counter() - started) * 1000, 1),
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LlmopsArtifactError(
                "artifact_not_text",
                "문서 보기 파일을 안전한 Text로 해석할 수 없습니다.",
                round((time.perf_counter() - started) * 1000, 1),
            ) from exc
        media_type = str(object_row.get("content_type") or "").strip()
        if not media_type:
            media_type = "application/json" if artifact_type == "canonical" else "text/markdown"
        return ArtifactViewResponse(
            doc_id=doc_id,
            revision_id=revision_id,
            artifact_type=artifact_type,
            media_type=media_type,
            content=content,
            size_bytes=len(raw),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    def _find_object(
        self,
        doc_id: str,
        revision_id: str,
        artifact_type: str,
        started: float,
    ) -> dict[str, Any] | None:
        schema = sql.Identifier(self._settings.llmops_postgres_schema)
        query = sql.SQL(
            """
            SELECT a.object_uri, a.content_type, a.size_bytes
            FROM {}.artifacts AS a
            JOIN {}.document_revisions AS r ON r.revision_id = a.revision_id
            JOIN {}.documents AS d ON d.doc_id = r.doc_id
            WHERE d.doc_id = %s::uuid
              AND d.active_revision_id = %s::uuid
              AND r.revision_id = %s::uuid
              AND d.deleted_at IS NULL
              AND upper(r.status::text) = 'ACTIVE'
              AND a.artifact_type = %s
            ORDER BY a.created_at DESC
            LIMIT 1
            """
        ).format(schema, schema, schema)
        try:
            with self._connect(**self._connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, (doc_id, revision_id, revision_id, artifact_type))
                    return cursor.fetchone()
        except Exception as exc:
            raise LlmopsArtifactError(
                "artifact_registry_unavailable",
                "문서 보기 Registry를 확인할 수 없습니다.",
                round((time.perf_counter() - started) * 1000, 1),
            ) from exc

    def _parse_object_uri(self, object_uri: str) -> tuple[str, str]:
        parsed = urlparse(object_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if parsed.scheme != "s3" or bucket != self._settings.llmops_minio_bucket or not key:
            raise LlmopsArtifactError("artifact_uri_invalid", "문서 보기 Object 연결 정보가 올바르지 않습니다.")
        return bucket, key

    def _connection_kwargs(self) -> dict[str, Any]:
        connect_timeout_sec = max(1, math.ceil(self._settings.llmops_db_connect_timeout_ms / 1000))
        return {
            "host": self._settings.effective_llmops_db_host,
            "port": self._settings.postgres_port,
            "dbname": self._settings.llmops_postgres_db,
            "user": self._settings.postgres_user,
            "password": self._settings.postgres_password,
            "connect_timeout": connect_timeout_sec,
            "application_name": "automation_smb_artifact_read",
            "options": (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={self._settings.llmops_db_query_timeout_ms}"
            ),
        }

    def status(self) -> StoreConnectionState:
        """Object를 열거하지 않고 bucket HEAD만 수행한다."""

        started = time.perf_counter()
        if not self._settings.llmops_minio_configured:
            return StoreConnectionState(
                configured=False,
                connected=False,
                degraded=True,
                message="LLMOps MinIO가 구성되지 않았습니다.",
            )
        try:
            self._client.head_bucket(Bucket=self._settings.llmops_minio_bucket)
            return StoreConnectionState(
                configured=True,
                connected=True,
                message="연결됨",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                metadata={"bucket_configured": True, "read_operations": ["stat", "get"]},
            )
        except Exception:
            return StoreConnectionState(
                configured=True,
                connected=False,
                degraded=True,
                message="MinIO 연결을 확인할 수 없습니다.",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                metadata={"bucket_configured": True},
            )

    def close(self) -> None:
        """S3 HTTP client를 닫는다."""

        close = getattr(self._client, "close", None)
        if callable(close):
            close()
