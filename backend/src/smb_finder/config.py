"""LLMOps 문서 챗봇의 읽기 전용 연결 설정."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_REPOSITORY_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


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
    llmops_db_query_timeout_ms: int = Field(default=1000, ge=100)
    llmops_file_search_limit: int = Field(default=10, ge=1, le=25)
    llmops_file_search_max_limit: int = Field(default=20, ge=1, le=50)

    llmops_retrieval_top_k: int = Field(default=6, ge=1, le=20)
    llmops_retrieval_candidate_k: int = Field(default=40, ge=3, le=200)
    llmops_retrieval_score_cutoff: float = Field(default=0.01, ge=0.0, le=1.0)
    llmops_chunk_max_chars: int = Field(default=1600, ge=200, le=10_000)
    llmops_embedding_timeout_ms: int = Field(default=2000, ge=100, le=10_000)
    ollama_base_url: str = ""
    embedding_model: str = "nomic-embed-text-v2-moe"
    embedding_dim: int = Field(default=768, ge=1)
    embedding_query_prefix: str = "search_query: "
    llmops_chat_model: str = "qwen3:30b-a3b"
    llm_timeout_ms: int = Field(default=10_000, ge=100)
    rag_synthesis_evidence_chars: int = Field(default=1800, ge=500, le=10_000)
    rag_synthesis_max_tokens: int = Field(default=500, ge=64, le=500)
    playground_agent_budget_ms: int = Field(default=10_000, ge=100)
    playground_agent_context_messages: int = Field(default=6, ge=0, le=20)

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
            self.llmops_graph_enabled
            and self.neo4j_uri.strip()
            and self.neo4j_user.strip()
            and self.neo4j_password
        )


def load_settings() -> Settings:
    """현재 환경의 설정을 읽는다."""

    return Settings()
