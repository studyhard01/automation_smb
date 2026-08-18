"""현재 문서 runtime과 계약이 끊긴 레거시 테스트의 명시적 수집 경계."""

# 격리 근거: ../../docs/BOT_MAIN_CORE_IMPLEMENTATION_PLAN.md#p0-1--활성-계약과-레거시-경계-고정
# 삭제된 Finder/SQLite 및 이전 RAG 설정 계약을 복원하지 않는다.
_REMOVED_FINDER_CONTRACT_TESTS = (
    "test_content.py",
    "test_finder.py",
    "test_rag_search.py",
)

# 활성 FastAPI에 연결되지 않은 이전 Playground/MCP 계약은 현재 문서 runtime과 분리한다.
_INACTIVE_PLAYGROUND_CONTRACT_TESTS = (
    "test_phase2_evaluation.py",
    "test_playground.py",
    "test_qc_audit.py",
    "test_qc_report_draft.py",
    "test_skills.py",
)

collect_ignore = [*_REMOVED_FINDER_CONTRACT_TESTS, *_INACTIVE_PLAYGROUND_CONTRACT_TESTS]

assert len(collect_ignore) == 8
assert len(set(collect_ignore)) == len(collect_ignore)


def pytest_report_header() -> list[str]:
    """수집하지 않는 레거시 모듈의 고정 범위를 pytest 결과에 노출한다."""

    return [
        "legacy quarantine: 8 modules not collected (fixed list, no globs)",
        f"  removed Finder/SQLite/RAG contracts (3): {', '.join(_REMOVED_FINDER_CONTRACT_TESTS)}",
        f"  inactive Playground contracts (5): {', '.join(_INACTIVE_PLAYGROUND_CONTRACT_TESTS)}",
    ]
