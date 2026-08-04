"""LLM 기반 합성 QC 보고서 초안 tool 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from smb_finder.config import Settings
from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.models import AgentDecision, ChatRequest, PlannedToolCall
from smb_finder.playground.tools import PlaygroundRuntime, ToolExecutionContext, build_tool_registry


def _rules_path() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "qc_audit" / "synthetic_sop_rules.json"


def _registry(tmp_path: Path):  # noqa: ANN202
    settings = Settings(
        _env_file=None,
        llm_model="test-model",
        llm_base_url="http://127.0.0.1:11434/v1",
        playground_upload_dir=str(tmp_path),
        playground_qc_sop_path=str(_rules_path()),
    )
    return settings, build_tool_registry(PlaygroundRuntime(settings=settings))


def _narrative() -> dict[str, object]:
    return {
        "summary": "합성 측정 항목을 바탕으로 작성한 검토용 초안입니다.",
        "interpretation": "결정론적 판정과 근거를 유지했으며 사람이 내용을 확인해야 합니다.",
        "follow_up": ["담당 검토자가 판정과 근거를 확인합니다."],
    }


@pytest.mark.parametrize(
    ("temperature_c", "expected_status"),
    [(21.0, "PASS"), (23.6, "WARNING"), (26.5, "FAIL")],
)
def test_draft_tool_generates_llm_narrative_and_reaudits(
    tmp_path: Path,
    temperature_c: float,
    expected_status: str,
):
    _settings, registry = _registry(tmp_path)
    captured: dict[str, object] = {}

    def invoke_json(messages, max_tokens, purpose):  # noqa: ANN001
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        captured["purpose"] = purpose
        return _narrative()

    context = ToolExecutionContext(provider="local", model="test-model", invoke_json=invoke_json)
    result = registry["draft_qc_report"].execute(
        {
            "temperature_c": temperature_c,
            "recovery_rate_pct": 95,
            "self_check_status": "PASS",
            "operator_notes": "합성 기능 시험 메모",
        },
        context=context,
    )

    assert result.status == "ok"
    assert result.result_payload is not None
    assert result.result_payload["review_status"] == "DRAFT"
    assert result.result_payload["deterministic_status"] == expected_status
    assert result.result_payload["verification_status"] == expected_status
    assert result.result_payload["warnings"] == ["synthetic_sop_only", "draft_requires_human_review"]
    assert result.result_payload["llm_ms"] < 1000
    assert result.result_payload["verification_ms"] < 1000
    assert result.result_payload["elapsed_ms"] < 1000
    assert result.result_payload["over_budget"] is False
    assert "DRAFT - HUMAN REVIEW REQUIRED" in result.result_text
    assert f"| SYN-QC-TEMP | Aurora chamber temperature | {temperature_c:g} | °C |" in result.result_text
    assert "SYN-SOP-001 §2.1" in result.result_text
    assert "합성 기능 시험 메모" in result.result_text
    assert captured["purpose"] == "qc_report_draft"
    assert captured["max_tokens"] == 500
    assert "operator_notes" in str(captured["messages"])
    assert not list(tmp_path.iterdir())


def test_draft_tool_rejects_missing_input_without_calling_llm(tmp_path: Path):
    _settings, registry = _registry(tmp_path)
    calls: list[str] = []

    def invoke_json(messages, max_tokens, purpose):  # noqa: ANN001, ARG001
        calls.append(purpose)
        return _narrative()

    context = ToolExecutionContext(provider="local", model="test-model", invoke_json=invoke_json)
    result = registry["draft_qc_report"].execute(
        {"temperature_c": 21, "self_check_status": "PASS"},
        context=context,
    )

    assert result.status == "error"
    assert result.error_code == "qc_draft_invalid_input"
    assert calls == []


def test_draft_tool_rejects_numeric_llm_claim(tmp_path: Path):
    _settings, registry = _registry(tmp_path)

    def invoke_json(messages, max_tokens, purpose):  # noqa: ANN001, ARG001
        response = _narrative()
        response["summary"] = "검증되지 않은 측정값 이십일 대신 숫자 21을 추가했습니다."
        return response

    context = ToolExecutionContext(provider="local", model="test-model", invoke_json=invoke_json)
    result = registry["draft_qc_report"].execute(
        {"temperature_c": 21, "recovery_rate_pct": 95, "self_check_status": "PASS"},
        context=context,
    )

    assert result.status == "error"
    assert result.error_code == "qc_draft_invalid_response"
    assert not list(tmp_path.iterdir())


def test_draft_tool_requires_llm_context(tmp_path: Path):
    _settings, registry = _registry(tmp_path)

    result = registry["draft_qc_report"].run(
        {"temperature_c": 21, "recovery_rate_pct": 95, "self_check_status": "PASS"}
    )

    assert result.status == "error"
    assert result.error_code == "llm_context_required"


def test_agent_returns_verified_qc_draft_as_tool_final_answer(tmp_path: Path, monkeypatch):
    settings, registry = _registry(tmp_path)
    agent = PlaygroundAgent(settings)
    decision = AgentDecision(
        action="tool_call",
        tool_calls=[
            PlannedToolCall(
                tool_id="draft_qc_report",
                arguments={
                    "temperature_c": 21,
                    "recovery_rate_pct": 95,
                    "self_check_status": "PASS",
                },
            )
        ],
    )
    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: decision)
    monkeypatch.setattr(agent, "_chat_json", lambda *args, **kwargs: _narrative())

    response = agent.run(
        ChatRequest(
            provider="local",
            local_base_url="http://127.0.0.1:11434/v1",
            model="test-model",
            message="합성 온도와 회수율, 자체 점검 값으로 QC 보고서 초안을 작성해줘",
            selected_tool_ids=["draft_qc_report"],
        ),
        registry,
    )

    assert response.error_code == ""
    assert response.tool_calls[0].tool_id == "draft_qc_report"
    assert response.assistant_message == response.tool_calls[0].result_text
    assert "DRAFT - HUMAN REVIEW REQUIRED" in response.assistant_message
