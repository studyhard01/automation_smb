"""설정 로더 — .env 환경변수에서 SMB·인덱스·LLM 설정을 읽는다.

비밀정보(자격증명·내부 IP)는 코드에 하드코딩하지 않고 .env에서만 주입한다.
(CLAUDE.md 보안 규칙)
"""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수 기반 서비스 설정."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── SMB 접속 ──
    smb_host: str = Field(default="", description="SMB 서버 IP")
    smb_share_name: str = Field(default="", description="SMB 공유 폴더 이름")
    smb_username: str = Field(default="", description="SMB 사용자명")
    smb_password: str = Field(default="", description="SMB 비밀번호")

    # ── 인덱스 (지연 최소화 핵심) ──
    smb_index_cache_path: str = Field(default=".cache/folder.index.json", description="인덱스 캐시 파일 경로")
    smb_index_max_depth: int = Field(default=4, description="인덱싱 최대 깊이 (0=무제한). 깊을수록 빌드가 급격히 느려짐")
    smb_index_build_budget_sec: int = Field(default=60, description="인덱스 빌드 시간 상한(초). 초과 시 부분 인덱스로 저장")
    smb_index_exclude: str = Field(default="#recycle,@eaDir,.DS_Store", description="인덱싱 제외 폴더명(쉼표 구분, 대소문자 무시)")
    smb_index_refresh_sec: int = Field(default=900, description="백그라운드 재인덱싱 주기(초), 0=비활성")

    # ── 시간 예산 ──
    find_budget_ms: int = Field(default=1500, description="이 시간을 넘기면 부분 결과라도 반환")
    find_default_limit: int = Field(default=5, description="기본 반환 폴더 수")

    # ── 내용 검색 인덱스 (FTS5, 파일 본문) ──
    content_index_db_path: str = Field(default=".cache/content.fts.db", description="내용 인덱스 SQLite(FTS5) 경로 — 환자 본문 포함, 외부 공유 금지")
    content_index_max_depth: int = Field(default=0, description="내용 인덱싱 최대 깊이 (0=무제한)")
    content_index_build_budget_sec: int = Field(default=300, description="내용 인덱스 빌드 시간 상한(초). 추출이 느려 폴더보다 넉넉")
    content_max_file_mb: float = Field(default=20.0, description="이보다 큰 파일은 내용 인덱싱 건너뜀(추출 비용·지연 방지)")
    content_max_chars_per_file: int = Field(default=200_000, description="파일당 저장 본문 상한(글자). 인덱스 비대화 방지")
    content_extensions: str = Field(
        default=".txt,.csv,.tsv,.md,.log,.json,.xml,.htm,.html,.docx,.xlsx,.pptx,.pdf",
        description="내용 인덱싱 대상 확장자(쉼표 구분). pdf는 pypdf 설치 시에만 본문 추출",
    )
    content_search_budget_ms: int = Field(default=1500, description="내용 검색 시간 예산(ms)")
    content_default_limit: int = Field(default=10, description="기본 반환 파일 수")

    # ── 로컬 RAG PostgreSQL/pgvector 검색 ──
    rag_db_enabled: bool = Field(default=True, description="로컬 PostgreSQL 벡터 검색 tool 활성화 여부")
    rag_db_host: str = Field(default="localhost", description="RAG PostgreSQL host")
    rag_db_port: int = Field(default=5432, ge=1, le=65535, description="RAG PostgreSQL port")
    rag_db_name: str = Field(default="rag_db_local_20260713", description="RAG PostgreSQL database")
    rag_db_user: str = Field(default="postgres", description="RAG PostgreSQL username")
    rag_db_password: str = Field(default="", description="RAG PostgreSQL password")
    rag_db_connect_timeout_ms: int = Field(default=1000, ge=100, description="RAG DB 연결 timeout(ms)")
    rag_db_query_timeout_ms: int = Field(default=1500, ge=100, description="RAG 벡터 SQL 시간 예산(ms)")
    rag_db_default_limit: int = Field(default=5, ge=1, le=20, description="RAG 기본 chunk 반환 수")
    rag_db_max_limit: int = Field(default=10, ge=1, le=50, description="RAG 최대 chunk 반환 수")
    rag_chunk_max_chars: int = Field(default=1200, ge=100, le=10000, description="chunk별 반환 본문 상한")
    rag_embedding_base_url: str = Field(
        default="http://127.0.0.1:8081/v1",
        description="nomic 임베딩용 OpenAI 호환 로컬 endpoint",
    )
    rag_embedding_model: str = Field(default="nomic-embed-text-v2-moe", description="DB와 동일한 임베딩 모델")
    rag_embedding_api_key: str = Field(default="", description="로컬 임베딩 endpoint API key")
    rag_embedding_dimensions: int = Field(default=768, ge=1, description="DB embedding 차원")
    rag_embedding_query_prefix: str = Field(default="search_query: ", description="검색 질의 임베딩 prefix")
    rag_embedding_timeout_ms: int = Field(default=3000, ge=100, description="질의 임베딩 timeout(ms)")

    # ── 관리자 API (무거운 인덱싱 작업) ──
    admin_api_token: str = Field(
        default="",
        description="관리자 전용 API 토큰. 비어 있으면 /admin/* 엔드포인트는 비활성화",
    )
    content_index_job_retention: int = Field(default=50, description="메모리에 보관할 최근 내용 인덱싱 job 수")
    smb_allowed_hosts: str = Field(
        default="",
        description="관리자 인덱싱에서 host override를 허용할 SMB 호스트 목록(쉼표 구분). 비우면 SMB_HOST만 허용",
    )
    smb_allowed_shares: str = Field(
        default="",
        description="관리자 인덱싱에서 share_name override를 허용할 공유명 목록(쉼표 구분). 비우면 SMB_SHARE_NAME만 허용",
    )

    # ── MCP Streamable HTTP (기본 비활성, 로컬 전용) ──
    mcp_enabled: bool = Field(default=False, description="읽기 전용 MCP endpoint 활성화 여부")
    mcp_api_token: str = Field(default="", description="MCP 전용 bearer token. ADMIN_API_TOKEN과 분리")
    mcp_allow_remote: bool = Field(default=False, description="원격 client 연결 허용 여부. 기본은 loopback만 허용")
    mcp_allowed_hosts: str = Field(
        default="127.0.0.1,localhost,[::1]",
        description="MCP HTTP Host 허용 목록(쉼표 구분, port 제외)",
    )
    mcp_allowed_origins: str = Field(
        default="",
        description="Origin 헤더가 있는 MCP client 허용 목록. 비우면 해당 요청은 거부",
    )
    mcp_max_body_bytes: int = Field(default=65_536, ge=1024, le=1_048_576, description="MCP 요청 본문 상한(bytes)")

    # ── L2 의도 해석 LLM (OpenAI 호환) ──
    llm_intent_enabled: bool = Field(default=False, description="모호한 질의를 LLM으로 정규화할지")
    llm_base_url: str = Field(default="http://localhost:8080", description="OpenAI 호환 LLM base_url")
    llm_model: str = Field(default="", description="LLM 모델명 (llama.cpp는 빈 값 가능)")
    llm_api_key: str = Field(default="", description="LLM API 키 (llama.cpp는 빈 값 가능)")
    llm_timeout_ms: int = Field(default=10000, description="LLM 호출 timeout(ms)")
    openai_base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI API base_url")
    openai_model: str = Field(default="gpt-4.1-mini", description="Playground OpenAI 기본 모델명")
    openai_api_key: str = Field(default="", description="OpenAI API 키. UI 요청 값이 우선")

    # ── LangSmith 관측성 (기본 OFF, OpenAI provider 호출만 추적) ──
    langsmith_tracing: bool = Field(default=False, description="LangSmith tracing 활성화 여부")
    langsmith_api_key: str = Field(default="", description="LangSmith API key. .env에만 저장")
    langsmith_project: str = Field(default="automation-smb-playground", description="LangSmith project 이름")
    langsmith_endpoint: str = Field(default="", description="self-hosted LangSmith endpoint. 비우면 LangSmith Cloud")
    langsmith_hide_inputs: bool = Field(default=False, description="LangSmith trace에서 LLM 입력 본문 숨김")
    langsmith_hide_outputs: bool = Field(default=False, description="LangSmith trace에서 LLM 출력 본문 숨김")

    # ── MLflow 문서 챗봇 오프라인 평가 (서비스 경로 기본 OFF) ──
    mlflow_evaluation_enabled: bool = Field(default=False, description="MLflow 오프라인 평가 기능 활성화 여부")
    mlflow_tracing_enabled: bool = Field(default=False, description="MLflow 서비스 tracing 활성화 여부")
    mlflow_tracking_uri: str = Field(default="http://127.0.0.1:5000", description="MLflow Tracking Server URL")
    mlflow_experiment_name: str = Field(
        default="automation-smb-doc-chatbot",
        description="문서 챗봇 평가 experiment 이름",
    )
    mlflow_tracking_timeout_ms: int = Field(default=2000, ge=100, le=30000, description="MLflow preflight timeout")
    mlflow_judge_enabled: bool = Field(default=False, description="MLflow LLM judge 활성화 여부")
    mlflow_judge_model: str = Field(default="openai:/gpt-4.1-mini", description="MLflow judge model URI")
    mlflow_trace_include_content: bool = Field(default=False, description="합성 평가 trace에 chunk 본문 포함 여부")

    # ── 로컬 LangGraph Studio 관측기 (기본 OFF, 안전한 메타데이터만 전송) ──
    langgraph_studio_observer_enabled: bool = Field(
        default=False,
        description="로컬 LangGraph Studio에 Playground 실행 메타데이터를 비동기로 전달할지 여부",
    )
    langgraph_studio_observer_url: str = Field(
        default="http://127.0.0.1:2024",
        description="loopback 전용 LangGraph API URL",
    )
    langgraph_studio_observer_graph_id: Literal["playground_observer"] = Field(
        default="playground_observer",
        description="안전한 관측 이벤트만 받는 고정 LangGraph graph ID",
    )
    langgraph_studio_observer_timeout_ms: int = Field(
        default=300,
        ge=50,
        le=2000,
        description="관측 이벤트 전달 timeout(ms)",
    )
    langgraph_studio_observer_queue_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="응답 경로와 분리된 관측 이벤트 메모리 큐 상한",
    )

    # ── Playground 제한형 agent/debug ──
    playground_agent_max_steps: int = Field(default=3, description="Playground agent 최대 판단 단계 수")
    playground_agent_max_tool_calls: int = Field(default=2, description="Playground agent 요청당 최대 tool 호출 수")
    playground_agent_budget_ms: int = Field(default=10000, description="Playground agent 전체 시간 예산(ms)")
    playground_agent_context_messages: int = Field(default=6, description="Playground agent에 전달할 최근 대화 수")
    playground_agent_result_chars: int = Field(default=2000, description="LLM observation에 전달할 tool 결과 최대 글자 수")
    playground_skills_dir: str = Field(
        default=".cache/playground-skills",
        description="UI에서 생성한 사용자 SKILL.md를 저장할 Git 제외 로컬 디렉터리",
    )
    playground_skill_prompt_chars: int = Field(
        default=16_000,
        ge=1000,
        le=100_000,
        description="한 요청에서 system prompt에 주입할 전체 skill 지침 글자 수 상한",
    )
    playground_debug_preview_chars: int = Field(default=4000, description="raw LLM debug preview 최대 글자 수")
    @field_validator("langgraph_studio_observer_url")
    @classmethod
    def validate_langgraph_studio_observer_url(cls, value: str) -> str:
        """Studio 관측 대상은 userinfo나 부가 URL 요소가 없는 loopback HTTP만 허용한다."""

        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("LANGGRAPH_STUDIO_OBSERVER_URL은 부가 경로가 없는 loopback HTTP URL이어야 합니다.")
        hostname = parsed.hostname.lower()
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = hostname == "localhost"
        if not is_loopback:
            raise ValueError("LANGGRAPH_STUDIO_OBSERVER_URL은 loopback 주소만 허용합니다.")
        return normalized

    @property
    def smb_root(self) -> str:
        r"""공유 루트 UNC 경로 (\\host\share)."""
        return rf"\\{self.smb_host}\{self.smb_share_name}"

    @property
    def exclude_set(self) -> set[str]:
        """인덱싱 제외 폴더명 집합 (소문자 정규화)."""
        return {x.strip().lower() for x in self.smb_index_exclude.split(",") if x.strip()}

    @property
    def content_ext_set(self) -> set[str]:
        """내용 인덱싱 대상 확장자 집합 (소문자, 점 포함)."""
        return {x.strip().lower() for x in self.content_extensions.split(",") if x.strip()}

    @property
    def smb_allowed_host_set(self) -> set[str]:
        """관리자 override 허용 SMB 호스트 집합."""
        configured = {self.smb_host.strip().lower()} if self.smb_host.strip() else set()
        extra = {x.strip().lower() for x in self.smb_allowed_hosts.split(",") if x.strip()}
        return configured | extra

    @property
    def smb_allowed_share_set(self) -> set[str]:
        """관리자 override 허용 SMB 공유명 집합."""
        configured = {self.smb_share_name.strip().lower()} if self.smb_share_name.strip() else set()
        extra = {x.strip().lower() for x in self.smb_allowed_shares.split(",") if x.strip()}
        return configured | extra

    @property
    def content_max_file_bytes(self) -> int:
        """내용 인덱싱 파일 크기 상한(바이트)."""
        return int(self.content_max_file_mb * 1024 * 1024)

    @property
    def mcp_allowed_host_set(self) -> set[str]:
        """port를 제외하고 비교할 MCP HTTP Host 허용 집합."""
        return {value.strip().lower() for value in self.mcp_allowed_hosts.split(",") if value.strip()}

    @property
    def mcp_allowed_origin_set(self) -> set[str]:
        """Origin 헤더가 있는 MCP client에 명시적으로 허용한 값의 집합."""
        return {value.strip().rstrip("/").lower() for value in self.mcp_allowed_origins.split(",") if value.strip()}


def load_settings() -> Settings:
    """설정 인스턴스를 생성한다."""
    return Settings()
