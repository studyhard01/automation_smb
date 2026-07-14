"""공통 검색 도구 계약과 실행기 테스트."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from smb_finder.config import Settings
from smb_finder.tooling import ToolCatalog, ToolExecutionError, ToolExecutor


class RecordingFinder:
    def __init__(self, *, name: str = "분자검사", path: str = "검사결과/2026/분자검사") -> None:
        self.name = name
        self.path = path
        self.requests: list[object] = []

    def find(self, request):  # noqa: ANN001
        self.requests.append(request)
        return SimpleNamespace(
            query=request.query,
            normalized_query=request.query,
            hits=[SimpleNamespace(name=self.name, path=self.path, score=0.9, depth=3)],
            result_count=1,
            elapsed_ms=3.2,
            over_budget=False,
            source="index",
        )


class RecordingSearcher:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def search(self, request):  # noqa: ANN001
        self.requests.append(request)
        return SimpleNamespace(
            query=request.query,
            terms=[request.query],
            hits=[
                SimpleNamespace(
                    name="result.txt",
                    path="검사결과/result.txt",
                    ext=".txt",
                    score=1.2,
                    snippet="외부에 노출하면 안 되는 본문",
                    size=123,
                    mtime=1.0,
                )
            ],
            result_count=1,
            elapsed_ms=4.1,
            over_budget=False,
            indexed_files=99,
        )


def _runtime(*, finder=None, content_searcher=None, **settings_kwargs):  # noqa: ANN001
    return SimpleNamespace(
        settings=Settings(_env_file=None, **settings_kwargs),
        finder=finder if finder is not None else RecordingFinder(),
        content_searcher=content_searcher if content_searcher is not None else RecordingSearcher(),
    )


def test_mcp_catalog_exposes_only_two_read_only_search_tools():
    specs = ToolCatalog().list("mcp")

    assert [spec.id for spec in specs] == ["find_folder", "search_content"]
    assert all(spec.permission == "read" for spec in specs)
    assert all(spec.read_only and spec.idempotent for spec in specs)
    assert all(not spec.destructive and not spec.open_world for spec in specs)


def test_explicit_empty_catalog_is_deny_all():
    catalog = ToolCatalog(())

    assert catalog.list("mcp") == ()
    assert catalog.get("find_folder", "mcp") is None


def test_find_folder_uses_default_limit_once_and_returns_minimal_contract():
    finder = RecordingFinder()
    executor = ToolExecutor(_runtime(finder=finder, find_default_limit=7))

    result = executor.execute("find_folder", {"query": "  분자검사  "}, surface="mcp")

    assert len(finder.requests) == 1
    assert finder.requests[0].query == "분자검사"
    assert finder.requests[0].limit == 7
    assert result.model_dump() == {
        "hits": [{"name": "분자검사", "path": "검사결과/2026/분자검사", "score": 0.9, "depth": 3}],
        "result_count": 1,
        "elapsed_ms": 3.2,
        "over_budget": False,
    }


def test_search_content_excludes_query_snippet_and_internal_metadata():
    searcher = RecordingSearcher()
    executor = ToolExecutor(_runtime(content_searcher=searcher))

    result = executor.execute("search_content", {"query": "민감 검색어", "limit": 2}, surface="mcp")
    payload = result.model_dump_json()

    assert len(searcher.requests) == 1
    assert searcher.requests[0].limit == 2
    assert "민감 검색어" not in payload
    assert "노출하면 안 되는 본문" not in payload
    assert "snippet" not in payload
    assert "indexed_files" not in payload
    assert "mtime" not in payload
    assert "size" not in payload


def test_invalid_arguments_and_logs_do_not_echo_query(caplog):
    caplog.set_level(logging.INFO, logger="smb_finder.tooling.executor")
    secret_query = "환자식별자-DO-NOT-ECHO"
    executor = ToolExecutor(_runtime())

    with pytest.raises(ToolExecutionError) as exc_info:
        executor.execute("find_folder", {"query": secret_query, "limit": 999}, surface="mcp")

    assert exc_info.value.code == "invalid_arguments"
    assert secret_query not in exc_info.value.message
    assert secret_query not in caplog.text


def test_absolute_or_unc_output_is_rejected_without_echoing_path():
    unsafe_path = r"\\internal-host\private-share\patient.txt"
    executor = ToolExecutor(_runtime(finder=RecordingFinder(path=unsafe_path)))

    with pytest.raises(ToolExecutionError) as exc_info:
        executor.execute("find_folder", {"query": "test"}, surface="mcp")

    assert exc_info.value.code == "unsafe_tool_output"
    assert unsafe_path not in exc_info.value.message


@pytest.mark.parametrize(
    ("finder", "settings_kwargs"),
    [
        (RecordingFinder(path="10.20.30.40/private"), {}),
        (RecordingFinder(name="10.20.30.40"), {}),
        (RecordingFinder(path="configured-share/folder"), {"smb_share_name": "configured-share"}),
        (
            RecordingFinder(name="report-local-secret-token.txt"),
            {"mcp_api_token": "local-secret-token"},
        ),
    ],
)
def test_internal_identifiers_and_tokens_are_rejected_from_name_and_path(finder, settings_kwargs):
    executor = ToolExecutor(_runtime(finder=finder, **settings_kwargs))

    with pytest.raises(ToolExecutionError) as exc_info:
        executor.execute("find_folder", {"query": "test"}, surface="mcp")

    assert exc_info.value.code == "unsafe_tool_output"


def test_content_index_count_is_internal_only():
    class EmptySearcher:
        def search(self, request):  # noqa: ANN001, ARG002
            return SimpleNamespace(
                hits=[],
                result_count=0,
                elapsed_ms=1.0,
                over_budget=False,
                indexed_files=10,
            )

    executor = ToolExecutor(_runtime(content_searcher=EmptySearcher()))
    result = executor.execute("search_content", {"query": "no match"}, surface="playground")

    assert result.indexed_files == 10
    assert "indexed_files" not in result.model_dump()
    assert "indexed_files" not in result.model_json_schema()["properties"]


def test_timeout_is_soft_and_returns_safe_error():
    class SlowFinder(RecordingFinder):
        def find(self, request):  # noqa: ANN001
            time.sleep(0.2)
            return super().find(request)

    executor = ToolExecutor(_runtime(finder=SlowFinder(), find_budget_ms=100), max_concurrency=1)
    started = time.perf_counter()

    with pytest.raises(ToolExecutionError) as exc_info:
        executor.execute("find_folder", {"query": "slow"}, surface="mcp")

    assert exc_info.value.code == "tool_timeout"
    assert time.perf_counter() - started < 0.18


def test_semaphore_wait_and_execution_share_one_deadline():
    class VerySlowFinder(RecordingFinder):
        def find(self, request):  # noqa: ANN001
            time.sleep(0.35)
            return super().find(request)

    executor = ToolExecutor(_runtime(finder=VerySlowFinder(), find_budget_ms=100), max_concurrency=1)
    with pytest.raises(ToolExecutionError):
        executor.execute("find_folder", {"query": "first"}, surface="mcp")

    started = time.perf_counter()
    with pytest.raises(ToolExecutionError) as exc_info:
        executor.execute("find_folder", {"query": "second"}, surface="mcp")
    elapsed = time.perf_counter() - started

    assert exc_info.value.code == "tool_busy"
    assert elapsed < 0.16
    time.sleep(0.16)
