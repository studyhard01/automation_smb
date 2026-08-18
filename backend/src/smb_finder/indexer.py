"""인덱스 빌드 — SMB를 순회해 폴더 인덱스를 만들고 캐시에 저장한다.

이 순회는 **느린 작업**이므로 사용자 요청 경로가 아니라 시작 시/백그라운드에서만 돈다.
(지연 사수: 사용자 질의는 항상 인메모리 인덱스에서 처리)
"""

from __future__ import annotations

import logging

from .config import Settings
from .index import FolderIndex
from .smb_client import SMBClient

_logger = logging.getLogger(__name__)


def build_index(settings: Settings, save: bool = True) -> FolderIndex:
    """SMB를 순회해 새 인덱스를 만든다. 연결 실패 시 빈 인덱스를 반환한다."""
    if not settings.smb_host or not settings.smb_share_name:
        _logger.warning("SMB 설정이 비어 있어 인덱싱을 건너뜀 (.env 확인)")
        return FolderIndex([])

    client = SMBClient(
        host=settings.smb_host,
        share_name=settings.smb_share_name,
        username=settings.smb_username,
        password=settings.smb_password,
    )
    if not client.connect():
        return FolderIndex([])

    try:
        entries = list(client.walk_folders(
            max_depth=settings.smb_index_max_depth,
            budget_sec=settings.smb_index_build_budget_sec,
            exclude=settings.exclude_set,
        ))
        _logger.info(
            "인덱싱 완료: 폴더 %d건 (max_depth=%d, budget=%ds)",
            len(entries), settings.smb_index_max_depth, settings.smb_index_build_budget_sec,
        )
        index = FolderIndex(entries)
        if save:
            index.save(settings.smb_index_cache_path)
        return index
    finally:
        client.disconnect()


def load_or_build(settings: Settings) -> FolderIndex:
    """캐시가 있으면 즉시 로드(빠름), 없으면 빌드한다."""
    index = FolderIndex.load(settings.smb_index_cache_path)
    if index.is_empty:
        _logger.info("인덱스 캐시 없음 — SMB 순회로 빌드")
        index = build_index(settings)
    return index
