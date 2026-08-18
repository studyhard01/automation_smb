"""내용 검색 인덱스 — SQLite FTS5(trigram) 저장/검색.

폴더 이름 검색(index.py)과 분리된, **파일 본문** 검색 저장소다.
- **trigram 토크나이저**: 한국어·영문·검사코드 모두 부분일치(substring) 검색이 된다.
  형태소 분석기 같은 외부 의존성이 필요 없다(온프레미스·SSL프록시 환경에 적합).
  단 trigram은 3글자 미만 질의를 매칭하지 못하므로, 그 경우 LIKE로 폴백한다.
- **bm25 랭킹 + snippet**: 관련도순 정렬과 본문 미리보기를 SQLite가 직접 제공한다.

⚠️ 보안: 이 DB 파일에는 환자/검사 본문이 들어간다. 원본 공유폴더와 동급의 민감 데이터로
취급한다 — 로컬에만 두고(.cache/ 는 .gitignore 대상), 외부 전송·커밋 금지.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from .models import ContentHit

_logger = logging.getLogger(__name__)

# FTS5 컬럼 순서 (snippet 컬럼 인덱스 계산에 사용): 0 path,1 name,2 content,3 ext,4 size,5 mtime
_CONTENT_COL = 2

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS files USING fts5(
    path UNINDEXED,
    name,
    content,
    ext UNINDEXED,
    size UNINDEXED,
    mtime UNINDEXED,
    tokenize = 'trigram'
);
"""


class ContentIndex:
    """파일 본문 FTS5 인덱스 (저장 + 검색)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI threadpool에서 호출되므로 스레드 경계를 허용하고,
        # 동시 접근은 _lock으로 직렬화한다(검색은 빠르므로 락 비용은 무시할 수준).
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._ensure_fts5()
        self._conn.executescript(_SCHEMA)

    def _ensure_fts5(self) -> None:
        """FTS5 컴파일 여부를 확인한다 (없으면 명확히 실패시켜 조용한 빈 결과를 막는다)."""
        try:
            self._conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
            self._conn.execute("DROP TABLE _fts5_probe")
        except sqlite3.OperationalError as e:  # pragma: no cover - 환경 의존
            raise RuntimeError("이 SQLite는 FTS5 미지원 — 내용 검색을 쓸 수 없습니다") from e

    # ── 빌드 ────────────────────────────────────────────────
    def begin_rebuild(self) -> None:
        """전체 재색인을 위해 기존 내용을 비운다."""
        with self._lock:
            self._conn.execute("DELETE FROM files")

    def clear_subpath(self, subpath: str) -> None:
        """특정 경로(폴더) 아래 항목만 비운다 (그 폴더만 재색인하기 위함).

        subpath가 비면 전체를 비운다(=begin_rebuild와 동일). 비우는 범위를 폴더 단위로
        좁혀, 한 폴더를 다시 인덱싱해도 다른 폴더의 인덱스는 보존된다(여러 폴더 누적 가능).
        """
        subpath = subpath.strip().strip("/")
        with self._lock:
            if subpath:
                self._conn.execute(
                    "DELETE FROM files WHERE path = ? OR path LIKE ?",
                    (subpath, subpath + "/%"),
                )
            else:
                self._conn.execute("DELETE FROM files")

    def metadata_by_subpath(self, subpath: str) -> dict[str, tuple[int, float]]:
        """특정 경로 아래 기존 파일의 size/mtime 메타데이터를 path 기준으로 반환한다."""
        subpath = subpath.strip().strip("/")
        with self._lock:
            if subpath:
                rows = self._conn.execute(
                    "SELECT path, size, mtime FROM files WHERE path = ? OR path LIKE ?",
                    (subpath, subpath + "/%"),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT path, size, mtime FROM files").fetchall()
        return {
            path: (int(size) if str(size).isdigit() else 0, float(mtime) if mtime else 0.0)
            for path, size, mtime in rows
        }

    def delete_path(self, path: str) -> None:
        """단일 파일 경로의 기존 인덱스를 삭제한다."""
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def delete_paths(self, paths: set[str]) -> None:
        """여러 파일 경로의 기존 인덱스를 삭제한다."""
        if not paths:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM files WHERE path = ?", [(path,) for path in paths])

    def add(self, *, path: str, name: str, content: str, ext: str, size: int, mtime: float) -> None:
        """파일 1건을 인덱스에 넣는다 (commit은 호출자가 일괄로)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO files(path, name, content, ext, size, mtime) VALUES (?,?,?,?,?,?)",
                (path, name, content, ext, str(size), str(mtime)),
            )

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def count(self) -> int:
        """인덱싱된 파일 수."""
        with self._lock:
            return int(self._conn.execute("SELECT count(*) FROM files").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 검색 ────────────────────────────────────────────────
    def search(self, terms: list[str], limit: int) -> list[ContentHit]:
        """키워드(정규화된 토큰 리스트)로 본문을 검색한다.

        - 3글자 이상 토큰은 FTS5 MATCH(부분일치 + bm25 랭킹).
        - 3글자 이상 토큰이 없으면 짧은 토큰을 LIKE로 폴백(랭킹 없음).
        반환은 관련도순. snippet으로 매칭 주변 본문 미리보기를 함께 준다.
        """
        terms = [t for t in terms if t]
        if not terms:
            return []
        fts_terms = [t for t in terms if len(t) >= 3]
        if fts_terms:
            return self._search_fts(fts_terms, limit)
        return self._search_like(max(terms, key=len), limit)

    def _search_fts(self, fts_terms: list[str], limit: int) -> list[ContentHit]:
        """FTS5 MATCH 검색 (모든 토큰 AND, bm25 오름차순=관련도 높은 순)."""
        match = " AND ".join('"%s"' % t.replace('"', '""') for t in fts_terms)
        sql = (
            f"SELECT path, name, ext, size, mtime, "
            f"snippet(files, {_CONTENT_COL}, '[', ']', '…', 12) AS snip, "
            f"bm25(files) AS rank "
            f"FROM files WHERE files MATCH ? ORDER BY rank LIMIT ?"
        )
        try:
            with self._lock:
                rows = self._conn.execute(sql, (match, limit)).fetchall()
        except sqlite3.OperationalError as e:
            _logger.warning("FTS 검색 실패(쿼리 무시): %s", e)
            return []
        return [self._row_to_hit(r, score=-float(r[6])) for r in rows]

    def _search_like(self, term: str, limit: int) -> list[ContentHit]:
        """짧은 토큰(<3자) 폴백 — content/name LIKE 부분일치 (랭킹 없음)."""
        like = f"%{term}%"
        sql = (
            "SELECT path, name, ext, size, mtime, substr(content, 1, 160) AS snip "
            "FROM files WHERE content LIKE ? OR name LIKE ? LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (like, like, limit)).fetchall()
        return [self._row_to_hit(r, score=0.0) for r in rows]

    @staticmethod
    def _row_to_hit(row: tuple, score: float) -> ContentHit:
        """SQL 행을 ContentHit으로 변환한다."""
        path, name, ext, size, mtime, snip = row[0], row[1], row[2], row[3], row[4], row[5]
        return ContentHit(
            path=path,
            name=name,
            ext=ext or "",
            size=int(size) if str(size).isdigit() else 0,
            mtime=float(mtime) if mtime else 0.0,
            score=round(score, 4),
            snippet=(snip or "").strip(),
        )


def open_index(db_path: str) -> ContentIndex:
    """내용 인덱스를 연다 (없으면 빈 인덱스 생성)."""
    return ContentIndex(db_path)
