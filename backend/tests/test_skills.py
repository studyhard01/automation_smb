"""Playground SKILL.md 저장·slash command·agent prompt 적용 테스트."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from smb_finder.config import Settings
from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.api import create_playground_router
from smb_finder.playground.models import ChatRequest
from smb_finder.playground.skills import SkillStore, SkillStoreError
from smb_finder.playground.tools import PlaygroundRuntime, build_tool_registry


def _document(skill_id: str, body: str = "Follow this synthetic workflow.") -> str:
    return (
        "---\n"
        f"name: {skill_id}\n"
        "description: Use for a synthetic custom workflow.\n"
        "---\n\n"
        f"# Custom Skill\n\n{body}\n"
    )


def test_builtin_skills_use_real_skill_md_shape(tmp_path):
    skills = {skill.id: skill for skill in SkillStore(tmp_path).list()}

    assert {
        "tools",
        "skills",
        "skill-creator",
        "rag-grounded-answer",
        "smb-navigation",
        "report-workflow",
        "latency-first",
    } <= set(skills)
    assert skills["tools"].document.startswith("---\nname: tools\n")
    assert skills["tools"].source == "builtin"
    assert skills["tools"].editable is False


def test_user_skill_crud_persists_as_skill_md(tmp_path):
    store = SkillStore(tmp_path)

    created = store.create("custom-flow", _document("custom-flow"))
    assert created.editable is True
    assert (tmp_path / "custom-flow" / "SKILL.md").read_text(encoding="utf-8") == created.document

    updated = store.update("custom-flow", _document("custom-flow", "Use the updated workflow."))
    assert "updated workflow" in updated.instructions

    store.delete("custom-flow")
    assert store.get("custom-flow") is None


def test_skill_frontmatter_name_must_match_directory_id(tmp_path):
    with pytest.raises(SkillStoreError) as exc_info:
        SkillStore(tmp_path).create("custom-flow", _document("different-name"))

    assert exc_info.value.code == "skill_name_mismatch"


def test_create_skill_tool_persists_actual_skill_md_without_returning_body(tmp_path):
    settings = Settings(_env_file=None, playground_skills_dir=str(tmp_path))
    tool = build_tool_registry(PlaygroundRuntime(settings=settings))["create_playground_skill"]

    result = tool.run(
        {
            "skill_id": "meeting-summary",
            "description": 'Use when the user asks for a concise "synthetic" meeting summary.',
            "instructions": "# Meeting Summary\n\nSummarize the supplied synthetic notes in three bullets.",
        }
    )

    stored = (tmp_path / "meeting-summary" / "SKILL.md").read_text(encoding="utf-8")
    assert result.status == "ok"
    assert result.result_payload == {
        "id": "meeting-summary",
        "name": "meeting-summary",
        "description": 'Use when the user asks for a concise "synthetic" meeting summary.',
        "source": "user",
        "editable": True,
    }
    assert "name: meeting-summary" in stored
    assert "# Meeting Summary" in stored
    assert SkillStore(tmp_path).get("meeting-summary").description == result.result_payload["description"]
    assert "document" not in result.result_payload
    assert "instructions" not in result.result_payload

    duplicate = tool.run(
        {
            "skill_id": "meeting-summary",
            "description": "Use for duplicate input.",
            "instructions": "Do the duplicate task.",
        }
    )
    assert duplicate.status == "error"
    assert duplicate.error_code == "skill_exists"


def test_slash_commands_do_not_require_an_llm_connection(tmp_path):
    settings = Settings(_env_file=None, llm_model="", openai_api_key="", playground_skills_dir=str(tmp_path))
    runtime = PlaygroundRuntime(settings=settings)
    agent = PlaygroundAgent(settings, skill_store=SkillStore(tmp_path))
    registry = build_tool_registry(runtime)

    tools_response = agent.run(ChatRequest(message="/tools", provider="openai"), registry)
    skills_response = agent.run(ChatRequest(message="/skills", provider="openai"), registry)

    assert tools_response.error_code == ""
    assert "find_folder" in tools_response.assistant_message
    assert tools_response.active_skill_ids == ["tools"]
    assert skills_response.error_code == ""
    assert "rag-grounded-answer" in skills_response.assistant_message
    assert skills_response.active_skill_ids == ["skills"]


def test_selected_skill_document_is_injected_into_system_prompt(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    store.create("custom-flow", _document("custom-flow", "Always answer with the synthetic workflow marker."))
    settings = Settings(
        _env_file=None,
        llm_model="local-model",
        llm_base_url="http://127.0.0.1:18080/v1",
        playground_skills_dir=str(tmp_path),
    )
    agent = PlaygroundAgent(settings, skill_store=store)
    captured: dict[str, object] = {}

    def fake_chat(messages, **kwargs):  # noqa: ANN001, ARG001
        captured["messages"] = messages
        return {"action": "final_answer", "answer": "synthetic ok", "rationale": "skill applied"}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(message="run synthetic flow", selected_skill_ids=["custom-flow"]),
        registry={},
    )

    system_message = captured["messages"][0]["content"]  # type: ignore[index]
    assert "name: custom-flow" in system_message
    assert "synthetic workflow marker" in system_message
    assert response.active_skill_ids == ["custom-flow"]


def test_skill_creator_auto_binds_tool_and_applies_created_skill_in_same_request(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        llm_model="local-model",
        llm_base_url="http://127.0.0.1:18080/v1",
        playground_skills_dir=str(tmp_path),
        playground_agent_max_steps=3,
        playground_agent_max_tool_calls=2,
    )
    store = SkillStore(tmp_path)
    agent = PlaygroundAgent(settings, skill_store=store)
    registry = build_tool_registry(PlaygroundRuntime(settings=settings))
    system_prompts: list[str] = []

    def fake_chat(messages, **kwargs):  # noqa: ANN001, ARG001
        system_prompts.append(messages[0]["content"])
        if len(system_prompts) == 1:
            return {
                "action": "tool_call",
                "tool_calls": [
                    {
                        "tool_id": "create_playground_skill",
                        "arguments": {
                            "skill_id": "meeting-summary",
                            "description": "Use when synthetic meeting notes need a concise summary.",
                            "instructions": "# Meeting Summary\n\nReturn exactly three concise bullets.",
                        },
                    }
                ],
                "rationale": "요청한 재사용 스킬을 생성합니다.",
            }
        return {"action": "final_answer", "answer": "생성 완료", "rationale": "새 스킬이 적용되었습니다."}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(
            message="합성 회의록 요약 스킬을 만들어줘",
            selected_tool_ids=[],
            selected_skill_ids=["skill-creator"],
        ),
        registry,
    )

    assert response.error_code == ""
    assert response.active_skill_ids == ["skill-creator", "meeting-summary"]
    assert response.tool_calls[0].tool_id == "create_playground_skill"
    assert response.tool_calls[0].status == "ok"
    assert response.tool_calls[0].elapsed_ms < 1000
    assert store.get("meeting-summary") is not None
    assert "name: skill-creator" in system_prompts[0]
    assert "name: meeting-summary" in system_prompts[1]


def test_skills_api_crud_uses_configured_local_directory(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_model="local-model",
        llm_base_url="http://127.0.0.1:18080/v1",
        playground_skills_dir=str(tmp_path),
    )
    app = FastAPI()
    app.include_router(create_playground_router(lambda: PlaygroundRuntime(settings=settings)))

    async def exercise_api() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/playground/skills")
            created = await client.post(
                "/api/playground/skills",
                json={"skill_id": "custom-flow", "document": _document("custom-flow")},
            )
            updated = await client.put(
                "/api/playground/skills/custom-flow",
                json={"document": _document("custom-flow", "Updated instructions.")},
            )
            deleted = await client.delete("/api/playground/skills/custom-flow")
            return listed, created, updated, deleted

    listed, created, updated, deleted = asyncio.run(exercise_api())

    assert listed.status_code == 200
    assert any(skill["id"] == "tools" for skill in listed.json())
    assert created.status_code == 201
    assert created.json()["source"] == "user"
    assert "Updated instructions" in updated.json()["instructions"]
    assert deleted.status_code == 204
