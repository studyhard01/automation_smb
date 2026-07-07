"""생성된 flow JSON을 Langflow 서버에 등록한다."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Any

import httpx
from pydantic import BaseModel

from .models import is_internal_http_url

DEFAULT_LANGFLOW_URL = "http://127.0.0.1:7860"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    return {"x-api-key": token} if token else {}


def _validate_api_key_env_name(api_key_env: str) -> str:
    """API key 값이 아니라 환경변수 이름만 받는다."""
    name = (api_key_env or "").strip()
    if not name:
        return ""
    if not ENV_NAME_RE.fullmatch(name):
        raise ValueError(
            "langflow_api_key_env에는 API key 값이 아니라 환경변수 이름을 입력해야 합니다. "
            "예: LANGFLOW_API_KEY"
        )
    return name


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


def _registry_component(registry: dict[str, Any], component_type: str) -> tuple[str, dict[str, Any]] | None:
    """Langflow `/api/v1/all` 응답에서 컴포넌트 정의를 찾는다."""
    if component_type in {"ChatInput", "ChatOutput"}:
        component = registry.get("input_output", {}).get(component_type)
        return (component_type, component) if isinstance(component, dict) else None

    custom_matches = {
        "SMBFolderFinder": "SMBFolderFinderComponent",
    }
    class_name = custom_matches.get(component_type, component_type)
    for category in registry.values():
        if not isinstance(category, dict):
            continue
        for namespaced_id, component in category.items():
            if class_name in str(namespaced_id) and isinstance(component, dict):
                return str(namespaced_id), component
    return None


def _apply_template_overrides(component: dict[str, Any], overrides: dict[str, Any]) -> None:
    """생성 스펙의 value/input_types를 Langflow 실제 템플릿에 주입한다."""
    template = component.get("template")
    if not isinstance(template, dict):
        return
    for field_name, field_override in overrides.items():
        if not isinstance(field_override, dict) or field_name not in template:
            continue
        target = template[field_name]
        if not isinstance(target, dict):
            continue
        for key in ["value", "input_types", "required", "advanced", "placeholder"]:
            if key in field_override:
                target[key] = field_override[key]


def _selected_output(component: dict[str, Any], preferred: str = "message") -> str:
    outputs = component.get("outputs") or []
    if any(output.get("name") == preferred for output in outputs if isinstance(output, dict)):
        return preferred
    for output in outputs:
        if isinstance(output, dict) and output.get("name"):
            return str(output["name"])
    return preferred


def _hydrate_node(node: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    """얇은 생성 노드를 Langflow UI가 렌더링하는 풀 노드로 보강한다."""
    data = node.get("data") or {}
    raw_node = data.get("node") or {}
    component_type = str(data.get("type") or "")
    found = _registry_component(registry, component_type)
    if found is None:
        return node

    langflow_type, component = found
    component = deepcopy(component)
    _apply_template_overrides(component, raw_node.get("template") or {})

    node_id = str(node.get("id") or data.get("id") or langflow_type)
    position = node.get("position") or {"x": 0, "y": 0}
    measured = node.get("measured") or {"width": 320, "height": 240}
    hydrated = {
        **node,
        "id": node_id,
        "type": "genericNode",
        "position": position,
        "positionAbsolute": node.get("positionAbsolute") or dict(position),
        "selected": False,
        "dragging": False,
        "measured": measured,
        "data": {
            "id": node_id,
            "type": langflow_type,
            "node": component,
            "selected_output": _selected_output(component),
            "display_name": component.get("display_name", ""),
            "description": component.get("description", ""),
        },
    }
    return hydrated


def _output_types(node: dict[str, Any], output_name: str) -> list[str]:
    outputs = ((node.get("data") or {}).get("node") or {}).get("outputs") or []
    for output in outputs:
        if isinstance(output, dict) and output.get("name") == output_name:
            return list(output.get("types") or [output.get("selected") or ""])
    return ["Message"]


def _input_meta(node: dict[str, Any], field_name: str) -> tuple[list[str], str]:
    template = ((node.get("data") or {}).get("node") or {}).get("template") or {}
    field = template.get(field_name) or {}
    if not isinstance(field, dict):
        return ["Message", "str"], "str"
    input_types = list(field.get("input_types") or field.get("inputTypes") or ["Message", "str"])
    return input_types, str(field.get("type") or "str")


def _handle_repr(value: dict[str, Any]) -> str:
    """Langflow/ReactFlow가 쓰는 handle 문자열 포맷을 만든다."""
    return json.dumps(value, separators=(",", ":")).replace('"', "œ")


def _hydrate_edges(edges: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node_by_id = {str(node.get("id")): node for node in nodes}
    hydrated_edges: list[dict[str, Any]] = []
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        source_node = node_by_id.get(source)
        target_node = node_by_id.get(target)
        edge_data = edge.get("data") or {}
        source_name = str(edge_data.get("sourceName") or edge.get("sourceHandle") or "message")
        target_name = str(edge_data.get("targetName") or edge.get("targetHandle") or "input_value")
        if not (source_node and target_node):
            hydrated_edges.append(edge)
            continue

        source_handle = {
            "dataType": (source_node.get("data") or {}).get("type", ""),
            "id": source,
            "name": source_name,
            "output_types": _output_types(source_node, source_name),
        }
        input_types, input_type = _input_meta(target_node, target_name)
        target_handle = {
            "fieldName": target_name,
            "id": target,
            "inputTypes": input_types,
            "type": input_type,
        }
        source_repr = _handle_repr(source_handle)
        target_repr = _handle_repr(target_handle)
        hydrated_edges.append(
            {
                **edge,
                "id": f"reactflow__edge-{source}{source_repr}-{target}{target_repr}",
                "source": source,
                "target": target,
                "sourceHandle": source_repr,
                "targetHandle": target_repr,
                "animated": False,
                "className": "",
                "selected": False,
                "data": {
                    "sourceHandle": source_handle,
                    "targetHandle": target_handle,
                },
            }
        )
    return hydrated_edges


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
        self.api_key_env = _validate_api_key_env_name(api_key_env)
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
        headers = _api_headers(self.api_key_env)

        with httpx.Client(base_url=self.langflow_url, timeout=self.timeout_s, transport=self.transport) as client:
            flow = self._hydrate_for_canvas(client, flow, headers=headers)
            payload = _flow_payload(flow)
            if folder_id:
                payload["folder_id"] = folder_id

            if update_existing:
                existing_id = self._find_existing_flow_id(
                    client,
                    payload["name"],
                    str(payload.get("endpoint_name") or ""),
                    headers=headers,
                )
                if existing_id:
                    response = client.patch(f"/api/v1/flows/{existing_id}", json=payload, headers=headers)
                    self._raise_for_status(response)
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
            self._raise_for_status(response)
            return self._result(response, action="created")

    def _hydrate_for_canvas(
        self,
        client: httpx.Client,
        flow: dict[str, Any],
        *,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """현재 Langflow 서버의 컴포넌트 템플릿으로 flow data를 렌더링 가능하게 보강한다."""
        try:
            response = client.get("/api/v1/all", headers=headers)
            if response.status_code != 200:
                return flow
            registry = response.json()
        except Exception:  # noqa: BLE001 - 레지스트리를 못 읽어도 기존 생성 경로는 유지한다.
            return flow
        if not isinstance(registry, dict):
            return flow

        data = deepcopy(flow.get("data") or {})
        nodes = [_hydrate_node(node, registry) for node in data.get("nodes") or []]
        data["nodes"] = nodes
        data["edges"] = _hydrate_edges(list(data.get("edges") or []), nodes)
        return {**flow, "data": data}

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

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Langflow 인증 실패를 캔버스 사용자가 바로 조치할 수 있는 메시지로 바꾼다."""
        if response.status_code not in {401, 403}:
            response.raise_for_status()
            return

        has_token = bool(os.getenv(self.api_key_env, "").strip()) if self.api_key_env else False
        token_state = (
            f"{self.api_key_env} 환경변수에 값은 있으나 Langflow가 거부했습니다. API key 값과 권한을 확인하세요."
            if has_token
            else f"{self.api_key_env} 환경변수가 비어 있습니다."
        )
        raise PermissionError(
            f"Langflow API 인증 실패({response.status_code}). 브라우저 로그인 세션은 서버에서 실행되는 "
            "커스텀 컴포넌트의 API 호출에 자동 전달되지 않습니다. Langflow에서 API key를 발급한 뒤 "
            f"{self.api_key_env} 환경변수로 컨테이너/실행 환경에 주입하세요. {token_state}"
        )

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
