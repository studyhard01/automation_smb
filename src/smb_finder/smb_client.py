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


@dataclass
class FileEntry:
    """공유폴더 내 파일 1건 (내용 인덱싱 단위).

    abs_path는 빌드 중 파일을 읽기 위한 UNC 경로이며, 인덱스에 영속화하지 않는다.
    """

    path: str       # 공유 루트 기준 상대 경로
    name: str       # 파일명
    ext: str        # 소문자 확장자 (점 포함, 예 ".xlsx")
    size: int       # 바이트
    mtime: float    # 수정 시각 (epoch)
    abs_path: str   # UNC 절대 경로 (읽기용, 비영속)


def _ext_of(name: str) -> str:
    """파일명에서 소문자 확장자(점 포함)를 뽑는다. 없으면 빈 문자열."""
    dot = name.rfind(".")
    return name[dot:].lower() if dot != -1 else ""


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
            _logger.info("SMB 연결 성공")
            return True
        except Exception as e:  # noqa: BLE001 — 연결 실패는 단일 지점에서 처리
            _logger.error("SMB 연결 실패: %s", type(e).__name__)
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
            entries = list(smbclient.scandir(abs_path))  # 지연 이터레이터 → list로 접근 강제(에러 포착)
        except Exception as e:  # noqa: BLE001 — 접근 불가 폴더는 건너뛴다
            _logger.debug("scandir 실패(skip): %s", type(e).__name__)
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

    def walk_files(
        self,
        allow_ext: set[str],
        max_file_bytes: int,
        start_rel: str = "",
        max_depth: int = 0,
        budget_sec: float = 0,
        exclude: set[str] | None = None,
    ) -> Iterator[FileEntry]:
        """**지정 경로 아래** 파일을 순회한다 (내용 인덱스 빌드용 — 느린 작업).

        Args:
            allow_ext: 포함할 확장자 집합(소문자, 점 포함). 비면 모든 확장자.
            max_file_bytes: 이보다 큰 파일은 건너뛴다(추출 비용·지연 방지). 0=무제한.
            start_rel: 시작 폴더(공유 루트 기준 상대 경로). 빈 값이면 공유 루트 전체.
                       공유폴더 전체 대신 사용자가 원하는 폴더만 인덱싱하기 위한 범위 지정.
            max_depth: 시작 폴더로부터 최대 깊이 (0=무제한).
            budget_sec: 빌드 시간 상한(초, 0=무제한). 초과 시 그때까지 찾은 것만 내고 멈춘다.
            exclude: 제외할 폴더명 집합(소문자).

        Yields:
            FileEntry — 파일만(디렉터리는 내려가되 yield하지 않음). path는 공유 루트 기준 상대 경로.
        """
        if not self._connected:
            raise RuntimeError("SMB에 연결되어 있지 않습니다")
        start_rel = start_rel.strip().replace("\\", "/").strip("/")
        start_abs = self.root
        if start_rel:
            start_abs = self.root + "\\" + start_rel.replace("/", "\\")
        deadline = time.perf_counter() + budget_sec if budget_sec else 0.0
        yield from self._walk_files(
            start_abs, start_rel, 1, allow_ext, max_file_bytes, max_depth, deadline, exclude or set()
        )

    def _walk_files(
        self,
        abs_path: str,
        rel: str,
        depth: int,
        allow_ext: set[str],
        max_file_bytes: int,
        max_depth: int,
        deadline: float,
        exclude: set[str],
    ) -> Iterator[FileEntry]:
        """파일 재귀 순회 (내부)."""
        if deadline and time.perf_counter() > deadline:
            return
        try:
            # scandir는 지연 이터레이터 — 실제 SMB 접근은 순회 시점에 발생하므로
            # list()로 try 안에서 강제해, 잘못된/접근 불가 경로를 여기서 잡는다.
            entries = list(smbclient.scandir(abs_path))
        except Exception as e:  # noqa: BLE001 — 접근 불가/없는 폴더는 건너뛴다(상위는 0건 처리)
            _logger.debug("scandir 실패(skip): %s", type(e).__name__)
            return

        for entry in entries:
            if deadline and time.perf_counter() > deadline:
                return
            try:
                is_dir = entry.is_dir()
            except Exception:  # noqa: BLE001
                continue
            child_rel = f"{rel}/{entry.name}" if rel else entry.name

            if is_dir:
                if entry.name.lower() in exclude:
                    continue
                if max_depth and depth >= max_depth:
                    continue  # 더 내려가지 않음 (파일은 깊이 제한을 폴더 경계로 적용)
                yield from self._walk_files(
                    entry.path, child_rel, depth + 1, allow_ext, max_file_bytes,
                    max_depth, deadline, exclude,
                )
                continue

            ext = _ext_of(entry.name)
            if allow_ext and ext not in allow_ext:
                continue
            try:
                st = entry.stat()
                size = st.st_size
                mtime = float(st.st_mtime)
            except Exception:  # noqa: BLE001 — stat 실패 파일은 건너뜀
                continue
            if max_file_bytes and size > max_file_bytes:
                continue
            yield FileEntry(
                path=child_rel, name=entry.name, ext=ext, size=size, mtime=mtime, abs_path=entry.path
            )

    def read_bytes(self, abs_path: str, max_bytes: int) -> bytes:
        """파일 바이트를 읽는다 (최대 max_bytes). 읽기 실패는 호출자가 처리한다."""
        with smbclient.open_file(abs_path, mode="rb") as f:
            return f.read(max_bytes) if max_bytes else f.read()
