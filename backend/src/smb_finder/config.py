"""LLMOps 문서 챗봇의 읽기 전용 연결 설정."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_REPOSITORY_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _safe_legacy_nas_path_segments(value: str) -> tuple[str, ...]:
    """legacy NAS_FOLD_PATH를 share와 하위 폴더로 분리할 수 있을 때만 반환한다."""

    candidate = value.strip()
    if not candidate or candidate.startswith(("\\", "/")) or re.match(r"^[A-Za-z]:", candidate):
        return ()
    if any(ord(char) < 32 for char in candidate):
        return ()
    segments = tuple(candidate.replace("\\", "/").split("/"))
    if any(not segment or segment in {".", ".."} for segment in segments):
        return ()
    if any(any(char in segment for char in '<>:"|?*') for segment in segments):
        return ()
    if any(segment != segment.strip() or segment.endswith(".") for segment in segments):
        return ()
    return segments


class Settings(BaseSettings):
    """환경변수로 주입되는 PostgreSQL·MinIO·Neo4j·Ollama 설정."""

    model_config = SettingsConfigDict(env_file=_REPOSITORY_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    llmops_db_enabled: bool = True
    llmops_db_host: str = ""
    dev_server: str = ""
    postgres_host: str = ""
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = ""
    postgres_password: str = ""
    llmops_postgres_db: str = ""
    llmops_postgres_schema: str = "llmops"
    skip_postgres: bool = False
    llmops_db_connect_timeout_ms: int = Field(default=1000, ge=100)
    llmops_db_query_timeout_ms: int = Field(default=1500, ge=100)
    llmops_file_search_limit: int = Field(default=10, ge=1, le=25)
    llmops_file_search_max_limit: int = Field(default=20, ge=1, le=50)
    llmops_file_search_budget_ms: int = Field(default=1000, ge=500, le=30_000)
    llmops_file_search_source_limit: int = Field(default=30, ge=5, le=100)
    llmops_file_search_llm_enabled: bool = True
    llmops_file_search_llm_model: str = ""
    llmops_file_search_llm_timeout_ms: int = Field(default=300, ge=200, le=10_000)
    llmops_file_search_llm_max_terms: int = Field(default=8, ge=2, le=20)

    llmops_retrieval_top_k: int = Field(default=6, ge=1, le=20)
    llmops_retrieval_candidate_k: int = Field(default=40, ge=3, le=200)
    llmops_retrieval_score_cutoff: float = Field(default=0.01, ge=0.0, le=1.0)
    llmops_chunk_max_chars: int = Field(default=1600, ge=200, le=10_000)
    llmops_embedding_timeout_ms: int = Field(default=8000, ge=100, le=10_000)
    ollama_base_url: str = ""
    embedding_model: str = "nomic-embed-text-v2-moe"
    embedding_dim: int = Field(default=768, ge=1)
    embedding_query_prefix: str = "search_query: "
    llmops_chat_model: str = "qwen3:30b-a3b"
    llm_timeout_ms: int = Field(default=30_000, ge=100)
    proposal_llm_timeout_ms: int = Field(default=120_000, ge=1000, le=600_000)
    proposal_llm_num_ctx: int = Field(default=24_576, ge=4096, le=262_144)
    proposal_llm_max_tokens: int = Field(default=4096, ge=512, le=16_384)
    proposal_context_max_chars: int = Field(default=30_000, ge=2000, le=200_000)
    proposal_context_per_document_chars: int = Field(default=10_000, ge=500, le=100_000)
    proposal_context_max_citations: int = Field(default=30, ge=1, le=100)
    rag_synthesis_evidence_chars: int = Field(default=1800, ge=500, le=10_000)
    rag_synthesis_max_tokens: int = Field(default=500, ge=64, le=500)
    playground_agent_budget_ms: int = Field(default=30_000, ge=100)
    playground_agent_context_messages: int = Field(default=6, ge=0, le=20)

    # MCP는 현재 문서 metadata 1개만 loopback에서 별도 token으로 공개한다.
    mcp_enabled: bool = False
    mcp_api_token: str = ""
    mcp_metadata_timeout_ms: int = Field(default=1500, ge=1100, le=10_000)
    mcp_metadata_max_concurrency: int = Field(default=2, ge=1, le=8)

    minio_endpoint: str = ""
    minio_secure: bool = False
    llmops_minio_bucket: str = ""
    llmops_minio_access_key: str = ""
    llmops_minio_secret_key: str = ""
    llmops_minio_connect_timeout_ms: int = Field(default=500, ge=100, le=5000)
    llmops_minio_read_timeout_ms: int = Field(default=1000, ge=100, le=10_000)
    llmops_artifact_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)

    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""
    llmops_neo4j_database: str = ""
    llmops_neo4j_mode: str = "graph_space"
    llmops_graph_space: str = "llmops_document_bot"
    llmops_graph_enabled: bool = True
    llmops_neo4j_timeout_ms: int = Field(default=1000, ge=100, le=10_000)

    # SMB 업로드는 명시적으로 켠 경우에만 활성화한다. 기존 NAS_* 키는 전환 기간에만 fallback으로 읽는다.
    smb_upload_enabled: bool = False
    smb_host: str = ""
    smb_share_name: str = ""
    smb_username: str = ""
    smb_password: str = ""
    nas_url: str = ""
    nas_fold_path: str = ""
    nas_user: str = ""
    nas_pw: str = ""
    smb_upload_default_relative_directory: str = ""
    proposal_draft_default_relative_directory: str = ""
    smb_upload_runtime_settings_path: str = ".runtime/playground_settings.json"
    smb_upload_registry_path: str = ".runtime/upload_registry.json"
    smb_upload_max_size_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    smb_upload_allowed_extensions: str = ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv"
    smb_upload_timeout_ms: int = Field(default=15_000, ge=1000, le=120_000)
    smb_upload_max_concurrency: int = Field(default=2, ge=1, le=8)

    @property
    def effective_llmops_db_host(self) -> str:
        """Windows 개발 환경과 컨테이너 환경에 맞는 DB 호스트를 선택한다."""

        if self.llmops_db_host.strip():
            return self.llmops_db_host.strip()
        if os.name == "nt" and self.dev_server.strip():
            return self.dev_server.strip()
        return self.postgres_host.strip() or self.dev_server.strip()

    @property
    def llmops_db_configured(self) -> bool:
        """자격증명을 공개하지 않고 PostgreSQL adapter 준비 여부를 반환한다."""

        return bool(
            self.llmops_db_enabled
            and not self.skip_postgres
            and self.effective_llmops_db_host
            and self.llmops_postgres_db.strip()
            and self.postgres_user.strip()
            and self.postgres_password
        )

    @property
    def llmops_minio_configured(self) -> bool:
        """MinIO 읽기 adapter 준비 여부를 반환한다."""

        return bool(
            self.minio_endpoint.strip()
            and self.llmops_minio_bucket.strip()
            and self.llmops_minio_access_key.strip()
            and self.llmops_minio_secret_key
        )

    @property
    def llmops_neo4j_configured(self) -> bool:
        """Neo4j 읽기 adapter 준비 여부를 반환한다."""

        return bool(
            self.llmops_graph_enabled and self.neo4j_uri.strip() and self.neo4j_user.strip() and self.neo4j_password
        )

    @property
    def effective_smb_upload_host(self) -> str:
        """자격증명을 노출하지 않고 canonical/legacy SMB host를 선택한다."""

        canonical = self.smb_host.strip()
        if canonical:
            return canonical
        candidate = self.nas_url.strip()
        if not candidate or any(ord(char) < 32 for char in candidate):
            return ""
        if candidate.lower().startswith("smb://"):
            parsed = urlsplit(candidate)
            return parsed.hostname or ""
        candidate = candidate.lstrip("\\/")
        return candidate.replace("\\", "/").split("/", 1)[0].strip()

    @property
    def effective_smb_upload_share_name(self) -> str:
        """한 경로 segment인 canonical/legacy 공유 이름을 선택한다."""

        candidate = self.smb_share_name.strip()
        if not candidate:
            legacy_segments = _safe_legacy_nas_path_segments(self.nas_fold_path)
            candidate = legacy_segments[0] if legacy_segments else ""
        if not candidate or candidate in {".", ".."} or any(char in candidate for char in "\\/"):
            return ""
        if any(ord(char) < 32 for char in candidate):
            return ""
        return candidate

    @property
    def effective_smb_upload_default_relative_directory(self) -> str:
        """canonical 상대 경로 또는 legacy 폴더의 share 하위 부분만 반환한다."""

        canonical = self.smb_upload_default_relative_directory.strip()
        if canonical:
            return canonical
        legacy_segments = _safe_legacy_nas_path_segments(self.nas_fold_path)
        if not legacy_segments or len(legacy_segments) < 2:
            return ""
        return "/".join(legacy_segments[1:])

    @property
    def effective_smb_upload_username(self) -> str:
        """canonical 계정을 우선하고 legacy 계정을 fallback으로 사용한다."""

        return (self.smb_username or self.nas_user).strip()

    @property
    def effective_smb_upload_password(self) -> str:
        """canonical 비밀번호를 우선하고 legacy 비밀번호를 fallback으로 사용한다."""

        return self.smb_password or self.nas_pw

    @property
    def smb_upload_credentials_configured(self) -> bool:
        """비밀 값을 반환하지 않고 SMB 업로드 연결 구성 여부만 확인한다."""

        return bool(
            self.effective_smb_upload_host
            and self.effective_smb_upload_share_name
            and self.effective_smb_upload_username
            and self.effective_smb_upload_password
        )

    @property
    def local_llm_configured(self) -> bool:
        """외부 주소를 공개하지 않고 온프레미스 LLM 구성 여부만 반환한다."""

        return bool(self.ollama_base_url.strip() and self.llmops_chat_model.strip())


def load_settings() -> Settings:
    """현재 환경의 설정을 읽는다."""

    return Settings()
