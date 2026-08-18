"""Bot Core의 의존성 주입과 내부 상태 계약."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

BotRoute = Literal["document_qa", "metadata", "unsupported"]
SelectedDocumentId = tuple[str, str]


class BotGraphState(TypedDict, total=False):
    """Graph node 사이에서만 전달하는 최소 비민감 상태."""

    request_id: str
    session_id: str
    message: str
    selected_ids: tuple[SelectedDocumentId, ...]
    route: BotRoute
    citation_ids: tuple[str, ...]
    safe_error: str
    timings_ms: dict[str, float]
    elapsed_ms: float
    over_budget: bool
    node_trace: tuple[str, ...]


@dataclass(frozen=True)
class BotStepResult:
    """의존성 호출에서 graph에 반영할 수 있는 최소 결과."""

    citation_ids: tuple[str, ...] = ()
    safe_error: str = ""


class RetrieverProtocol(Protocol):
    """선택 Revision 범위에서 citation 식별자만 조회하는 계약."""

    def retrieve(self, message: str, selected_ids: tuple[SelectedDocumentId, ...]) -> BotStepResult: ...


class ModelProtocol(Protocol):
    """검증된 citation ID 범위로 모델 단계를 실행하는 계약."""

    def generate(self, message: str, citation_ids: tuple[str, ...]) -> BotStepResult: ...


class MetadataProtocol(Protocol):
    """서버 발급 문서 ID의 metadata 상태를 확인하는 계약."""

    def read(self, selected_ids: tuple[SelectedDocumentId, ...]) -> BotStepResult: ...


class BotCoreRunner(Protocol):
    """FastAPI adapter가 선택적으로 호출하는 동기 runner 계약."""

    def __call__(self, request: Any, *, request_id: str) -> Any: ...


@dataclass(frozen=True)
class BotDependencies:
    """Graph 생성 시 한 번 주입되는 의존성과 시간 예산."""

    retriever: RetrieverProtocol
    model: ModelProtocol
    metadata: MetadataProtocol
    budget_ms: int = 1000
    clock: Callable[[], float] = time.perf_counter

    def __post_init__(self) -> None:
        if self.budget_ms <= 0:
            raise ValueError("budget_ms는 1 이상이어야 합니다.")


@dataclass
class DeterministicRetrieverFake:
    """네트워크 없이 고정 citation ID를 반환하는 테스트용 fake."""

    result: BotStepResult = field(default_factory=BotStepResult)
    raise_timeout: bool = False
    call_log: list[str] | None = None
    calls: int = 0

    def retrieve(self, message: str, selected_ids: tuple[SelectedDocumentId, ...]) -> BotStepResult:
        del message, selected_ids
        self.calls += 1
        if self.call_log is not None:
            self.call_log.append("retriever")
        if self.raise_timeout:
            raise TimeoutError
        return self.result


@dataclass
class DeterministicModelFake:
    """네트워크 없이 고정 모델 단계 결과를 반환하는 테스트용 fake."""

    result: BotStepResult = field(default_factory=BotStepResult)
    raise_timeout: bool = False
    call_log: list[str] | None = None
    calls: int = 0

    def generate(self, message: str, citation_ids: tuple[str, ...]) -> BotStepResult:
        del message, citation_ids
        self.calls += 1
        if self.call_log is not None:
            self.call_log.append("model")
        if self.raise_timeout:
            raise TimeoutError
        return self.result


@dataclass
class DeterministicMetadataFake:
    """저장소 없이 고정 metadata 단계 결과를 반환하는 테스트용 fake."""

    result: BotStepResult = field(default_factory=BotStepResult)
    raise_timeout: bool = False
    call_log: list[str] | None = None
    calls: int = 0

    def read(self, selected_ids: tuple[SelectedDocumentId, ...]) -> BotStepResult:
        del selected_ids
        self.calls += 1
        if self.call_log is not None:
            self.call_log.append("metadata")
        if self.raise_timeout:
            raise TimeoutError
        return self.result
