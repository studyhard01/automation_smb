"""WorkflowSpec을 Langflow import용 JSON으로 렌더링한다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import FOLDER_SEARCH_TEMPLATE, WorkflowGenerationResult, WorkflowSpec

SCHEMA_VERSION = "automation_smb.langflow_flow.v1"


def _node(
    *,
    node_id: str,
    component_type: str,
    display_name: str,
    position: dict[str, int],
    template: dict[str, Any],
    outputs: list[dict[str, Any]],
    icon: str,
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "genericNode",
        "position": position,
        "data": {
            "id": node_id,
            "type": component_type,
            "node": {
                "display_name": display_name,
                "description": description,
                "icon": icon,
                "base_classes": [],
                "template": template,
                "outputs": outputs,
            },
        },
    }


def render_langflow_flow(
    spec: WorkflowSpec,
    *,
    generated_at: datetime | None = None,
) -> WorkflowGenerationResult:
    """기존 `SMBFolderFinder` 컴포넌트를 재사용하는 Langflow flow JSON을 만든다."""
    if not spec.supported:
        reason = spec.unsupported_reason or "unsupported workflow request"
        raise ValueError(reason)

    generated_at = generated_at or datetime.now(timezone.utc)
    chat_input_id = "ChatInput-folder-search"
    finder_id = "SMBFolderFinder-folder-search"
    chat_output_id = "ChatOutput-folder-search"

    flow = {
        "name": "SMB 공유폴더 찾기",
        "description": "사용자 채팅 입력을 기존 SMBFolderFinder 컴포넌트에 연결해 /find 검색을 실행한다.",
        "is_component": False,
        "metadata": {
            "schema": SCHEMA_VERSION,
            "generated_by": "automation_smb",
            "generated_at": generated_at.isoformat(),
            "template_id": spec.template_id,
            "template_display_name": FOLDER_SEARCH_TEMPLATE.display_name,
            "reused_component": "SMBFolderFinder",
            "service_endpoint": "/find",
            "security": [
                "SMB 자격증명은 flow JSON에 저장하지 않는다.",
                "환자/검사 파일 내용과 폴더 목록은 외부 LLM으로 보내지 않는다.",
                "검색 실행은 사내 smb-finder 서비스의 /find 호출만 사용한다.",
            ],
        },
        "data": {
            "nodes": [
                _node(
                    node_id=chat_input_id,
                    component_type="ChatInput",
                    display_name="Chat Input",
                    position={"x": 0, "y": 120},
                    icon="MessagesSquare",
                    template={
                        "input_value": {
                            "type": "str",
                            "value": "",
                            "placeholder": "찾을 공유폴더를 자연어로 입력",
                            "required": False,
                        }
                    },
                    outputs=[{"name": "message", "display_name": "Message", "types": ["Message"]}],
                ),
                _node(
                    node_id=finder_id,
                    component_type="SMBFolderFinder",
                    display_name="SMB 공유폴더 찾기",
                    position={"x": 360, "y": 120},
                    icon="folder-search",
                    description="기존 smb_finder 서비스의 POST /find를 호출하는 커스텀 컴포넌트.",
                    template={
                        "query": {
                            "type": "str",
                            "value": "",
                            "input_types": ["Message", "str"],
                            "required": True,
                        },
                        "service_url": {
                            "type": "str",
                            "value": spec.service_url,
                            "advanced": True,
                        },
                        "limit": {"type": "int", "value": spec.limit},
                        "timeout_ms": {"type": "int", "value": spec.timeout_ms, "advanced": True},
                    },
                    outputs=[
                        {"name": "folders", "display_name": "폴더 목록", "types": ["DataFrame"]},
                        {"name": "message", "display_name": "요약 메시지", "types": ["Message"]},
                    ],
                ),
                _node(
                    node_id=chat_output_id,
                    component_type="ChatOutput",
                    display_name="Chat Output",
                    position={"x": 760, "y": 120},
                    icon="MessageSquare",
                    template={
                        "input_value": {
                            "type": "str",
                            "value": "",
                            "input_types": ["Message", "str"],
                            "required": True,
                        }
                    },
                    outputs=[],
                ),
            ],
            "edges": [
                {
                    "source": chat_input_id,
                    "target": finder_id,
                    "sourceHandle": "message",
                    "targetHandle": "query",
                    "data": {"sourceName": "message", "targetName": "query"},
                },
                {
                    "source": finder_id,
                    "target": chat_output_id,
                    "sourceHandle": "message",
                    "targetHandle": "input_value",
                    "data": {"sourceName": "message", "targetName": "input_value"},
                },
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        },
    }
    return WorkflowGenerationResult(
        template_id=spec.template_id,
        provider_used=spec.planner,
        flow=flow,
        warnings=list(spec.warnings),
    )
