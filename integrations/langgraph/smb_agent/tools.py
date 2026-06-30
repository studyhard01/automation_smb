"""LangGraph 도구 — smb-finder 서비스(HTTP) 래퍼.

Langflow 커스텀 컴포넌트(smb_folder_finder.py / smb_content_search.py / smb_content_indexer.py)와
**완전히 동일한 엔드포인트**(POST /find, /search-content, /refresh-content)를 호출한다.
즉 진짜 검색·인덱스 로직은 전부 smb-finder가 하고, 여기는 얇은 도구 어댑터다.

설계 원칙 (CLAUDE.md):
- **보안**: 환자/검사 데이터(경로·본문 스니펫)는 외부로 나가면 안 된다. 이 도구는 **사내 localhost의
  smb-finder만** 호출한다(config의 smb_finder_url). 외부 LLM·검색 API로 보내지 않는다.
- **지연**: timeout을 강제해 무한 대기를 막고, 초과 시 짧은 상태 메시지로 즉시 반환한다.
  결과는 상위 N건만, 사람이 읽기 좋은 짧은 텍스트로 — LLM 입력 토큰도 줄인다.
- **읽기 전용**: 찾기/인덱싱만. 공유폴더 파일 쓰기/이동/삭제 없음(/refresh-content는 로컬 인덱스만 갱신).
"""

from __future__ import annotations

import httpx
from langchain_core.tools import tool

from .config import load_settings

_settings = load_settings()
# 연결 재사용(매번 재연결 금지 — CLAUDE.md 지연 규칙). 모든 도구가 공유하는 동기 클라이언트.
_client = httpx.Client(base_url=_settings.smb_finder_url.rstrip("/"))


def _timeout_s() -> float:
    return max(0.1, _settings.tool_timeout_ms / 1000)


@tool
def find_folder(query: str) -> str:
    """폴더 **이름·경로**로 사내 SMB 공유폴더를 찾는다.

    예: 'OO검사 결과 폴더', '2026 분자유전 검사결과'. 파일 본문이 아니라 폴더 이름을 찾을 때 쓴다.
    인메모리 인덱스 검색이라 빠르다. 결과는 '이름 — 경로' 목록.
    """
    q = (query or "").strip()
    if not q:
        return "질의가 비어 있습니다."
    payload = {"query": q, "limit": _settings.find_limit}
    try:
        resp = _client.post("/find", json=payload, timeout=_timeout_s())
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return f"timeout({_settings.tool_timeout_ms}ms) — 결과 없이 반환"
    except Exception as e:  # noqa: BLE001 — 그래프 흐름이 끊기지 않게 흡수
        return f"smb-finder 호출 실패: {e}"

    hits = data.get("hits", [])
    if not hits:
        return f"'{data.get('normalized_query', q)}' 관련 폴더를 찾지 못했습니다."
    lines = [f"'{data.get('normalized_query', q)}' 폴더 검색 ({len(hits)}건, {data.get('elapsed_ms', '?')}ms):"]
    lines += [f"{i + 1}. {h['name']}  —  {h['path']}" for i, h in enumerate(hits)]
    return "\n".join(lines)


@tool
def search_content(query: str) -> str:
    """파일 **본문 내용**(키워드)으로 사내 SMB 파일을 찾는다.

    예: 'BRCA1 변이 보고서', '음성 판정 결과'. 폴더 이름이 아니라 파일 안 텍스트를 찾을 때 쓴다.
    먼저 해당 폴더가 DB화(refresh_content)돼 있어야 한다. 결과는 '파일명 — 경로 + 스니펫' 목록.
    """
    q = (query or "").strip()
    if not q:
        return "질의가 비어 있습니다."
    payload = {"query": q, "limit": _settings.content_limit}
    try:
        resp = _client.post("/search-content", json=payload, timeout=_timeout_s())
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return f"timeout({_settings.tool_timeout_ms}ms) — 결과 없이 반환"
    except Exception as e:  # noqa: BLE001
        return f"smb-finder 호출 실패: {e}"

    hits = data.get("hits", [])
    if not hits:
        if data.get("indexed_files", 0) == 0:
            return "내용 인덱스가 비어 있습니다. 먼저 refresh_content로 폴더를 DB화하세요."
        return "본문이 일치하는 파일을 찾지 못했습니다."
    terms = " ".join(data.get("terms", []))
    lines = [f"'{terms}' 내용 검색 ({len(hits)}건, {data.get('elapsed_ms', '?')}ms):"]
    for i, h in enumerate(hits):
        snip = (h.get("snippet") or "").replace("\n", " ").strip()
        line = f"{i + 1}. {h['name']}  —  {h['path']}"
        if snip:
            line += f"\n    …{snip}…"
        lines.append(line)
    return "\n".join(lines)


@tool
def refresh_content(path: str = "") -> str:
    """내용 검색을 위해 폴더를 **DB화(인덱싱)** 한다. (관리 작업, 느릴 수 있음)

    path는 공유 루트 기준 상대 경로(예: '검사결과/2026/OO검사'). 비우면 공유 전체(매우 느림).
    인덱싱은 read-only 순회로 로컬 인덱스만 갱신한다 — 공유폴더 파일은 건드리지 않는다.
    DB화가 끝나야 그 폴더를 search_content로 검색할 수 있다.
    """
    payload = {"path": path.strip()} if path and path.strip() else {}
    try:
        # 인덱싱은 추출이 무거워 timeout을 넉넉히(여기선 도구 timeout의 60배 또는 최소 60초).
        build_timeout = max(60.0, _settings.tool_timeout_ms / 1000 * 60)
        resp = _client.post("/refresh-content", json=payload, timeout=build_timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return "DB화 timeout — 부분 인덱싱됐을 수 있습니다. 잠시 후 search_content로 확인하세요."
    except Exception as e:  # noqa: BLE001
        return f"smb-finder 호출 실패: {e}"

    target = path.strip() or "(공유 전체)"
    indexed = data.get("indexed_files", data.get("count", "?"))
    return f"'{target}' DB화 완료: {indexed}개 파일 적재."


TOOLS = [find_folder, search_content, refresh_content]
