"""SMB 연결 및 폴더 트리 순회.

참고 구현: ../automation/src/automation/smb_converter/smb_client.py (smbclient/smbprotocol).
여기서는 '폴더 찾기'가 목적이므로 파일이 아니라 **디렉터리만** 수집한다.
순회는 인덱싱(빌드) 시에만 일어나고, 사용자 질의는 인메모리 인덱스에서 처리한다(지연 최소화).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterator

import smbclient

_logger = logging.getLogger(__name__)


@dataclass
class FolderEntry:
    """공유폴더 내 디렉터리 1건 (인덱스 단위)."""

    path: str   # 공유 루트 기준 상대 경로 (예: "검사결과/2026/OO검사")
    name: str   # 말단 폴더명
    depth: int  # 루트로부터 깊이 (루트 직하 = 1)


class SMBClient:
    """SMB 세션 관리 + 폴더 트리 순회.

    세션은 한 번 등록하고 재사용한다(매 요청 재연결 금지 — 지연 사수).
    """

    def __init__(self, host: str, share_name: str, username: str, password: str) -> None:
        self.host = host
        self.share_name = share_name
        self.username = username
        self.password = password
        self._connected = False

    def connect(self, connection_timeout: int = 10) -> bool:
        """SMB 서버에 세션을 등록한다 (연결 timeout으로 무한 대기 방지)."""
        try:
            smbclient.register_session(
                self.host,
                username=self.username,
                password=self.password,
                connection_timeout=connection_timeout,
            )
            self._connected = True
            _logger.info("SMB 연결 성공: %s", self.host)
            return True
        except Exception as e:  # noqa: BLE001 — 연결 실패는 단일 지점에서 처리
            _logger.error("SMB 연결 실패: %s", e)
            return False

    def disconnect(self) -> None:
        """세션 캐시를 정리한다."""
        smbclient.reset_connection_cache()
        self._connected = False

    @property
    def root(self) -> str:
        r"""공유 루트 UNC 경로 (\\host\share)."""
        return rf"\\{self.host}\{self.share_name}"

    def walk_folders(
        self, max_depth: int = 0, budget_sec: float = 0, exclude: set[str] | None = None
    ) -> Iterator[FolderEntry]:
        """공유 루트 아래 디렉터리를 순회한다 (인덱스 빌드용 — 느린 작업).

        Args:
            max_depth: 최대 깊이 (0=무제한). 깊이를 제한해 빌드 시간을 통제한다.
            budget_sec: 빌드 시간 상한(초, 0=무제한). 초과 시 그때까지 찾은 것만 내고 멈춘다.
            exclude: 제외할 폴더명 집합(소문자). 휴지통 등 노이즈 폴더를 건너뛴다.

        Yields:
            FolderEntry — 디렉터리만.
        """
        if not self._connected:
            raise RuntimeError("SMB에 연결되어 있지 않습니다")
        deadline = time.perf_counter() + budget_sec if budget_sec else 0.0
        yield from self._walk(self.root, "", 1, max_depth, deadline, exclude or set())

    def _walk(
        self, abs_path: str, rel: str, depth: int, max_depth: int, deadline: float, exclude: set[str]
    ) -> Iterator[FolderEntry]:
        """디렉터리 재귀 순회 (내부)."""
        if max_depth and depth > max_depth:
            return
        if deadline and time.perf_counter() > deadline:
            return
        try:
            entries = smbclient.scandir(abs_path)
        except Exception as e:  # noqa: BLE001 — 접근 불가 폴더는 건너뛴다
            _logger.debug("scandir 실패 (skip): %s (%s)", abs_path, e)
            return

        for entry in entries:
            if deadline and time.perf_counter() > deadline:
                return
            try:
                if not entry.is_dir():
                    continue
            except Exception:  # noqa: BLE001
                continue
            if entry.name.lower() in exclude:
                continue
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            yield FolderEntry(path=child_rel, name=entry.name, depth=depth)
            yield from self._walk(entry.path, child_rel, depth + 1, max_depth, deadline, exclude)
