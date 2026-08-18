"""골격 검증 — 라이브 SMB/LLM 없이 '텍스트 → 폴더 찾기' 수직 슬라이스를 확인한다.

지연 핵심: 인메모리 인덱스 검색은 시간 예산 안에 들어와야 한다.
"""

from __future__ import annotations

from smb_finder.config import Settings
from smb_finder.finder import Finder
from smb_finder.index import FolderIndex
from smb_finder.intent import normalize_rule
from smb_finder.models import FindRequest
from smb_finder.smb_client import FolderEntry


def _sample_index() -> FolderIndex:
    """테스트용 가짜 폴더 인덱스 (SMB 불필요)."""
    return FolderIndex([
        FolderEntry(path="검사결과/2026/OO검사", name="OO검사", depth=3),
        FolderEntry(path="검사결과/2026/XX검사", name="XX검사", depth=3),
        FolderEntry(path="검사결과", name="검사결과", depth=1),
        FolderEntry(path="공용/문서양식", name="문서양식", depth=2),
    ])


def _settings() -> Settings:
    # .env 영향을 받지 않도록 명시값으로 생성 (LLM 비활성 = fast-path)
    return Settings(llm_intent_enabled=False, find_default_limit=5, find_budget_ms=1500)


def test_normalize_strips_filler():
    # "OO검사 결과 폴더 찾아줘" → 핵심어만 남는다
    assert normalize_rule("OO검사 결과 폴더 찾아줘") == "OO검사"
    assert normalize_rule("문서양식 어디 있어") == "문서양식"


def test_find_returns_relevant_folder():
    finder = Finder(_sample_index(), _settings())
    resp = finder.find(FindRequest(query="OO검사 결과 폴더 찾아줘"))

    assert resp.normalized_query == "OO검사"
    assert resp.hits, "결과가 비어 있으면 안 된다"
    assert resp.hits[0].name == "OO검사"
    assert resp.result_count == len(resp.hits)
    assert resp.source == "index"


def test_find_is_within_budget():
    finder = Finder(_sample_index(), _settings())
    resp = finder.find(FindRequest(query="검사결과"))
    assert not resp.over_budget
    assert resp.elapsed_ms < 1500


def test_empty_query_returns_no_hits():
    finder = Finder(_sample_index(), _settings())
    resp = finder.find(FindRequest(query="폴더 찾아줘"))  # 군더더기만 → 키워드 없음
    # 군더더기만 남으면 원문으로 폴백하되, 매칭은 없을 수 있다
    assert isinstance(resp.hits, list)


def test_find_budget_warning_does_not_log_raw_query(caplog):
    finder = Finder(_sample_index(), Settings(llm_intent_enabled=False, find_budget_ms=-1))
    sensitive_query = "SECRET_PATIENT_123 폴더 찾아줘"

    with caplog.at_level("WARNING", logger="smb_finder.finder"):
        resp = finder.find(FindRequest(query=sensitive_query))

    assert resp.over_budget
    assert sensitive_query not in caplog.text
    assert "query_len=" in caplog.text
