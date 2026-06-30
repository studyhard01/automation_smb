"""내용 인덱스 빌드 — SMB 파일 순회 → 본문 추출 → FTS5 적재.

이 파이프라인은 **느린 작업**(파일 읽기 + 추출)이므로 사용자 요청 경로가 아니라
시작 시/백그라운드/관리 트리거(/refresh-content)에서만 돈다.
시간 예산을 두고, 초과 시 그때까지 넣은 부분 인덱스로 마감한다(지연 사수).

보안: 추출 본문은 로컬 FTS5 DB에만 적재한다. 외부로 전송하지 않는다.
"""

from __future__ import annotations

import logging
import time

from . import extract
from .config import Settings
from .content_index import ContentIndex
from .smb_client import SMBClient

_logger = logging.getLogger(__name__)


def build_content_index(
    settings: Settings, subpath: str = "", host: str = "", share_name: str = ""
) -> dict:
    """지정 경로(subpath) 아래 파일 본문을 추출해 내용 인덱스에 적재한다.

    subpath가 비면 공유 루트 전체를 대상으로 한다. 지정하면 그 폴더 아래만 (재)색인하고,
    다른 폴더의 인덱스는 보존한다 — 사용자가 원하는 폴더만 골라 DB화하기 위함.

    host/share_name을 주면 그 공유폴더를 대상으로 한다(비우면 .env 기본값).
    **자격증명(아이디/비밀번호)은 항상 .env에서만** 가져온다 — 요청으로 받지 않는다(보안).

    Returns:
        통계 dict (path/host/share/indexed/unsupported/empty/errors/elapsed_sec/over_budget).
    """
    subpath = (subpath or "").strip().replace("\\", "/").strip("/")
    eff_host = (host or "").strip() or settings.smb_host
    eff_share = (share_name or "").strip() or settings.smb_share_name
    index = ContentIndex(settings.content_index_db_path)
    stats = {"path": subpath, "host": eff_host, "share": eff_share,
             "indexed": 0, "unsupported": 0, "empty": 0, "errors": 0,
             "elapsed_sec": 0.0, "over_budget": False}

    if not eff_host or not eff_share:
        _logger.warning("SMB host/share가 비어 있어 내용 인덱싱을 건너뜀 (.env 또는 요청 확인)")
        index.close()
        return stats

    client = SMBClient(
        host=eff_host,
        share_name=eff_share,
        username=settings.smb_username,   # 자격증명은 항상 .env
        password=settings.smb_password,   # 요청으로 받지 않음
    )
    if not client.connect():
        index.close()
        return stats

    started = time.perf_counter()
    budget = settings.content_index_build_budget_sec
    deadline = started + budget if budget else 0.0
    try:
        index.clear_subpath(subpath)  # 해당 폴더만 비우고 재색인(전체는 subpath="")
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
                _logger.warning("내용 인덱싱 시간 예산 초과 — 부분 인덱스로 마감")
                break
            try:
                data = client.read_bytes(fe.abs_path, settings.content_max_file_bytes)
            except Exception as e:  # noqa: BLE001 — 한 파일 읽기 실패는 건너뜀
                _logger.debug("읽기 실패 %s: %s", fe.path, e)
                stats["errors"] += 1
                continue

            result = extract.extract_text(fe.name, data, settings.content_max_chars_per_file)
            if result.status == "ok":
                index.add(
                    path=fe.path, name=fe.name, content=result.text,
                    ext=fe.ext, size=fe.size, mtime=fe.mtime,
                )
                stats["indexed"] += 1
            elif result.status == "unsupported":
                stats["unsupported"] += 1
            elif result.status == "empty":
                stats["empty"] += 1
            else:
                stats["errors"] += 1

        index.commit()
        stats["elapsed_sec"] = round(time.perf_counter() - started, 2)
        _logger.info(
            "내용 인덱싱 완료 [%s @ %s]: %d건 적재 (unsupported=%d, empty=%d, errors=%d, %.1fs%s)",
            subpath or "(전체)", eff_share, stats["indexed"], stats["unsupported"], stats["empty"],
            stats["errors"], stats["elapsed_sec"], ", 예산초과" if stats["over_budget"] else "",
        )
        return stats
    finally:
        index.close()
        client.disconnect()
