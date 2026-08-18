"""단일 MCP metadata 도구 계약과 bounded 실행기 테스트."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from smb_finder.config import Settings
from smb_finder.llmops_search import LlmopsFileSearcher, LlmopsSearchError
from smb_finder.tooling import (
    GET_DOCUMENT_METADATA_TOOL_DESCRIPTION,
    GET_DOCUMENT_METADATA_TOOL_NAME,
    DocumentMetadataOutput,
    GetDocumentMetadataInput,
    ToolExecutionError,
    ToolExecutor,
)


class RecordingMetadataReader:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID | None, float]] = []

    def get_document_metadata(
        self,
        doc_id: UUID,
        revision_id: UUID | None = None,
        *,
        deadline: float,
    ) -> dict[str, object]:
        self.calls.append((doc_id, revision_id, deadline))
        return _metadata_payload(doc_id, revision_id or uuid4())


def _metadata_payload(doc_id: UUID, revision_id: UUID) -> dict[str, object]:
    return {
        "source": "llmops",
        "doc_id": doc_id,
        "revision_id": revision_id,
        "is_active": True,
        "revision_status": "active",
        "extension": ".txt",
        "size_bytes": 123,
        "modified_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "elapsed_ms": 2.0,
        "over_budget": False,
        "degraded_dependencies": [],
    }


def test_metadata_contract_is_exact_and_forbids_legacy_search_fields():
    assert GET_DOCUMENT_METADATA_TOOL_NAME == "get_document_metadata"
    assert GET_DOCUMENT_METADATA_TOOL_DESCRIPTION
    assert set(GetDocumentMetadataInput.model_json_schema()["properties"]) == {"doc_id", "revision_id"}
    assert set(DocumentMetadataOutput.model_json_schema()["properties"]) == {
        "source",
        "doc_id",
        "revision_id",
        "is_active",
        "revision_status",
        "extension",
        "size_bytes",
        "modified_at",
        "elapsed_ms",
        "over_budget",
        "degraded_dependencies",
    }
    serialized = (
        DocumentMetadataOutput(
            **_metadata_payload(uuid4(), uuid4()),
        )
        .model_dump_json()
        .lower()
    )
    for forbidden in ("filename", "title", "path", "uri", "key", "body", "snippet", "query"):
        assert forbidden not in serialized


def test_executor_validates_uuid_without_calling_reader_or_echoing_input():
    reader = RecordingMetadataReader()
    executor = ToolExecutor(reader)
    invalid_doc_id = "invalid-document-id"
    try:
        with pytest.raises(ToolExecutionError) as captured:
            executor.execute({"doc_id": invalid_doc_id})
    finally:
        executor.close()

    assert captured.value.code == "invalid_arguments"
    assert invalid_doc_id not in captured.value.message
    assert reader.calls == []


def test_executor_passes_one_absolute_deadline_and_returns_exact_output():
    reader = RecordingMetadataReader()
    executor = ToolExecutor(reader)
    doc_id = uuid4()
    revision_id = uuid4()
    started = time.monotonic()
    try:
        result = executor.execute({"doc_id": str(doc_id), "revision_id": str(revision_id)})
    finally:
        executor.close()

    assert result.doc_id == doc_id
    assert result.revision_id == revision_id
    assert reader.calls[0][:2] == (doc_id, revision_id)
    assert started < reader.calls[0][2] <= started + 1.6


def test_default_executor_budget_reaches_real_llmops_metadata_adapter():
    doc_id = uuid4()
    revision_id = uuid4()
    connect_calls: list[dict[str, object]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):  # noqa: ANN002
            return None

        def execute(self, _query, _parameters):  # noqa: ANN001
            return None

        def fetchone(self):
            return {
                "doc_id": doc_id,
                "revision_id": revision_id,
                "is_active": True,
                "revision_status": "ACTIVE",
                "extension": ".txt",
                "file_size": 1,
                "source_modified_at": None,
            }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):  # noqa: ANN002
            return None

        def cursor(self, **_kwargs):  # noqa: ANN003
            return Cursor()

    def connect(**kwargs):  # noqa: ANN003
        connect_calls.append(kwargs)
        return Connection()

    searcher = LlmopsFileSearcher(
        Settings(
            _env_file=None,
            llmops_db_host="db.test",
            llmops_postgres_db="llmops_test",
            postgres_user="reader",
            postgres_password="synthetic-placeholder",
        ),
        connect=connect,
    )
    executor = ToolExecutor(searcher)
    durations: list[float] = []
    try:
        for _ in range(100):
            started = time.perf_counter()
            result = executor.execute({"doc_id": str(doc_id)})
            durations.append(time.perf_counter() - started)
    finally:
        executor.close()

    assert result.doc_id == doc_id
    assert result.revision_id == revision_id
    assert len(connect_calls) == 100
    assert all(call["connect_timeout"] == 1 for call in connect_calls)
    p95_seconds = sorted(durations)[94]
    assert p95_seconds < 0.05, f"fake-connect metadata p95={p95_seconds * 1000:.3f}ms"


def test_executor_maps_adapter_budget_error_to_safe_timeout():
    class BudgetReader(RecordingMetadataReader):
        def get_document_metadata(self, doc_id, revision_id=None, *, deadline):  # noqa: ANN001, ANN201
            raise LlmopsSearchError("metadata_budget_exhausted", "adapter detail", 15.0)

    executor = ToolExecutor(BudgetReader())
    try:
        with pytest.raises(ToolExecutionError) as captured:
            executor.execute({"doc_id": str(uuid4())})
    finally:
        executor.close()

    assert captured.value.code == "tool_timeout"
    assert "adapter detail" not in captured.value.message


def test_executor_maps_unexpected_adapter_error_without_reflecting_detail():
    class FailingReader(RecordingMetadataReader):
        def get_document_metadata(self, doc_id, revision_id=None, *, deadline):  # noqa: ANN001, ANN201
            raise ValueError("private adapter detail")

    executor = ToolExecutor(FailingReader())
    try:
        with pytest.raises(ToolExecutionError) as captured:
            executor.execute({"doc_id": str(uuid4())})
    finally:
        executor.close()

    assert captured.value.code == "internal_error"
    assert "private adapter detail" not in captured.value.message


def test_timeout_returns_promptly_and_running_worker_is_not_reused_as_queue_capacity():
    release = threading.Event()
    active = 0
    active_lock = threading.Lock()
    all_active = threading.Event()

    class BlockingReader(RecordingMetadataReader):
        def get_document_metadata(self, doc_id, revision_id=None, *, deadline):  # noqa: ANN001, ANN201
            nonlocal active
            with active_lock:
                active += 1
                if active == 2:
                    all_active.set()
            try:
                release.wait(timeout=2)
                return _metadata_payload(doc_id, revision_id or uuid4())
            finally:
                with active_lock:
                    active -= 1

    executor = ToolExecutor(BlockingReader(), timeout_ms=100, max_concurrency=2)
    errors: list[str] = []

    def invoke() -> None:
        try:
            executor.execute({"doc_id": str(uuid4())})
        except ToolExecutionError as exc:
            errors.append(exc.code)

    callers = [threading.Thread(target=invoke) for _ in range(2)]
    for caller in callers:
        caller.start()
    assert all_active.wait(timeout=1)
    started = time.monotonic()
    with pytest.raises(ToolExecutionError) as captured:
        executor.execute({"doc_id": str(uuid4())})
    elapsed = time.monotonic() - started
    release.set()
    for caller in callers:
        caller.join(timeout=1)
    executor.close()

    assert captured.value.code == "tool_timeout"
    assert elapsed < 0.05
    assert errors == []
    assert active == 0


def test_close_waits_for_cooperative_active_worker_and_returns_with_zero_active_workers():
    release = threading.Event()
    entered = threading.Event()
    active = 0

    class BlockingReader(RecordingMetadataReader):
        def get_document_metadata(self, doc_id, revision_id=None, *, deadline):  # noqa: ANN001, ANN201
            nonlocal active
            active += 1
            entered.set()
            try:
                release.wait(timeout=2)
                return _metadata_payload(doc_id, revision_id or uuid4())
            finally:
                active -= 1

    executor = ToolExecutor(BlockingReader(), timeout_ms=100, max_concurrency=1)
    caller = threading.Thread(
        target=lambda: pytest.raises(ToolExecutionError, executor.execute, {"doc_id": str(uuid4())})
    )
    caller.start()
    assert entered.wait(timeout=1)
    caller.join(timeout=1)
    close_returned = threading.Event()
    closer = threading.Thread(target=lambda: (executor.close(), close_returned.set()))
    closer.start()
    assert close_returned.wait(timeout=0.05) is False
    release.set()
    closer.join(timeout=1)

    assert close_returned.is_set() is True
    assert active == 0
    assert all(not thread.is_alive() for thread in executor._pool._threads)  # noqa: SLF001
