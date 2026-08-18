"""공통 Model Gateway의 deadline·오류·schema 검증 계약을 확인한다."""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from smb_finder.bot_core import ModelGatewayError, OllamaModelGateway
from smb_finder.config import Settings


class TermsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terms: list[str]


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200, text: str | None = None) -> None:  # noqa: ANN001
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("provider status", request=request, response=response)

    def json(self):  # noqa: ANN201
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeClient:
    def __init__(self, response, *, status_code: int = 200, text: str | None = None) -> None:  # noqa: ANN001
        self.response = response
        self.status_code = status_code
        self.text = text
        self.calls: list[tuple[str, dict, float]] = []
        self.closed = False

    def post(self, url: str, *, json: dict, timeout: float):  # noqa: ANN201
        self.calls.append((url, json, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return FakeResponse(self.response, status_code=self.status_code, text=self.text)

    def close(self) -> None:
        self.closed = True


def _invoke(
    gateway: OllamaModelGateway,
    *,
    deadline: float = 100.25,
    context_window_tokens: int | None = None,
):  # noqa: ANN202
    return gateway.invoke_json(
        purpose="search_query_expansion",
        model="synthetic-model",
        messages=[{"role": "user", "content": "synthetic query"}],
        response_model=TermsPayload,
        deadline=deadline,
        max_timeout_ms=500,
        max_output_tokens=20,
        context_window_tokens=context_window_tokens,
    )


def test_gateway_uses_remaining_deadline_and_validates_payload():
    client = FakeClient(
        {
            "message": {"content": '{"terms":["plan","schedule"]}'},
            "prompt_eval_count": 7,
            "eval_count": 3,
        }
    )
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=client,
        clock=lambda: 100.0,
    )

    result = _invoke(gateway)

    assert result.payload.terms == ["plan", "schedule"]
    assert result.provider == "ollama"
    assert result.model == "synthetic-model"
    assert result.usage.total_tokens == 10
    assert client.calls[0][0] == "http://127.0.0.1:11434/api/chat"
    assert client.calls[0][2] == pytest.approx(0.25)
    assert client.calls[0][1]["format"]["additionalProperties"] is False
    assert client.calls[0][1]["options"] == {"temperature": 0, "num_predict": 20}


def test_gateway_adds_only_typed_context_window_option() -> None:
    client = FakeClient({"terms": []})
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=client,
        clock=lambda: 100.0,
    )

    _invoke(gateway, context_window_tokens=4096)

    assert client.calls[0][1]["options"] == {"temperature": 0, "num_predict": 20, "num_ctx": 4096}


@pytest.mark.parametrize(
    "client",
    [
        FakeClient({}, status_code=413, text="private provider detail"),
        FakeClient({"error": "prompt too long: private provider detail"}),
        FakeClient({}, status_code=400, text="maximum context exceeded: private provider detail"),
    ],
)
def test_gateway_maps_context_limit_without_exposing_provider_text(client: FakeClient) -> None:
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=client,
        clock=lambda: 100.0,
    )

    with pytest.raises(ModelGatewayError) as captured:
        _invoke(gateway)

    assert captured.value.code == "model_context_limit"
    assert "private provider detail" not in str(captured.value)


def test_gateway_maps_provider_grammar_rejection_to_output_schema_error() -> None:
    client = FakeClient(
        {},
        status_code=400,
        text="Failed to initialize samplers: failed to parse grammar; private provider detail",
    )
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=client,
        clock=lambda: 100.0,
    )

    with pytest.raises(ModelGatewayError) as captured:
        _invoke(gateway)

    assert captured.value.code == "output_schema_invalid"
    assert captured.value.retryable is False
    assert "private provider detail" not in str(captured.value)


@pytest.mark.parametrize(
    ("root_url", "model", "expected"),
    [
        ("", "synthetic-model", "model_not_configured"),
        ("https://public.example.com", "synthetic-model", "model_url_not_internal"),
        ("http://127.0.0.1:11434", "", "model_not_configured"),
    ],
)
def test_gateway_rejects_unconfigured_or_non_internal_provider(root_url: str, model: str, expected: str):
    client = FakeClient({"terms": []})
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url=root_url),
        client=client,
        clock=lambda: 100.0,
    )

    with pytest.raises(ModelGatewayError) as captured:
        gateway.invoke_json(
            purpose="search_query_expansion",
            model=model,
            messages=[],
            response_model=TermsPayload,
            deadline=100.25,
            max_timeout_ms=500,
            max_output_tokens=20,
        )

    assert captured.value.code == expected
    assert client.calls == []


def test_gateway_does_not_start_call_after_deadline():
    client = FakeClient({"terms": []})
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=client,
        clock=lambda: 100.0,
    )

    with pytest.raises(ModelGatewayError) as captured:
        _invoke(gateway, deadline=100.0)

    assert captured.value.code == "model_budget_exhausted"
    assert client.calls == []


def test_gateway_maps_timeout_without_exposing_exception_text():
    client = FakeClient(httpx.ReadTimeout("private provider detail"))
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=client,
        clock=lambda: 100.0,
    )

    with pytest.raises(ModelGatewayError) as captured:
        _invoke(gateway)

    assert captured.value.code == "model_timeout"
    assert captured.value.retryable is True
    assert "private provider detail" not in str(captured.value)


def test_gateway_distinguishes_invalid_json_and_schema():
    invalid_json_gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=FakeClient({"message": {"content": "not-json"}}),
        clock=lambda: 100.0,
    )
    invalid_schema_gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=FakeClient({"message": {"content": '{"other":[]}'}}),
        clock=lambda: 100.0,
    )

    with pytest.raises(ModelGatewayError) as invalid_json:
        _invoke(invalid_json_gateway)
    with pytest.raises(ModelGatewayError) as invalid_schema:
        _invoke(invalid_schema_gateway)

    assert invalid_json.value.code == "invalid_model_response"
    assert invalid_schema.value.code == "output_schema_invalid"


@pytest.mark.parametrize("message", [None, [], "content", 7])
def test_gateway_maps_non_object_message_to_invalid_model_response(message):  # noqa: ANN001
    gateway = OllamaModelGateway(
        Settings(_env_file=None, ollama_base_url="http://127.0.0.1:11434"),
        client=FakeClient({"message": message}),
        clock=lambda: 100.0,
    )

    with pytest.raises(ModelGatewayError) as captured:
        _invoke(gateway)

    assert captured.value.code == "invalid_model_response"


def test_gateway_closes_reused_client():
    client = FakeClient({"terms": []})
    gateway = OllamaModelGateway(Settings(_env_file=None), client=client)

    gateway.close()

    assert client.closed is True
