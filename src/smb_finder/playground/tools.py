"""Playground tool registry."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from smb_finder.config import Settings
from smb_finder.rag_search import RagSearchError
from smb_finder.reports import run_cytogenetics_karyotype_summary, run_cytogenetics_report, run_ngs_report
from smb_finder.tooling import ToolExecutionError, ToolExecutor

from .models import ToolDefinition, ToolExecutionResult
from .skills import SkillStore, SkillStoreError


@dataclass(frozen=True)
class PlaygroundRuntime:
    """Playground tool 실행에 필요한 런타임 객체 묶음."""

    settings: Settings
    finder: Any | None = None
    content_searcher: Any | None = None
    rag_searcher: Any | None = None


@dataclass(frozen=True)
class ToolExecutionContext:
    """LLM-backed tool이 현재 agent의 provider/model 호출을 재사용하는 문맥."""

    provider: str
    model: str
    invoke_json: Callable[[list[dict[str, str]], int, str], dict[str, Any]]


@dataclass(frozen=True)
class ToolHandler:
    """tool 정의와 실행 함수를 함께 보관한다."""

    definition: ToolDefinition
    run: Callable[[dict[str, Any]], ToolExecutionResult]
    run_with_context: Callable[[dict[str, Any], ToolExecutionContext], ToolExecutionResult] | None = None
    returns_final_answer: bool = False

    def execute(
        self,
        args: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecutionResult:
        """일반 tool 또는 현재 LLM 문맥이 필요한 tool을 실행한다."""

        if self.run_with_context is None:
            return self.run(args)
        if context is None:
            return ToolExecutionResult(
                status="error",
                result_text="이 tool은 선택한 LLM 실행 정보가 필요합니다.",
                error_code="llm_context_required",
            )
        return self.run_with_context(args, context)


def _text_arg(args: dict[str, Any], key: str, fallback: str = "") -> str:
    value = args.get(key, fallback)
    return str(value or "").strip()


def _format_find_response(data: Any, query: str = "") -> str:
    hits = list(getattr(data, "hits", []))
    if not hits:
        display_query = getattr(data, "normalized_query", getattr(data, "query", query))
        return f"'{display_query}' 관련 폴더를 찾지 못했습니다."
    lines = [
        f"폴더 검색 결과 {len(hits)}건 ({getattr(data, 'elapsed_ms', 0)}ms):",
    ]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. {hit.name} - {hit.path}")
    return "\n".join(lines)


def _format_content_response(data: Any) -> str:
    hits = list(getattr(data, "hits", []))
    if not hits:
        indexed_files = getattr(data, "indexed_files", 0)
        if indexed_files == 0:
            return "내용 인덱스가 비어 있습니다. 먼저 필요한 폴더를 DB화해야 합니다."
        return "본문 내용과 일치하는 파일을 찾지 못했습니다."
    terms = " ".join(getattr(data, "terms", []))
    lines = [
        f"내용 검색 결과 {len(hits)}건 ({terms}, {getattr(data, 'elapsed_ms', 0)}ms):",
    ]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. {hit.name} ({hit.ext}) - {hit.path}")
    return "\n".join(lines)


def _format_rag_response(data: Any) -> str:
    hits = list(getattr(data, "hits", []))
    if not hits:
        cutoff = float(getattr(data, "similarity_cutoff", 0.0) or 0.0)
        top_similarity = getattr(data, "top_similarity", None)
        if bool(getattr(data, "no_answer", False)) and top_similarity is not None:
            return (
                "답변할 만큼 충분한 문서 근거를 찾지 못했습니다. "
                f"(최고 유사도 {float(top_similarity):.3f}, 기준 {cutoff:.3f})"
            )
        return "DB 벡터 검색과 일치하는 chunk를 찾지 못했습니다."
    lines = [
        (
            f"DB 벡터 검색 결과 {len(hits)}건 "
            f"(임베딩 {getattr(data, 'embedding_ms', 0)}ms, DB {getattr(data, 'db_ms', 0)}ms):"
        )
    ]
    for index, hit in enumerate(hits, start=1):
        location = hit.section_path or hit.location_label or hit.location_type
        location_text = f" / {location}" if location else ""
        lines.append(f"{index}. {hit.file_name}{location_text} (유사도 {hit.similarity:.3f})")
        lines.append(hit.content)
    return "\n".join(lines)


def build_tool_registry(runtime: PlaygroundRuntime) -> dict[str, ToolHandler]:
    """현재 런타임 상태에 맞는 tool registry를 만든다."""

    settings = runtime.settings
    search_executor = ToolExecutor(runtime)
    skill_store = SkillStore(settings.playground_skills_dir)

    def find_folder(args: dict[str, Any]) -> ToolExecutionResult:
        query = _text_arg(args, "query")
        started = time.perf_counter()
        try:
            response = search_executor.execute("find_folder", args, surface="playground")
        except ToolExecutionError as exc:
            error_code = "empty_query" if exc.code == "invalid_arguments" and not query else exc.code
            return ToolExecutionResult(
                status="error",
                result_text=exc.message,
                error_code=error_code,
                arguments_summary=f"query_len={len(query)} elapsed_ms={exc.elapsed_ms}",
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return ToolExecutionResult(
            result_text=_format_find_response(response, query),
            result_payload=response.model_dump(),
            arguments_summary=f"query_len={len(query)} elapsed_ms={elapsed_ms}",
        )

    def search_content(args: dict[str, Any]) -> ToolExecutionResult:
        query = _text_arg(args, "query")
        started = time.perf_counter()
        try:
            response = search_executor.execute("search_content", args, surface="playground")
        except ToolExecutionError as exc:
            error_code = "empty_query" if exc.code == "invalid_arguments" and not query else exc.code
            return ToolExecutionResult(
                status="error",
                result_text=exc.message,
                error_code=error_code,
                arguments_summary=f"query_len={len(query)} elapsed_ms={exc.elapsed_ms}",
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return ToolExecutionResult(
            result_text=_format_content_response(response),
            result_payload=response.model_dump(),
            arguments_summary=f"query_len={len(query)} elapsed_ms={elapsed_ms}",
        )

    def search_rag_chunks(args: dict[str, Any]) -> ToolExecutionResult:
        query = _text_arg(args, "query")
        raw_limit = args.get("limit", settings.rag_db_default_limit)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = settings.rag_db_default_limit
        if runtime.rag_searcher is None:
            return ToolExecutionResult(
                status="error",
                result_text="로컬 RAG DB 검색기가 준비되지 않았습니다. 서버 설정과 시작 로그를 확인하세요.",
                error_code="rag_search_unavailable",
                arguments_summary=f"query_len={len(query)} limit={limit}",
            )
        try:
            response = runtime.rag_searcher.search(query, limit)
        except RagSearchError as exc:
            return ToolExecutionResult(
                status="error",
                result_text=exc.message,
                error_code=exc.code,
                arguments_summary=f"query_len={len(query)} limit={limit} elapsed_ms={exc.elapsed_ms}",
            )
        result_text = _format_rag_response(response)
        return ToolExecutionResult(
            result_text=result_text,
            observation_text=result_text,
            result_payload=response.model_dump(),
            arguments_summary=(
                f"query_len={len(query)} limit={limit} embedding_ms={response.embedding_ms} "
                f"db_ms={response.db_ms} elapsed_ms={response.elapsed_ms} "
                f"cutoff={response.similarity_cutoff} top_similarity={response.top_similarity} "
                f"rejected_count={response.rejected_count}"
            ),
        )

    def refresh_content(args: dict[str, Any]) -> ToolExecutionResult:
        path = _text_arg(args, "path")
        if not settings.admin_api_token.strip():
            return ToolExecutionResult(
                status="error",
                result_text="ADMIN_API_TOKEN이 설정되지 않아 내용 DB화 tool은 비활성화되어 있습니다.",
                error_code="admin_api_disabled",
                arguments_summary=f"path_len={len(path)}",
            )
        return ToolExecutionResult(
            status="skipped",
            result_text=(
                "내용 DB화는 무거운 관리자 작업이라 Playground 1차 버전에서는 자동 실행하지 않습니다. "
                "/admin/content-index-jobs API 또는 Langflow의 DB화 컴포넌트를 사용하세요."
            ),
            error_code="admin_tool_manual_only",
            arguments_summary=f"path_len={len(path)}",
        )

    def cytogenetics_report(args: dict[str, Any]) -> ToolExecutionResult:
        return run_cytogenetics_report(args, settings=settings, content_searcher=runtime.content_searcher)

    def cytogenetics_karyotype_summary(args: dict[str, Any]) -> ToolExecutionResult:
        return run_cytogenetics_karyotype_summary(args)

    def cytogenetics_karyotype_summary_with_context(
        args: dict[str, Any], context: ToolExecutionContext
    ) -> ToolExecutionResult:
        return run_cytogenetics_karyotype_summary(
            args,
            invoke_json=context.invoke_json,
            budget_ms=settings.playground_agent_budget_ms,
        )

    def ngs_report(args: dict[str, Any]) -> ToolExecutionResult:
        return run_ngs_report(args, settings=settings, content_searcher=runtime.content_searcher)

    def create_playground_skill(args: dict[str, Any]) -> ToolExecutionResult:
        """검증된 사용자 SKILL.md를 로컬 Playground 저장소에 만든다."""

        skill_id = _text_arg(args, "skill_id")
        description = re.sub(r"\s+", " ", _text_arg(args, "description")).strip()
        instructions = _text_arg(args, "instructions").replace("\r\n", "\n").replace("\r", "\n")
        arguments_summary = (
            f"skill_id={skill_id or '-'} description_len={len(description)} instructions_len={len(instructions)}"
        )
        if not description:
            return ToolExecutionResult(
                status="error",
                result_text="스킬을 언제 사용할지 설명하는 description이 필요합니다.",
                error_code="skill_description_missing",
                arguments_summary=arguments_summary,
            )
        if len(description) > 500:
            return ToolExecutionResult(
                status="error",
                result_text="스킬 description은 500자 이하여야 합니다.",
                error_code="skill_description_too_large",
                arguments_summary=arguments_summary,
            )
        if not instructions:
            return ToolExecutionResult(
                status="error",
                result_text="스킬 본문 instructions가 필요합니다.",
                error_code="skill_instructions_missing",
                arguments_summary=arguments_summary,
            )
        if instructions.startswith("---"):
            return ToolExecutionResult(
                status="error",
                result_text="instructions에는 frontmatter를 넣지 마세요. 서버가 SKILL.md 메타데이터를 생성합니다.",
                error_code="skill_instructions_frontmatter",
                arguments_summary=arguments_summary,
            )
        if len(instructions.splitlines()) > 500:
            return ToolExecutionResult(
                status="error",
                result_text="스킬 본문은 500줄 이하여야 합니다.",
                error_code="skill_instructions_too_many_lines",
                arguments_summary=arguments_summary,
            )

        document = (
            "---\n"
            f"name: {skill_id}\n"
            f"description: {json.dumps(description, ensure_ascii=False)}\n"
            "---\n\n"
            f"{instructions}\n"
        )
        try:
            skill = skill_store.create(skill_id, document)
        except SkillStoreError as exc:
            return ToolExecutionResult(
                status="error",
                result_text=exc.message,
                error_code=exc.code,
                arguments_summary=arguments_summary,
            )
        except OSError:
            return ToolExecutionResult(
                status="error",
                result_text="로컬 skill 저장소에 SKILL.md를 기록하지 못했습니다.",
                error_code="skill_write_failed",
                arguments_summary=arguments_summary,
            )

        return ToolExecutionResult(
            result_text=f"'{skill.id}' 스킬을 생성하고 현재 agent에 즉시 활성화했습니다.",
            observation_text=f"새 스킬 '{skill.id}'이 생성되어 다음 agent 판단부터 사용할 수 있습니다.",
            result_payload={
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "editable": skill.editable,
            },
            arguments_summary=arguments_summary,
        )

    find_timeout = max(100, settings.find_budget_ms)
    content_timeout = max(100, settings.content_search_budget_ms)
    return {
        "find_folder": ToolHandler(
            definition=ToolDefinition(
                id="find_folder",
                display_name="공유폴더 찾기",
                description="폴더 이름이나 경로 단서로 사내 SMB 공유폴더를 찾습니다.",
                category="smb",
                permission="read",
                execution_type="code",
                enabled=True,
                default_selected=True,
                timeout_ms=find_timeout,
                input_schema={"query": "찾을 폴더 설명 또는 키워드"},
            ),
            run=find_folder,
        ),
        "search_content": ToolHandler(
            definition=ToolDefinition(
                id="search_content",
                display_name="파일 내용 검색",
                description="로컬 내용 인덱스에서 파일 본문 키워드를 검색합니다.",
                category="smb",
                permission="read",
                execution_type="code",
                enabled=True,
                default_selected=False,
                timeout_ms=content_timeout,
                input_schema={"query": "찾을 본문 키워드"},
            ),
            run=search_content,
        ),
        "search_rag_chunks": ToolHandler(
            definition=ToolDefinition(
                id="search_rag_chunks",
                display_name="벡터 기반 Chunk 검색",
                description=(
                    "로컬 PostgreSQL의 document_chunks를 pgvector 유사도로 검색하고, "
                    "찾은 근거 chunk를 챗봇 답변 생성에 전달합니다."
                ),
                category="database",
                permission="read",
                execution_type="code",
                enabled=settings.rag_db_enabled and runtime.rag_searcher is not None,
                default_selected=False,
                timeout_ms=max(100, settings.rag_embedding_timeout_ms + settings.rag_db_query_timeout_ms),
                input_schema={
                    "query": "DB에서 근거 chunk를 찾을 자연어 질문",
                    "limit": f"반환 chunk 수(최대 {settings.rag_db_max_limit})",
                },
            ),
            run=search_rag_chunks,
        ),
        "cytogenetics_karyotype_summary": ToolHandler(
            definition=ToolDefinition(
                id="cytogenetics_karyotype_summary",
                display_name="핵형분석요약",
                description=(
                    "선택한 LLM provider/model로 테스트용 ISCN 문자열을 요약합니다. "
                    "Local과 OpenAI에서 같은 방식으로 실행됩니다."
                ),
                category="report",
                permission="read",
                execution_type="llm",
                enabled=True,
                default_selected=False,
                timeout_ms=max(100, settings.llm_timeout_ms),
                input_schema={"iscn": "요약할 테스트용 ISCN 문자열"},
            ),
            run=cytogenetics_karyotype_summary,
            run_with_context=cytogenetics_karyotype_summary_with_context,
            returns_final_answer=True,
        ),
        "cytogenetics_report": ToolHandler(
            definition=ToolDefinition(
                id="cytogenetics_report",
                display_name="세포유전 보고서",
                description=(
                    "로컬 내용 인덱스에서 세포유전 보고서/템플릿 후보를 찾고, "
                    "핵형·FISH·ISCN 보고서 작성 체크리스트를 정리합니다."
                ),
                category="report",
                permission="read",
                execution_type="code",
                enabled=True,
                default_selected=False,
                timeout_ms=content_timeout,
                input_schema={
                    "query": "찾을 보고서/템플릿 단서",
                    "context": "검사 유형, 검체, 키워드 등 테스트 맥락",
                    "limit": "반환 후보 수(최대 20)",
                },
            ),
            run=cytogenetics_report,
        ),
        "ngs_report": ToolHandler(
            definition=ToolDefinition(
                id="ngs_report",
                display_name="NGS 보고서",
                description=(
                    "로컬 내용 인덱스에서 NGS 보고서/템플릿 후보를 찾고, "
                    "QC·변이 표기·해석 근거 체크리스트를 정리합니다."
                ),
                category="report",
                permission="read",
                execution_type="code",
                enabled=True,
                default_selected=False,
                timeout_ms=content_timeout,
                input_schema={
                    "query": "찾을 보고서/템플릿 단서",
                    "context": "패널, 질환군, 변이 종류 등 테스트 맥락",
                    "limit": "반환 후보 수(최대 20)",
                },
            ),
            run=ngs_report,
        ),
        "refresh_content": ToolHandler(
            definition=ToolDefinition(
                id="refresh_content",
                display_name="폴더 내용 DB화",
                description="관리자 승인 후 특정 폴더의 파일 본문을 로컬 검색 DB에 적재합니다.",
                category="smb",
                permission="admin",
                execution_type="code",
                enabled=False,
                default_selected=False,
                requires_admin=True,
                timeout_ms=max(100, settings.content_index_build_budget_sec * 1000),
                input_schema={"path": "공유 루트 기준 상대 폴더 경로"},
            ),
            run=refresh_content,
        ),
        "create_playground_skill": ToolHandler(
            definition=ToolDefinition(
                id="create_playground_skill",
                display_name="Skill 생성",
                description=(
                    "검증된 SKILL.md를 로컬 Playground 저장소에 만들고 현재 agent에 즉시 활성화합니다. "
                    "skill-creator 스킬을 선택했을 때 자동으로 사용할 수 있습니다."
                ),
                category="skill",
                permission="write",
                execution_type="code",
                enabled=True,
                default_selected=False,
                timeout_ms=1000,
                input_schema={
                    "skill_id": "소문자 영문·숫자·하이픈으로 된 고유 id(최대 64자)",
                    "description": "이 스킬을 사용할 상황과 trigger를 설명하는 한 줄",
                    "instructions": "frontmatter를 제외한 간결한 Markdown 실행 지침",
                },
            ),
            run=create_playground_skill,
        ),
    }
