"""활성 문서 runtime 위에 놓는 최소 Bot Core 계약."""

from .contracts import (
    BotCoreRunner,
    BotDependencies,
    BotGraphState,
    BotRoute,
    BotStepResult,
    DeterministicMetadataFake,
    DeterministicModelFake,
    DeterministicRetrieverFake,
    MetadataProtocol,
    ModelProtocol,
    RetrieverProtocol,
    SelectedDocumentId,
)
from .graph import create_bot_graph
from .model_gateway import (
    JsonModelGateway,
    ModelGatewayError,
    ModelGatewayErrorCode,
    ModelGatewayResult,
    ModelPurpose,
    ModelTokenUsage,
    OllamaModelGateway,
    is_internal_http_url,
)
from .routing import route_request

__all__ = [
    "BotCoreRunner",
    "BotDependencies",
    "BotGraphState",
    "BotRoute",
    "BotStepResult",
    "DeterministicMetadataFake",
    "DeterministicModelFake",
    "DeterministicRetrieverFake",
    "MetadataProtocol",
    "JsonModelGateway",
    "ModelGatewayError",
    "ModelGatewayErrorCode",
    "ModelGatewayResult",
    "ModelPurpose",
    "ModelTokenUsage",
    "ModelProtocol",
    "OllamaModelGateway",
    "RetrieverProtocol",
    "SelectedDocumentId",
    "create_bot_graph",
    "is_internal_http_url",
    "route_request",
]
