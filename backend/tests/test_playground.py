"""자체 Playground의 tool registry와 agent 계약 테스트."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from smb_finder.config import Settings
from smb_finder.playground.agent import (
    LlmResponseFormatError,
    PlaygroundAgent,
    _json_from_text,
    _json_request_messages,
)
from smb_finder.playground.models import (
    AgentDecision,
    ChatRequest,
    LlmDebugCall,
    LlmStatusRequest,
    PlannedToolCall,
    ToolDefinition,
)
from smb_finder.playground.tools import PlaygroundRuntime, ToolExecutionContext, build_tool_registry
from smb_finder.rag_search import RagChunkHit, RagSearchError, RagSearchResponse


class FakeFinder:
    def find(self, request):  # noqa: ANN001
        return SimpleNamespace(
            normalized_query=request.query,
            elapsed_ms=12.3,
            hits=[SimpleNamespace(name="OO검사", path="검사결과/2026/OO검사")],
        )


class FakeContentSearcher:
    def search(self, request):  # noqa: ANN001
        return SimpleNamespace(
            terms=[request.query],
            elapsed_ms=10.0,
            indexed_files=1,
            hits=[SimpleNamespace(name="report.txt", ext=".txt", path="검사결과/report.txt")],
        )


class FakeRagSearcher:
    def search(self, query: str, limit: int) -> RagSearchResponse:
        return RagSearchResponse(
            query=query,
            hits=[
                RagChunkHit(
                    chunk_id=7,
                    document_id=2,
                    chunk_index=1,
                    content="프로젝트 오로라 일정은 7월입니다.",
                    similarity=0.91,
                    file_name="aurora.md",
                    file_path="docs/aurora.md",
                    section_path="일정",
                )
            ],
            result_count=1,
            candidate_count=1,
            similarity_cutoff=0.4,
            top_similarity=0.91,
            embedding_model="nomic-embed-text-v2-moe",
            embedding_ms=4.0,
            db_ms=3.0,
            elapsed_ms=7.0,
        )


def test_agent_decision_prompt_includes_selected_file_scope(monkeypatch):
    captured = {}
    agent = PlaygroundAgent(
        Settings(
            _env_file=None,
            llm_model="local-model",
            llm_base_url="http://127.0.0.1:8080/v1",
        )
    )

    def fake_chat(messages, **_kwargs):  # noqa: ANN001
        captured.update(json.loads(messages[-1]["content"]))
        return {"action": "final_answer", "answer": "합성 응답"}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)
    decision = agent._decide_next_action(
        request=ChatRequest(
            message="이 파일을 요약해줘",
            selected_files=[
                {
                    "source": "llmops",
                    "doc_id": "11111111-1111-1111-1111-111111111111",
                    "revision_id": "22222222-2222-2222-2222-222222222222",
                    "file_name": "synthetic_wbs.xlsx",
                    "title": "Synthetic WBS",
                }
            ],
        ),
        handlers={},
        observations=[],
        model="local-model",
        base_url="http://127.0.0.1:8080/v1",
        api_key="",
        step=1,
    )

    assert decision.action == "final_answer"
    assert captured["selected_files"][0]["file_name"] == "synthetic_wbs.xlsx"
    assert captured["selected_files"][0]["revision_id"] == "22222222-2222-2222-2222-222222222222"


def _runtime(**settings_kwargs):
    settings = Settings(
        llm_model="local-model",
        llm_base_url="http://localhost:8080/v1",
        find_budget_ms=1500,
        **settings_kwargs,
    )
    return PlaygroundRuntime(
        settings=settings,
        finder=FakeFinder(),
        content_searcher=FakeContentSearcher(),
        rag_searcher=FakeRagSearcher(),
    )


def test_tool_registry_exposes_builtin_tools_without_admin():
    registry = build_tool_registry(
        PlaygroundRuntime(
            settings=Settings(admin_api_token="", find_budget_ms=1500),
            finder=FakeFinder(),
            content_searcher=FakeContentSearcher(),
            rag_searcher=FakeRagSearcher(),
        )
    )

    assert set(registry) == {
        "find_folder",
        "search_content",
        "search_rag_chunks",
        "cytogenetics_karyotype_summary",
        "cytogenetics_report",
        "ngs_report",
        "refresh_content",
        "create_playground_skill",
        "extract_uploaded_document",
        "search_sop_knowledge",
        "audit_qc_report",
        "draft_qc_report",
    }
    assert registry["find_folder"].definition.default_selected is True
    assert registry["refresh_content"].definition.enabled is False
    assert registry["cytogenetics_report"].definition.permission == "read"
    assert registry["ngs_report"].definition.permission == "read"
    assert registry["cytogenetics_karyotype_summary"].definition.execution_type == "llm"
    assert registry["draft_qc_report"].definition.execution_type == "llm"
    assert registry["find_folder"].definition.category == "smb"
    assert registry["search_rag_chunks"].definition.category == "database"
    assert registry["cytogenetics_report"].definition.category == "report"
    assert registry["create_playground_skill"].definition.category == "skill"
    assert registry["create_playground_skill"].definition.permission == "write"
    assert "provider_availability" not in registry["find_folder"].definition.model_dump()
    assert "external_provider_allowed" not in registry["find_folder"].definition.model_dump()
    assert {
        tool_id for tool_id, handler in registry.items() if handler.definition.execution_type == "code"
    } == {
        "find_folder",
        "search_content",
        "search_rag_chunks",
        "cytogenetics_report",
        "ngs_report",
        "refresh_content",
        "create_playground_skill",
        "extract_uploaded_document",
        "search_sop_knowledge",
        "audit_qc_report",
    }

    result = registry["find_folder"].run({"query": "OO검사 폴더 찾아줘"})

    assert result.status == "ok"
    assert "OO검사" in result.result_text
    assert "query_len=" in result.arguments_summary


def test_tool_definition_rejects_unknown_execution_type():
    with pytest.raises(ValidationError):
        ToolDefinition(
            id="invalid_tool",
            display_name="invalid",
            description="invalid",
            category="smb",
            execution_type="workflow",
        )


def test_rag_chunk_tool_returns_vector_search_evidence_and_timings():
    result = build_tool_registry(_runtime())["search_rag_chunks"].run(
        {"query": "프로젝트 오로라 일정은?", "limit": 3}
    )

    assert result.status == "ok"
    assert "프로젝트 오로라 일정은 7월" in result.result_text
    assert "유사도 0.910" in result.result_text
    assert result.result_payload is not None
    assert result.result_payload["result_count"] == 1
    assert result.result_payload["embedding_ms"] == 4.0
    assert result.result_payload["db_ms"] == 3.0
    assert "query_len=" in result.arguments_summary
    assert "프로젝트 오로라" not in result.arguments_summary


def test_search_content_distinguishes_no_match_from_empty_index():
    class EmptyResultSearcher:
        def search(self, request):  # noqa: ANN001, ARG002
            return SimpleNamespace(
                hits=[],
                result_count=0,
                elapsed_ms=2.0,
                over_budget=False,
                indexed_files=10,
            )

    runtime = PlaygroundRuntime(
        settings=Settings(_env_file=None),
        finder=FakeFinder(),
        content_searcher=EmptyResultSearcher(),
    )

    result = build_tool_registry(runtime)["search_content"].run({"query": "없는 키워드"})

    assert "일치하는 파일을 찾지 못했습니다" in result.result_text
    assert "인덱스가 비어" not in result.result_text
    assert "indexed_files" not in result.result_payload


def test_cytogenetics_karyotype_summary_tool_uses_llm_result_without_rule_parsing():
    registry = build_tool_registry(_runtime())
    handler = registry["cytogenetics_karyotype_summary"]
    captured: dict[str, object] = {}

    def invoke_json(messages, max_tokens, purpose):  # noqa: ANN001
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        captured["purpose"] = purpose
        return {"karyotype_summary": "핵형분석요약결과: LLM이 생성한 합성 핵형 요약입니다."}

    context = ToolExecutionContext(provider="local", model="test-model", invoke_json=invoke_json)
    raw_iscn = "arr[GRCh38] 1p36.33(1_1000)x1"
    result = handler.execute({"iscn": raw_iscn}, context=context)

    assert result.status == "ok"
    assert result.result_text == "핵형분석요약결과: LLM이 생성한 합성 핵형 요약입니다."
    assert result.result_payload is not None
    assert result.result_payload["summary_kind"] == "cytogenetics_karyotype_summary"
    assert result.result_text == result.result_payload["karyotype_summary"]
    assert "clone_count" not in result.result_payload
    assert "iscn" not in result.result_payload
    assert raw_iscn not in result.arguments_summary
    assert captured["purpose"] == "cytogenetics_karyotype_summary"
    assert captured["max_tokens"] == 500
    assert raw_iscn in str(captured["messages"])


def test_cytogenetics_karyotype_summary_does_not_run_without_llm_context():
    handler = build_tool_registry(_runtime())["cytogenetics_karyotype_summary"]

    result = handler.run({"iscn": "46,XX[20]"})

    assert result.error_code == "llm_context_required"


def test_cytogenetics_karyotype_summary_only_rejects_empty_or_oversized_input():
    handler = build_tool_registry(_runtime())["cytogenetics_karyotype_summary"]
    calls: list[str] = []

    def invoke_json(messages, max_tokens, purpose):  # noqa: ANN001, ARG001
        calls.append(str(messages))
        return {"karyotype_summary": "핵형분석요약결과: LLM 결과"}

    context = ToolExecutionContext(provider="local", model="test-model", invoke_json=invoke_json)

    assert handler.execute({"iscn": ""}, context=context).error_code == "empty_iscn"
    assert handler.execute({"iscn": "X" * 501}, context=context).error_code == "iscn_too_long"
    malformed = handler.execute(
        {"iscn": "46,XX,t(9;22)(q34;q11.2[20]"},
        context=context,
    )

    assert malformed.status == "ok"
    assert len(calls) == 1


def test_karyotype_summary_registry_has_provider_neutral_contract():
    definition = build_tool_registry(_runtime())["cytogenetics_karyotype_summary"].definition.model_dump()

    assert definition["enabled"] is True
    assert definition["execution_type"] == "llm"
    assert "provider_availability" not in definition
    assert "external_provider_allowed" not in definition


def test_cytogenetics_report_tool_returns_structured_payload():
    registry = build_tool_registry(_runtime())

    result = registry["cytogenetics_report"].run({"query": "염색체 보고서 템플릿", "context": "FISH"})

    assert result.status == "ok"
    assert result.result_payload is not None
    assert result.result_payload["report_kind"] == "cytogenetics"
    assert result.result_payload["candidate_files"][0]["name"] == "report.txt"
    assert "ISCN" in result.result_text
    assert "구체적인 파일명/경로/검사 데이터는 외부 LLM observation에서 제외" in result.observation_text
    assert "report.txt" not in result.observation_text


def test_ngs_report_tool_returns_operational_checklist():
    registry = build_tool_registry(_runtime())

    result = registry["ngs_report"].run({"query": "BRCA NGS 보고서", "limit": 3})

    assert result.status == "ok"
    assert result.result_payload is not None
    assert result.result_payload["report_kind"] == "ngs"
    assert "QC 지표" in result.result_text
    assert "HGVS" in result.result_text
    assert "candidates=1" in result.arguments_summary


def test_agent_requires_local_llm_model():
    agent = PlaygroundAgent(Settings(llm_model="", llm_base_url="http://localhost:8080/v1"))

    response = agent.run(ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]), registry={})

    assert response.error_code == "local_llm_not_configured"
    assert response.tool_calls == []


def test_qwen3_local_json_request_disables_thinking_without_mutating_input():
    messages = [{"role": "user", "content": "Return agent JSON."}]

    prepared = _json_request_messages(messages, provider="local", model="qwen3:30b-a3b")
    other_model = _json_request_messages(messages, provider="local", model="qwen2.5:7b")

    assert prepared[-1]["content"].endswith("/no_think")
    assert messages == [{"role": "user", "content": "Return agent JSON."}]
    assert other_model == messages


def test_agent_reports_invalid_json_response_separately(monkeypatch):
    runtime = _runtime()
    agent = PlaygroundAgent(runtime.settings)

    def invalid_decision(**kwargs):  # noqa: ANN003, ARG001
        raise LlmResponseFormatError("empty response")

    monkeypatch.setattr(agent, "_decide_next_action", invalid_decision)

    response = agent.run(ChatRequest(message="합성 핵형 요약", selected_tool_ids=[]), registry={})

    assert response.error_code == "llm_response_invalid_json"
    assert "JSON" in response.assistant_message


def test_agent_blocks_unselected_tool_from_llm(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decisions = iter(
        [
            AgentDecision(
                action="tool_call",
                tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
            ),
            AgentDecision(action="final_answer", answer="차단된 tool 요청이 있어 실행하지 않았습니다."),
        ]
    )

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["search_content"], debug_trace=True),
        registry=registry,
    )

    assert response.tool_calls == []
    assert "unselected_tool_blocked:find_folder" in response.warnings
    assert any(step.error_code == "unselected_tool_blocked" for step in response.agent_steps)


def test_agent_executes_tool_then_final_answer(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decisions = iter(
        [
            AgentDecision(
                action="tool_call",
                tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
                rationale="폴더 위치 요청이므로 공유폴더 찾기를 사용합니다.",
            ),
            AgentDecision(action="final_answer", answer="OO검사 폴더를 찾았습니다."),
        ]
    )

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_trace=True),
        registry=registry,
    )

    assert response.error_code == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tool_id == "find_folder"
    assert response.assistant_message == "OO검사 폴더를 찾았습니다."
    assert [step.kind for step in response.agent_steps] == ["decision", "tool_call", "observation", "decision", "final"]


def test_rag_grounded_fast_path_searches_directly_and_synthesizes_once(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    llm_calls = []

    def fail_decision(**kwargs):  # noqa: ANN003, ARG001
        raise AssertionError("RAG fast path must not call the decision LLM")

    def fake_chat(messages, **kwargs):  # noqa: ANN001, ANN003
        llm_calls.append({"messages": messages, **kwargs})
        return {"answer": "오로라 일정은 7월입니다. imaginary.md를 참고했습니다."}

    monkeypatch.setattr(agent, "_decide_next_action", fail_decision)
    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(
            message="프로젝트 오로라 일정은?",
            selected_tool_ids=["search_rag_chunks"],
            selected_skill_ids=["rag-grounded-answer"],
            debug_trace=True,
        ),
        registry,
        request_id="req-rag-fast-success",
    )

    assert response.error_code == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].result_payload is not None
    assert response.tool_calls[0].result_payload["hits"][0]["file_name"] == "aurora.md"
    assert len(llm_calls) == 1
    assert llm_calls[0]["purpose"] == "rag_grounded_synthesis"
    assert llm_calls[0]["request_id"] == "req-rag-fast-success"
    assert llm_calls[0]["max_tokens"] == runtime.settings.rag_synthesis_max_tokens
    synthesis_input = str(llm_calls[0]["messages"][1]["content"])
    assert "프로젝트 오로라 일정은 7월입니다." in synthesis_input
    assert len(synthesis_input) <= runtime.settings.rag_synthesis_evidence_chars + 200
    source_list = response.assistant_message.split("근거 문서:\n", maxsplit=1)[1]
    assert source_list == "- aurora.md / 일정"
    assert "imaginary.md" not in source_list
    assert [step.kind for step in response.agent_steps] == ["tool_call", "observation", "final"]


def test_rag_synthesis_evidence_distributes_budget_across_all_hits():
    agent = PlaygroundAgent(Settings(_env_file=None, rag_synthesis_evidence_chars=800))
    hits = [
        {
            "file_name": f"synthetic-{index}.md",
            "section_path": f"section-{index}",
            "similarity": 0.9 - index / 100,
            "content": f"evidence-{index}-" + ("x" * 500),
        }
        for index in range(1, 6)
    ]

    evidence = agent._rag_synthesis_evidence(hits)

    assert len(evidence) <= 800
    for index in range(1, 6):
        assert f"synthetic-{index}.md" in evidence
        assert f"evidence-{index}-" in evidence


def test_rag_grounded_fast_path_stops_without_llm_when_search_errors(monkeypatch):
    class ErrorRagSearcher:
        def search(self, query: str, limit: int) -> RagSearchResponse:  # noqa: ARG002
            raise RagSearchError("embedding_unavailable", "로컬 임베딩 endpoint에 연결할 수 없습니다.", 12.0)

    runtime = _runtime()
    runtime = PlaygroundRuntime(
        settings=runtime.settings,
        finder=runtime.finder,
        content_searcher=runtime.content_searcher,
        rag_searcher=ErrorRagSearcher(),
    )
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    llm_call_count = 0

    def fake_chat(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        nonlocal llm_call_count
        llm_call_count += 1
        return {"answer": "호출되면 안 됩니다."}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(
            message="프로젝트 오로라 일정은?",
            selected_tool_ids=["search_rag_chunks"],
            selected_skill_ids=["rag-grounded-answer"],
        ),
        registry,
    )

    assert llm_call_count == 0
    assert response.error_code == "embedding_unavailable"
    assert response.assistant_message == "로컬 임베딩 endpoint에 연결할 수 없습니다."
    assert response.tool_calls[0].error_code == "embedding_unavailable"


def test_rag_grounded_fast_path_returns_no_hit_without_synthesis(monkeypatch):
    class EmptyRagSearcher:
        def search(self, query: str, limit: int) -> RagSearchResponse:  # noqa: ARG002
            return RagSearchResponse(
                query=query,
                hits=[],
                result_count=0,
                candidate_count=5,
                rejected_count=5,
                similarity_cutoff=0.4,
                top_similarity=0.284252,
                no_answer=True,
                embedding_model="synthetic-embedding",
            )

    runtime = _runtime()
    runtime = PlaygroundRuntime(
        settings=runtime.settings,
        finder=runtime.finder,
        content_searcher=runtime.content_searcher,
        rag_searcher=EmptyRagSearcher(),
    )
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    llm_call_count = 0

    def fake_chat(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        nonlocal llm_call_count
        llm_call_count += 1
        return {"answer": "호출되면 안 됩니다."}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(
            message="DB에 없는 합성 질문",
            selected_tool_ids=["search_rag_chunks"],
            selected_skill_ids=["rag-grounded-answer"],
        ),
        registry,
    )

    assert llm_call_count == 0
    assert response.error_code == ""
    assert response.assistant_message == (
        "답변할 만큼 충분한 문서 근거를 찾지 못했습니다. (최고 유사도 0.284, 기준 0.400)"
    )
    assert response.tool_calls[0].result_payload["result_count"] == 0
    assert response.rag_grounding is not None
    assert response.rag_grounding.decision == "insufficient_evidence"
    assert response.rag_grounding.similarity_cutoff == 0.4
    assert response.rag_grounding.top_similarity == 0.284252
    assert response.rag_grounding.rejected_count == 5
    assert "rag_insufficient_evidence" in response.warnings


def test_general_agent_stops_without_second_decision_when_rag_cutoff_rejects_all_hits(monkeypatch):
    class CutoffRagSearcher:
        def search(self, query: str, limit: int) -> RagSearchResponse:  # noqa: ARG002
            return RagSearchResponse(
                query=query,
                hits=[],
                result_count=0,
                candidate_count=2,
                rejected_count=2,
                similarity_cutoff=0.4,
                top_similarity=0.31,
                no_answer=True,
                embedding_model="synthetic-embedding",
            )

    runtime = _runtime()
    runtime = PlaygroundRuntime(
        settings=runtime.settings,
        finder=runtime.finder,
        content_searcher=runtime.content_searcher,
        rag_searcher=CutoffRagSearcher(),
    )
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decision_count = 0

    def decide(**kwargs):  # noqa: ANN003, ARG001
        nonlocal decision_count
        decision_count += 1
        return AgentDecision(
            action="tool_call",
            tool_calls=[PlannedToolCall(tool_id="search_rag_chunks", arguments={"query": "합성 질문"})],
        )

    monkeypatch.setattr(agent, "_decide_next_action", decide)

    response = agent.run(
        ChatRequest(
            message="답이 없는 합성 질문",
            selected_tool_ids=["search_rag_chunks", "find_folder"],
            debug_trace=True,
        ),
        registry,
    )

    assert decision_count == 1
    assert response.error_code == ""
    assert response.rag_grounding is not None
    assert response.rag_grounding.decision == "insufficient_evidence"
    assert response.token_usage is None
    assert [step.kind for step in response.agent_steps] == ["decision", "tool_call", "observation", "final"]


def test_general_agent_stops_after_tool_error_without_second_decision(monkeypatch):
    class ErrorRagSearcher:
        def search(self, query: str, limit: int) -> RagSearchResponse:  # noqa: ARG002
            raise RagSearchError("embedding_unavailable", "합성 임베딩 오류", 9.0)

    runtime = _runtime()
    runtime = PlaygroundRuntime(
        settings=runtime.settings,
        finder=runtime.finder,
        content_searcher=runtime.content_searcher,
        rag_searcher=ErrorRagSearcher(),
    )
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decision_count = 0

    def decide(**kwargs):  # noqa: ANN003, ARG001
        nonlocal decision_count
        decision_count += 1
        return AgentDecision(
            action="tool_call",
            tool_calls=[PlannedToolCall(tool_id="search_rag_chunks", arguments={"query": "합성 질문"})],
        )

    monkeypatch.setattr(agent, "_decide_next_action", decide)

    response = agent.run(
        ChatRequest(
            message="합성 질문",
            selected_tool_ids=["search_rag_chunks", "find_folder"],
            debug_trace=True,
        ),
        registry,
    )

    assert decision_count == 1
    assert response.error_code == "embedding_unavailable"
    assert response.assistant_message == "합성 임베딩 오류"
    assert [step.kind for step in response.agent_steps] == ["decision", "tool_call", "observation", "error"]


def test_agent_accepts_local_model_tool_call_aliases(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    def fake_chat(
        messages, *, model, base_url, max_tokens, api_key="", purpose="chat_json", debug_calls=None, **kwargs
    ):  # noqa: ANN001, ARG001
        if purpose == "agent_decision_step_1":
            return {"action": "tool_call", "tool_calls": [{"id": "find_folder", "inputs": {"query": "OO검사"}}]}
        return {"action": "final_answer", "answer": "완료"}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]),
        registry=registry,
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tool_id == "find_folder"


def test_agent_blocks_duplicate_tool_call(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    repeated = AgentDecision(
        action="tool_call",
        tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
    )
    decisions = iter([repeated, repeated])

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_trace=True),
        registry=registry,
    )

    assert len(response.tool_calls) == 1
    assert "duplicate_tool_call_blocked:find_folder" in response.warnings
    assert any(step.error_code == "duplicate_tool_call_blocked" for step in response.agent_steps)


def test_agent_hides_agent_steps_when_trace_disabled(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: AgentDecision(action="final_answer", answer="완료"))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_trace=False),
        registry=registry,
    )

    assert response.assistant_message == "완료"
    assert response.agent_steps == []


def test_raw_llm_debug_is_omitted_when_request_does_not_enable_it(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: AgentDecision(action="final_answer", answer="완료"))

    response = agent.run(
        ChatRequest(message="오로라 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_raw_llm=False),
        registry=registry,
    )

    assert response.debug is None
    assert "debug_raw_llm_denied" not in response.warnings


def test_raw_llm_debug_is_returned_when_request_enables_it(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    def fake_decide(**kwargs):  # noqa: ANN003
        debug_calls = kwargs.get("debug_calls")
        if debug_calls is not None:
            debug_calls.append(LlmDebugCall(purpose="agent_decision_step_1", raw_response='{"action":"final_answer"}'))
        return AgentDecision(action="final_answer", answer="완료")

    monkeypatch.setattr(agent, "_decide_next_action", fake_decide)

    response = agent.run(
        ChatRequest(message="오로라 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_raw_llm=True),
        registry=registry,
    )

    assert response.debug is not None
    assert response.debug.llm_calls[0].purpose == "agent_decision_step_1"


def test_safe_debug_text_redacts_secrets_and_limits_length():
    settings_kwargs = {"llm_api" + "_key": "secret-key-value"}
    agent = PlaygroundAgent(
        Settings(
            smb_host="fileserver.local",
            smb_share_name="labshare",
            smb_password="pw-value",
            admin_api_token="admin-token-value",
            playground_debug_preview_chars=60,
            **settings_kwargs,
        )
    )

    text = "http://user:pass@localhost/v1 fileserver.local labshare pw-value admin-token-value secret-key-value "
    text += "x" * 100
    safe = agent._safe_debug_text(text)

    assert "fileserver.local" not in safe
    assert "labshare" not in safe
    assert "pw-value" not in safe
    assert "admin-token-value" not in safe
    assert "secret-key-value" not in safe
    assert "user:pass" not in safe
    assert len(safe) < len(text)


def test_json_from_text_repairs_missing_outer_object_closer():
    raw = """```json
{"tool_calls":[{"tool_id":"find_folder","arguments":{"query":"BRCA"}}]
```"""

    assert _json_from_text(raw) == {
        "tool_calls": [{"tool_id": "find_folder", "arguments": {"query": "BRCA"}}],
    }


def test_llm_status_blocks_public_url():
    agent = PlaygroundAgent(Settings(llm_model="local-model", llm_base_url="http://localhost:8080/v1"))

    response = agent.check_llm(LlmStatusRequest(local_base_url="https://llm.example/v1", model="local-model"))

    assert response.error_code == "local_llm_url_not_internal"


def test_llm_status_lists_models_and_checks_chat(monkeypatch):
    fake_key = "secret-key"
    settings_kwargs = {"llm_api" + "_key": fake_key}
    agent = PlaygroundAgent(Settings(llm_model="", llm_base_url="http://localhost:8080/v1", **settings_kwargs))
    captured: dict[str, str] = {}

    def fake_models(base_url, api_key=""):  # noqa: ANN001, ARG001
        captured["models_base_url"] = base_url
        return ["model-a", "model-b"]

    def fake_chat(
        messages, *, model, base_url, max_tokens, api_key="", purpose="chat_json", debug_calls=None, **kwargs
    ):  # noqa: ANN001, ARG001
        captured["chat_base_url"] = base_url
        captured["model"] = model
        captured["purpose"] = purpose
        captured["api_key"] = api_key
        return {"ok": True}

    monkeypatch.setattr(agent, "_list_models", fake_models)
    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.check_llm(LlmStatusRequest(local_base_url="http://127.0.0.1:11434/v1", model="model-b"))

    assert response.models_ok is True
    assert response.chat_ok is True
    assert response.available_models == ["model-a", "model-b"]
    assert response.base_url_used == "http://127.0.0.1:11434/v1"
    assert response.model_used == "model-b"
    assert response.model_dump_json().find(fake_key) == -1
    assert captured == {
        "models_base_url": "http://127.0.0.1:11434/v1",
        "chat_base_url": "http://127.0.0.1:11434/v1",
        "model": "model-b",
        "purpose": "llm_status",
        "api_key": "",
    }


def test_openai_status_requires_transient_key():
    agent = PlaygroundAgent(Settings(openai_model="gpt-test", openai_api_key=""))

    response = agent.check_llm(LlmStatusRequest(provider="openai", model="gpt-test"))

    assert response.provider_used == "openai"
    assert response.error_code == "openai_api_key_missing"


def test_openai_status_uses_header_key_and_does_not_echo_it(monkeypatch):
    agent = PlaygroundAgent(Settings(openai_model="gpt-test", openai_api_key=""))
    captured: dict[str, str] = {}

    def fake_models(base_url, api_key=""):  # noqa: ANN001
        captured["models_base_url"] = base_url
        captured["models_api_key"] = api_key
        return ["gpt-test"]

    def fake_chat(
        messages, *, model, base_url, max_tokens, api_key="", purpose="chat_json", debug_calls=None, **kwargs
    ):  # noqa: ANN001, ARG001
        captured["chat_base_url"] = base_url
        captured["chat_api_key"] = api_key
        captured["model"] = model
        return {"ok": True}

    monkeypatch.setattr(agent, "_list_models", fake_models)
    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.check_llm(LlmStatusRequest(provider="openai", model="gpt-test"), "provider-token")

    assert response.provider_used == "openai"
    assert response.base_url_used == "https://api.openai.com/v1"
    assert response.chat_ok is True
    assert response.model_dump_json().find("provider-token") == -1
    assert captured == {
        "models_base_url": "https://api.openai.com/v1",
        "models_api_key": "provider-token",
        "chat_base_url": "https://api.openai.com/v1",
        "chat_api_key": "provider-token",
        "model": "gpt-test",
    }


def test_openai_provider_uses_same_tool_loop(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decisions = iter(
        [
            AgentDecision(
                action="tool_call",
                tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
            ),
            AgentDecision(action="final_answer", answer="OpenAI provider에서도 tool 결과를 사용했습니다."),
        ]
    )

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(provider="openai", model="gpt-test", message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]),
        registry,
        "provider-token",
    )

    assert response.provider_used == "openai"
    assert response.error_code == ""
    assert response.tool_calls[0].tool_id == "find_folder"
    assert "external_llm_provider:openai" in response.warnings
    assert response.model_dump_json().find("provider-token") == -1


def test_openai_provider_runs_karyotype_summary_without_special_mode(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decision = AgentDecision(
        action="tool_call",
        tool_calls=[
            PlannedToolCall(
                tool_id="cytogenetics_karyotype_summary",
                arguments={"iscn": "46,XX,t(9;22)(q34;q11.2)[20]"},
            )
        ],
    )

    def fake_decide(**kwargs):  # noqa: ANN003
        return decision

    monkeypatch.setattr(agent, "_decide_next_action", fake_decide)
    monkeypatch.setattr(
        agent,
        "_chat_json",
        lambda *args, **kwargs: {
            "karyotype_summary": "핵형분석요약결과: 46,XX 핵형에 t(9;22)(q34;q11.2)가 표기되었습니다."
        },
    )

    response = agent.run(
        ChatRequest(
            provider="openai",
            model="gpt-test",
            message="합성 테스트: 46,XX,t(9;22)(q34;q11.2)[20]",
            selected_tool_ids=["cytogenetics_karyotype_summary"],
        ),
        registry,
        "provider-token",
    )

    assert response.error_code == ""
    assert response.tool_calls[0].tool_id == "cytogenetics_karyotype_summary"
    assert not any("karyotype_test_mode" in warning for warning in response.warnings)
    assert response.assistant_message == response.tool_calls[0].result_text
    assert "t(9;22)(q34;q11.2)" in response.assistant_message
    assert "provider-token" not in response.model_dump_json()


def test_openai_tool_observation_receives_tool_result(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    observations_seen = []

    def fake_decide(**kwargs):  # noqa: ANN003
        if kwargs["step"] == 1:
            return AgentDecision(
                action="tool_call",
                tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
            )
        observations_seen.extend(kwargs["observations"])
        return AgentDecision(action="final_answer", answer="도구 결과를 확인했습니다.")

    monkeypatch.setattr(agent, "_decide_next_action", fake_decide)

    response = agent.run(
        ChatRequest(provider="openai", model="gpt-test", message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]),
        registry,
        "provider-token",
    )

    assert "external_tool_result_hidden:find_folder" not in response.warnings
    assert response.tool_calls[0].result_text.find("검사결과/2026/OO검사") >= 0
    assert observations_seen[0]["result"].find("검사결과/2026/OO검사") >= 0


def test_openai_chat_response_exposes_token_usage(monkeypatch):
    agent = PlaygroundAgent(Settings(openai_model="gpt-test", openai_api_key=""))

    class FakeResponse:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"action":"final_answer","answer":"token usage ok"}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):  # noqa: ANN201
            return self

        def __exit__(self, *args):  # noqa: ANN002, ANN201
            return False

        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            return FakeResponse()

    monkeypatch.setattr("smb_finder.playground.agent.httpx.Client", FakeClient)

    response = agent.run(
        ChatRequest(provider="openai", model="gpt-test", message="synthetic token usage check"),
        {},
        "provider-token",
    )

    assert response.assistant_message == "token usage ok"
    assert response.token_usage is not None
    assert response.token_usage.provider == "openai"
    assert response.token_usage.model == "gpt-test"
    assert response.token_usage.prompt_tokens == 11
    assert response.token_usage.completion_tokens == 4
    assert response.token_usage.total_tokens == 15
    assert response.token_usage.calls == 1
    assert response.model_dump_json().find("provider-token") == -1


def test_chat_json_reuses_http_client_and_closes_it(monkeypatch):
    created_clients = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.closed = False
            created_clients.append(self)

        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            return FakeResponse()

        def close(self):  # noqa: ANN201
            self.closed = True

    monkeypatch.setattr("smb_finder.playground.agent.httpx.Client", FakeClient)
    agent = PlaygroundAgent(Settings(_env_file=None))
    messages = [{"role": "user", "content": "synthetic keep-alive check"}]

    first = agent._chat_json(messages, model="test-model", base_url="http://localhost:8080/v1", max_tokens=64)
    second = agent._chat_json(messages, model="test-model", base_url="http://localhost:8080/v1", max_tokens=64)
    agent.close()

    assert first == {"ok": True}
    assert second == {"ok": True}
    assert len(created_clients) == 1
    assert created_clients[0].closed is True


def test_chat_json_uses_ollama_native_schema_without_thinking(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {
                "message": {"content": '{"answer":"synthetic grounded answer"}'},
                "prompt_eval_count": 12,
                "eval_count": 7,
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def post(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
            calls.append((url, kwargs["json"]))
            return FakeResponse()

        def close(self):  # noqa: ANN201
            return None

    monkeypatch.setattr("smb_finder.playground.agent.httpx.Client", FakeClient)
    agent = PlaygroundAgent(Settings(_env_file=None, ollama_base_url="http://localhost:11434"))
    usage_calls = []

    result = agent._chat_json(
        [{"role": "user", "content": "synthetic grounded question"}],
        model="qwen3:test",
        base_url="http://localhost:11434/v1",
        max_tokens=64,
        provider="local",
        usage_calls=usage_calls,
        native_ollama=True,
    )
    agent.close()

    assert result == {"answer": "synthetic grounded answer"}
    assert calls[0][0] == "http://localhost:11434/api/chat"
    assert calls[0][1]["think"] is False
    assert calls[0][1]["stream"] is False
    assert calls[0][1]["format"]["required"] == ["answer"]
    assert calls[0][1]["options"]["num_predict"] == 64
    assert usage_calls[0].total_tokens == 19
