"""Langflow 커스텀 컴포넌트 — SMB 워크플로우 자동 생성기.

검사실 사용자가 "공유폴더 찾기 워크플로우 만들어줘"처럼 말하면, 기존 `SMBFolderFinder`
컴포넌트를 재사용하는 Langflow import용 JSON을 만든다. 검색 실행이 아니라 워크플로우 구성이 목적이다.
"""

from __future__ import annotations

import json

from lfx.custom.custom_component.component import Component
from lfx.io import DropdownInput, IntInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

try:
    from integrations.langflow.flow_builder import (
        DEFAULT_SERVICE_URL,
        LangflowFlowInstaller,
        WorkflowPlanner,
        render_langflow_flow,
    )
except ModuleNotFoundError:  # Langflow Docker에서는 integrations/ 없이 /app/flow_builder로 마운트된다.
    from flow_builder import DEFAULT_SERVICE_URL, LangflowFlowInstaller, WorkflowPlanner, render_langflow_flow


class SMBWorkflowComposerComponent(Component):
    """자연어 요구사항으로 Langflow 워크플로우 JSON을 생성한다."""

    display_name = "SMB 워크플로우 자동 생성"
    description = "채팅형 요구사항을 Langflow import용 워크플로우 JSON으로 바꾼다. 첫 템플릿은 공유폴더 찾기다."
    documentation = "https://github.com/langflow-ai/langflow"
    icon = "workflow"
    name = "SMBWorkflowComposer"

    inputs = [
        MessageTextInput(
            name="instruction",
            display_name="만들 워크플로우",
            info="예: '검사자가 공유폴더를 찾을 수 있는 workflow를 만들어줘'.",
            required=True,
            tool_mode=True,
        ),
        MessageTextInput(
            name="service_url",
            display_name="smb-finder 주소",
            info="생성될 SMBFolderFinder 컴포넌트가 호출할 사내 smb-finder base URL.",
            value=DEFAULT_SERVICE_URL,
            advanced=True,
        ),
        DropdownInput(
            name="llm_provider",
            display_name="생성 방식",
            info="auto는 로컬 LLM 설정이 있으면 사용하고, 없으면 규칙 기반으로 즉시 생성한다.",
            options=["auto", "rule", "local", "openai"],
            value="auto",
            advanced=True,
        ),
        MessageTextInput(
            name="local_llm_base_url",
            display_name="로컬 LLM 주소",
            info="OpenAI 호환 로컬 LLM base URL. 비우면 환경변수 LANGFLOW_COMPOSER_LOCAL_BASE_URL 사용.",
            value="",
            advanced=True,
        ),
        MessageTextInput(
            name="local_llm_model",
            display_name="로컬 LLM 모델",
            info="비우면 환경변수 LANGFLOW_COMPOSER_LOCAL_MODEL 사용.",
            value="",
            advanced=True,
        ),
        MessageTextInput(
            name="openai_model",
            display_name="OpenAI 모델",
            info="OpenAI는 환경변수 OPENAI_API_KEY와 LANGFLOW_COMPOSER_ALLOW_OPENAI=true가 있을 때만 사용.",
            value="",
            advanced=True,
        ),
        MessageTextInput(
            name="langflow_url",
            display_name="Langflow API 주소",
            info="생성된 workflow를 등록할 Langflow base URL. 보통 http://127.0.0.1:7860.",
            value="http://127.0.0.1:7860",
            advanced=True,
        ),
        MessageTextInput(
            name="langflow_api_key_env",
            display_name="Langflow API key 환경변수",
            info="인증이 켜진 Langflow에서만 사용. 실제 key가 아니라 환경변수 이름만 입력한다.",
            value="LANGFLOW_API_KEY",
            advanced=True,
        ),
        MessageTextInput(
            name="langflow_folder_id",
            display_name="Langflow folder id",
            info="특정 프로젝트/폴더에 넣어야 할 때만 입력한다.",
            value="",
            advanced=True,
        ),
        DropdownInput(
            name="update_existing",
            display_name="기존 자동생성 flow 갱신",
            info="같은 이름의 flow가 있으면 새로 만들지 않고 갱신한다.",
            options=["true", "false"],
            value="true",
            advanced=True,
        ),
        IntInput(
            name="limit",
            display_name="최대 결과 수",
            info="생성될 SMBFolderFinder 컴포넌트의 기본 결과 수.",
            value=5,
            advanced=True,
        ),
        IntInput(
            name="timeout_ms",
            display_name="요청 timeout(ms)",
            info="생성될 SMBFolderFinder 컴포넌트의 /find timeout.",
            value=1500,
            advanced=True,
        ),
        IntInput(
            name="langflow_timeout_ms",
            display_name="Langflow 등록 timeout(ms)",
            info="flow 등록 API 호출 timeout.",
            value=5000,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Langflow에 등록", name="installed_flow", method="install_flow"),
        Output(display_name="워크플로우 JSON", name="flow_json", method="as_data"),
        Output(display_name="안내 메시지", name="message", method="as_message"),
    ]

    _cache: dict | None = None
    _install_cache: dict | None = None

    def _generate(self) -> dict:
        instruction = (self.instruction or "").strip()
        if not instruction:
            self.status = "워크플로우 요구사항이 비어 있습니다."
            return {"error": "empty_instruction", "flow": {}, "warnings": []}

        cache_key = (
            instruction,
            self.service_url,
            self.llm_provider,
            self.local_llm_base_url,
            self.local_llm_model,
            self.openai_model,
            int(self.limit),
            int(self.timeout_ms),
        )
        if self._cache is not None and self._cache.get("_key") == cache_key:
            return self._cache

        try:
            spec = WorkflowPlanner().plan(
                instruction,
                service_url=str(self.service_url),
                limit=int(self.limit),
                timeout_ms=int(self.timeout_ms),
                llm_provider=str(self.llm_provider or "auto"),
                local_base_url=(self.local_llm_base_url or "").strip() or None,
                local_model=(self.local_llm_model or "").strip() or None,
                openai_model=(self.openai_model or "").strip() or None,
            )
            result = render_langflow_flow(spec)
        except Exception as exc:  # noqa: BLE001 - 캔버스 흐름이 끊기지 않게 오류를 메시지로 반환한다.
            message = f"워크플로우 생성 실패: {exc}"
            self.status = message
            return {"error": "generation_failed", "message": message, "flow": {}, "warnings": []}
        data = result.model_dump()
        data["_key"] = cache_key
        self._cache = data
        warning_text = f" / warning: {', '.join(result.warnings)}" if result.warnings else ""
        self.status = f"{result.template_id} 생성 완료 ({result.provider_used}){warning_text}"
        return data

    def install_flow(self) -> Data:
        """생성된 workflow를 Langflow 서버에 생성/갱신 등록한다."""
        result = self._generate()
        if result.get("error"):
            return Data(data=result)

        cache_key = (
            result.get("_key"),
            self.langflow_url,
            self.langflow_api_key_env,
            self.langflow_folder_id,
            self.update_existing,
            int(self.langflow_timeout_ms),
        )
        if self._install_cache is not None and self._install_cache.get("_key") == cache_key:
            return Data(data=self._install_cache)

        try:
            installer = LangflowFlowInstaller(
                langflow_url=str(self.langflow_url),
                api_key_env=str(self.langflow_api_key_env or "LANGFLOW_API_KEY"),
                timeout_s=max(0.1, int(self.langflow_timeout_ms) / 1000),
            )
            installed = installer.install(
                result["flow"],
                folder_id=(self.langflow_folder_id or "").strip(),
                update_existing=str(self.update_existing).lower() == "true",
            )
        except Exception as exc:  # noqa: BLE001 - 캔버스 흐름이 끊기지 않게 오류를 데이터로 반환한다.
            message = f"Langflow 등록 실패: {exc}"
            self.status = message
            return Data(data={"error": "install_failed", "message": message})

        data = installed.model_dump()
        data["_key"] = cache_key
        self._install_cache = data
        self.status = f"Langflow flow {installed.action}: {installed.flow_name}"
        return Data(data=data)

    def as_data(self) -> Data:
        """Langflow import용 JSON dict를 반환한다."""
        result = self._generate()
        return Data(data=result)

    def as_message(self) -> Message:
        """복사/저장 안내와 생성 JSON을 텍스트로 반환한다."""
        result = self._generate()
        if result.get("error"):
            return Message(text=result.get("message", "워크플로우 생성 실패"))

        flow = result["flow"]
        warnings = result.get("warnings") or []
        warning_line = f"\n주의: {', '.join(warnings)}" if warnings else ""
        text = (
            "SMB 공유폴더 찾기 워크플로우 JSON을 생성했습니다.\n"
            "원클릭 등록은 'Langflow에 등록' 출력을 실행하세요. 실패하면 JSON을 Import Flow로 가져오면 됩니다."
            f"{warning_line}\n\n"
            f"```json\n{json.dumps(flow, ensure_ascii=False, indent=2)}\n```"
        )
        return Message(text=text)
