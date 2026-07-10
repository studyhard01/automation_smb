"""LangGraph 도구: smb-finder HTTP 래퍼.

진짜 검색과 인덱싱 로직은 smb-finder 서비스가 담당한다. 이 파일은 LangGraph
agent가 내부 HTTP API를 호출할 수 있게 얇게 감싼다.
"""

from __future__ import annotations

import time

import httpx
from langchain_core.tools import tool

from smb_agent.config import load_settings

_settings = load_settings()
_client = httpx.Client(base_url=_settings.smb_finder_url.rstrip("/"))


def _timeout_s() -> float:
    return max(0.1, _settings.tool_timeout_ms / 1000)


def _admin_headers() -> dict[str, str]:
    token = _settings.admin_api_token.strip()
    return {"X-Admin-Token": token} if token else {}


def _content_job_summary(data: dict, target: str) -> str:
    status = data.get("status", "unknown")
    job_id = data.get("job_id", "")
    indexed = data.get("indexed", 0)
    elapsed_ms = data.get("elapsed_ms", 0)
    base = f"'{target}' DB화 job {status}: {job_id}"
    if status == "succeeded":
        over = " (시간예산초과)" if data.get("over_budget", False) else ""
        return (
            f"'{target}' DB화 완료: {indexed}개 파일 적재"
            f" (건너뜀 {data.get('skipped_unchanged', 0)}, 삭제 {data.get('removed_stale', 0)}, "
            f"미지원 {data.get('unsupported', 0)}, 빈본문 {data.get('empty', 0)}, "
            f"오류 {data.get('errors', 0)}, {elapsed_ms}ms{over})"
        )
    if status == "failed":
        message = data.get("message") or data.get("error_code") or "알 수 없는 오류"
        return f"{base} 실패: {message}"
    return f"{base} ({elapsed_ms}ms). 아직 실행 중이면 잠시 뒤 상태를 다시 확인하세요."


def _poll_content_index_job(status_url: str, target: str) -> str:
    interval_s = max(0.1, _settings.content_index_poll_interval_ms / 1000)
    deadline = time.monotonic() + max(0.1, _settings.content_index_poll_timeout_ms / 1000)
    last_status: dict | None = None

    while True:
        resp = _client.get(status_url, headers=_admin_headers(), timeout=_timeout_s())
        resp.raise_for_status()
        last_status = resp.json()
        if last_status.get("status") in {"succeeded", "failed"}:
            return _content_job_summary(last_status, target)
        if time.monotonic() + interval_s > deadline:
            return _content_job_summary(last_status, target)
        time.sleep(interval_s)


@tool
def find_folder(query: str) -> str:
    """폴더 이름이나 경로로 사내 SMB 공유폴더를 찾는다."""
    q = (query or "").strip()
    if not q:
        return "질의가 비어 있습니다."
    payload = {"query": q, "limit": _settings.find_limit}
    try:
        resp = _client.post("/find", json=payload, timeout=_timeout_s())
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return f"timeout({_settings.tool_timeout_ms}ms) - 결과 없이 반환"
    except Exception as e:  # noqa: BLE001 - agent tool에서는 안전한 짧은 메시지만 반환
        return f"smb-finder 호출 실패: {type(e).__name__}"

    hits = data.get("hits", [])
    if not hits:
        return f"'{data.get('normalized_query', q)}' 관련 폴더를 찾지 못했습니다."
    lines = [f"'{data.get('normalized_query', q)}' 폴더 검색({len(hits)}건, {data.get('elapsed_ms', '?')}ms):"]
    lines += [f"{i + 1}. {h['name']} - {h['path']}" for i, h in enumerate(hits)]
    return "\n".join(lines)


@tool
def search_content(query: str) -> str:
    """파일 본문 내용으로 사내 SMB 파일을 찾는다."""
    q = (query or "").strip()
    if not q:
        return "질의가 비어 있습니다."
    payload = {"query": q, "limit": _settings.content_limit}
    try:
        resp = _client.post("/search-content", json=payload, timeout=_timeout_s())
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return f"timeout({_settings.tool_timeout_ms}ms) - 결과 없이 반환"
    except Exception as e:  # noqa: BLE001
        return f"smb-finder 호출 실패: {type(e).__name__}"

    hits = data.get("hits", [])
    if not hits:
        if data.get("indexed_files", 0) == 0:
            return "내용 인덱스가 비어 있습니다. 먼저 refresh_content로 폴더를 DB화하세요."
        return "본문에 일치하는 파일을 찾지 못했습니다."
    terms = " ".join(data.get("terms", []))
    lines = [f"'{terms}' 내용 검색({len(hits)}건, {data.get('elapsed_ms', '?')}ms):"]
    for i, h in enumerate(hits):
        ext = h.get("ext", "")
        lines.append(f"{i + 1}. {h['name']} ({ext}) - 경로/본문 미리보기는 보안상 표시하지 않음")
    return "\n".join(lines)


@tool
def refresh_content(path: str = "") -> str:
    """내용 검색을 위해 폴더를 관리자 job으로 로컬 DB에 인덱싱한다."""
    clean_path = path.strip() if path else ""
    payload = {"path": clean_path} if clean_path else {}
    target = clean_path or "(공유 전체)"

    if not _settings.admin_api_token.strip():
        return "ADMIN_API_TOKEN이 필요합니다. 내용 DB화는 /admin/content-index-jobs job API로만 실행합니다."

    try:
        resp = _client.post(
            "/admin/content-index-jobs",
            json=payload,
            headers=_admin_headers(),
            timeout=_timeout_s(),
        )
        resp.raise_for_status()
        created = resp.json()
        status_url = created.get("status_url") or f"/admin/content-index-jobs/{created.get('job_id', '')}"
        return _poll_content_index_job(status_url, target)
    except httpx.TimeoutException:
        return f"DB화 job 생성/상태조회 timeout({_settings.tool_timeout_ms}ms). 요청을 다시 시도하세요."
    except Exception as e:  # noqa: BLE001
        return f"smb-finder admin job 호출 실패: {type(e).__name__}"


TOOLS = [find_folder, search_content, refresh_content]
