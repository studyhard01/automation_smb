"""내용 인덱스 빌드: SMB 파일 순회, 본문 추출, FTS5 적재.

이 파이프라인은 파일 읽기와 추출이 포함된 느린 작업이므로 사용자 검색 경로가 아니라
관리 트리거(`/refresh-content`, `/admin/content-index-jobs`)에서만 실행한다.
본문은 로컬 SQLite FTS DB에만 저장하고 외부로 전송하지 않는다.
"""

from __future__ import annotations

import logging
import time

from . import extract
from .config import Settings
from .content_index import ContentIndex
from .smb_client import SMBClient

_logger = logging.getLogger(__name__)


def _same_file(old_meta: tuple[int, float] | None, size: int, mtime: float) -> bool:
    """기존 인덱스 메타데이터와 현재 SMB 파일 메타데이터가 같은지 확인한다."""
    if old_meta is None:
        return False
    old_size, old_mtime = old_meta
    return old_size == size and abs(old_mtime - mtime) < 0.000001


def build_content_index(settings: Settings, subpath: str = "", host: str = "", share_name: str = "") -> dict:
    """지정 경로 아래 파일 본문을 추출해 내용 인덱스에 적재한다.

    subpath가 비면 공유 루트 전체를 대상으로 한다. 지정하면 그 폴더 아래만 증분 갱신하며,
    다른 폴더의 기존 인덱스는 보존한다. host/share_name override는 대상 공유 선택용이며
    자격증명은 항상 서버 설정(.env)에서만 읽는다.
    """
    subpath = (subpath or "").strip().replace("\\", "/").strip("/")
    host_override_used = bool((host or "").strip())
    share_override_used = bool((share_name or "").strip())
    eff_host = (host or "").strip() or settings.smb_host
    eff_share = (share_name or "").strip() or settings.smb_share_name
    index = ContentIndex(settings.content_index_db_path)
    target = "override" if host_override_used or share_override_used else "configured"
    stats = {
        "path": subpath,
        "target": target,
        "host": "override" if host_override_used else "configured",
        "share": "override" if share_override_used else "configured",
        "host_override_used": host_override_used,
        "share_override_used": share_override_used,
        "indexed": 0,
        "unsupported": 0,
        "empty": 0,
        "errors": 0,
        "skipped_unchanged": 0,
        "removed_stale": 0,
        "elapsed_sec": 0.0,
        "over_budget": False,
    }

    if not eff_host or not eff_share:
        _logger.warning("SMB host/share가 비어 있어 내용 인덱싱을 건너뜀 (.env 또는 요청 확인)")
        index.close()
        return stats

    client = SMBClient(
        host=eff_host,
        share_name=eff_share,
        username=settings.smb_username,
        password=settings.smb_password,
    )
    if not client.connect():
        index.close()
        return stats

    started = time.perf_counter()
    budget = settings.content_index_build_budget_sec
    deadline = started + budget if budget else 0.0
    try:
        existing = index.metadata_by_subpath(subpath)
        seen_paths: set[str] = set()
        files = client.walk_files(
            allow_ext=settings.content_ext_set,
            max_file_bytes=settings.content_max_file_bytes,
            start_rel=subpath,
            max_depth=settings.content_index_max_depth,
            budget_sec=budget,
            exclude=settings.exclude_set,
        )
        for fe in files:
            if deadline and time.perf_counter() > deadline:
                stats["over_budget"] = True
                _logger.warning("내용 인덱싱 시간 예산 초과: 부분 인덱스로 마감")
                break

            seen_paths.add(fe.path)
            if _same_file(existing.get(fe.path), fe.size, fe.mtime):
                stats["skipped_unchanged"] += 1
                continue

            index.delete_path(fe.path)
            try:
                data = client.read_bytes(fe.abs_path, settings.content_max_file_bytes)
            except Exception as e:  # noqa: BLE001 - 한 파일 읽기 실패는 건너뜀
                _logger.debug("읽기 실패(개별 파일 건너뜀): %s", type(e).__name__)
                stats["errors"] += 1
                continue

            result = extract.extract_text(fe.name, data, settings.content_max_chars_per_file)
            if result.status == "ok":
                index.add(
                    path=fe.path,
                    name=fe.name,
                    content=result.text,
                    ext=fe.ext,
                    size=fe.size,
                    mtime=fe.mtime,
                )
                stats["indexed"] += 1
            elif result.status == "unsupported":
                stats["unsupported"] += 1
            elif result.status == "empty":
                stats["empty"] += 1
            else:
                stats["errors"] += 1

        if not stats["over_budget"]:
            stale_paths = set(existing) - seen_paths
            index.delete_paths(stale_paths)
            stats["removed_stale"] = len(stale_paths)

        index.commit()
        stats["elapsed_sec"] = round(time.perf_counter() - started, 2)
        _logger.info(
            "내용 인덱싱 완료: target=%s scope=%s indexed=%d skipped=%d stale=%d "
            "(unsupported=%d, empty=%d, errors=%d, %.1fs%s)",
            target,
            "subpath" if subpath else "full",
            stats["indexed"],
            stats["skipped_unchanged"],
            stats["removed_stale"],
            stats["unsupported"],
            stats["empty"],
            stats["errors"],
            stats["elapsed_sec"],
            ", 예산초과" if stats["over_budget"] else "",
        )
        return stats
    finally:
        index.close()
        client.disconnect()
