"""생성된 flow JSON을 Langflow 서버에 등록한다."""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel

from .models import is_internal_http_url

DEFAULT_LANGFLOW_URL = "http://127.0.0.1:7860"


class FlowInstallResult(BaseModel):
    """Langflow 서버에 등록된 flow 정보."""

    action: str
    flow_id: str
    flow_name: str
    flow_url: str
    api_url: str
    status_code: int


def _api_headers(api_key_env: str) -> dict[str, str]:
    token = os.getenv(api_key_env, "").strip() if api_key_env else ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _flow_payload(flow: dict[str, Any]) -> dict[str, Any]:
    """Langflow FlowCreate/FlowUpdate 모델에 맞는 필드만 보낸다."""
    metadata = flow.get("metadata") or {}
    return {
        "name": flow["name"],
        "description": flow.get("description", ""),
        "data": flow["data"],
        "is_component": bool(flow.get("is_component", False)),
        "endpoint_name": f"automation_smb_{metadata.get('template_id', 'workflow')}",
        "tags": ["automation_smb", str(metadata.get("template_id", "workflow"))],
    }


class LangflowFlowInstaller:
    """Langflow HTTP API로 flow를 생성하거나 기존 자동 생성 flow를 갱신한다."""

    def __init__(
        self,
        *,
        langflow_url: str = DEFAULT_LANGFLOW_URL,
        api_key_env: str = "LANGFLOW_API_KEY",
        timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not is_internal_http_url(langflow_url):
            raise ValueError("langflow_url must be localhost, a private network URL, or an allowed internal host")
        self.langflow_url = langflow_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self.transport = transport

    def install(
        self,
        flow: dict[str, Any],
        *,
        folder_id: str = "",
        update_existing: bool = True,
    ) -> FlowInstallResult:
        """Langflow에 flow를 등록한다.

        같은 이름의 flow가 있으면 기본적으로 `PATCH /api/v1/flows/{id}`로 갱신한다. 없으면
        `POST /api/v1/flows/`로 새 flow를 만든다.
        """
        payload = _flow_payload(flow)
        if folder_id:
            payload["folder_id"] = folder_id
        headers = _api_headers(self.api_key_env)

        with httpx.Client(base_url=self.langflow_url, timeout=self.timeout_s, transport=self.transport) as client:
            if update_existing:
                existing_id = self._find_existing_flow_id(
                    client,
                    payload["name"],
                    str(payload.get("endpoint_name") or ""),
                    headers=headers,
                )
                if existing_id:
                    response = client.patch(f"/api/v1/flows/{existing_id}", json=payload, headers=headers)
                    response.raise_for_status()
                    return self._result(response, action="updated")

            response = client.post("/api/v1/flows/", json=payload, headers=headers)
            if response.status_code in {400, 409, 422} and update_existing:
                existing_id = self._find_existing_flow_id(
                    client,
                    payload["name"],
                    str(payload.get("endpoint_name") or ""),
                    headers=headers,
                )
                if existing_id:
                    response = client.patch(f"/api/v1/flows/{existing_id}", json=payload, headers=headers)
            response.raise_for_status()
            return self._result(response, action="created")

    def _find_existing_flow_id(
        self,
        client: httpx.Client,
        flow_name: str,
        endpoint_name: str,
        *,
        headers: dict[str, str],
    ) -> str:
        response = client.get(
            "/api/v1/flows/",
            params={"get_all": "true", "header_flows": "true"},
            headers=headers,
        )
        if response.status_code in {401, 403, 404}:
            return ""
        response.raise_for_status()
        flows = response.json()
        if isinstance(flows, dict):
            flows = flows.get("items", [])
        for item in flows or []:
            if item.get("name") == flow_name or (endpoint_name and item.get("endpoint_name") == endpoint_name):
                return str(item.get("id") or "")
        return ""

    def _result(self, response: httpx.Response, *, action: str) -> FlowInstallResult:
        data = response.json()
        flow_id = str(data.get("id", ""))
        flow_name = str(data.get("name", ""))
        return FlowInstallResult(
            action=action,
            flow_id=flow_id,
            flow_name=flow_name,
            flow_url=f"{self.langflow_url}/flow/{flow_id}" if flow_id else self.langflow_url,
            api_url=f"{self.langflow_url}/api/v1/flows/{flow_id}" if flow_id else f"{self.langflow_url}/api/v1/flows/",
            status_code=response.status_code,
        )


def install_flow(flow: dict[str, Any], **kwargs: Any) -> FlowInstallResult:
    """간단 호출용 헬퍼."""
    installer_kwargs = {
        key: kwargs.pop(key)
        for key in ["langflow_url", "api_key_env", "timeout_s", "transport"]
        if key in kwargs
    }
    return LangflowFlowInstaller(**installer_kwargs).install(flow, **kwargs)
