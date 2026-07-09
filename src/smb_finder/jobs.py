"""관리 작업 job 저장소.

무거운 내용 인덱싱을 사용자 요청 핫패스에서 분리하기 위한 인메모리 job 관리자다.
운영 재시작 후에도 보존해야 하는 장기 이력은 다음 단계에서 DB/파일 저장소로 분리한다.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings
from .models import ContentIndexJobStatusResponse, RefreshContentRequest

BuildContentIndex = Callable[[Settings, str, str, str], dict]


@dataclass
class _ContentIndexJob:
    """내용 인덱싱 job 내부 상태."""

    job_id: str
    request: RefreshContentRequest
    created_at: float
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_ms: float = 0.0
    stats: dict = field(default_factory=dict)
    error_code: str = ""
    message: str = ""


class ContentIndexJobStore:
    """최근 내용 인덱싱 job을 메모리에 보관하고 실행 상태를 갱신한다."""

    def __init__(self, retention: int = 50) -> None:
        self._retention = max(1, retention)
        self._jobs: dict[str, _ContentIndexJob] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()

    def create(self, request: RefreshContentRequest) -> ContentIndexJobStatusResponse:
        """새 job을 생성하고 queued 상태로 등록한다."""
        job = _ContentIndexJob(job_id=uuid.uuid4().hex, request=request, created_at=time.time())
        with self._lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._trim_locked()
            return self._to_response(job)

    def get(self, job_id: str) -> ContentIndexJobStatusResponse | None:
        """job 상태를 조회한다."""
        with self._lock:
            job = self._jobs.get(job_id)
            return self._to_response(job) if job else None

    def recent(self, limit: int = 20) -> list[ContentIndexJobStatusResponse]:
        """최근 job 상태를 최신순으로 반환한다."""
        with self._lock:
            ids = list(reversed(self._order[-max(1, limit):]))
            return [self._to_response(self._jobs[job_id]) for job_id in ids if job_id in self._jobs]

    def run(self, job_id: str, settings: Settings, build_content_index: BuildContentIndex) -> None:
        """job을 동기 실행한다. FastAPI BackgroundTasks에서 호출된다."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "running"
            job.started_at = time.time()

        if not self._run_lock.acquire(blocking=False):
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.status = "failed"
                job.finished_at = time.time()
                job.elapsed_ms = 0.0
                job.error_code = "content_index_busy"
                job.message = "다른 내용 인덱싱 작업이 실행 중입니다. 잠시 뒤 다시 시도하세요."
            return

        started = time.perf_counter()
        try:
            try:
                stats = build_content_index(settings, job.request.path, job.request.host, job.request.share_name)
            except Exception:  # noqa: BLE001 - 응답/로그에 원문 예외를 싣지 않는다
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None:
                        return
                    job.status = "failed"
                    job.finished_at = time.time()
                    job.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                    job.error_code = "content_index_failed"
                    job.message = "내용 인덱싱 작업 중 오류가 발생했습니다."
                return

            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.stats = stats
                job.status = "succeeded"
                job.finished_at = time.time()
                job.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                job.message = "내용 인덱싱 작업이 완료되었습니다."
        finally:
            self._run_lock.release()

    def _trim_locked(self) -> None:
        """retention을 넘는 오래된 job을 제거한다."""
        while len(self._order) > self._retention:
            old_id = self._order.pop(0)
            self._jobs.pop(old_id, None)

    @staticmethod
    def _to_response(job: _ContentIndexJob) -> ContentIndexJobStatusResponse:
        """내부 job을 안전한 응답 모델로 변환한다."""
        stats = job.stats or {}
        return ContentIndexJobStatusResponse(
            job_id=job.job_id,
            status=job.status,
            path=job.request.path,
            target=stats.get("target") or ("override" if job.request.host or job.request.share_name else "configured"),
            host_override_used=bool(stats.get("host_override_used", bool(job.request.host))),
            share_override_used=bool(stats.get("share_override_used", bool(job.request.share_name))),
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            elapsed_ms=job.elapsed_ms,
            indexed=int(stats.get("indexed", 0)),
            unsupported=int(stats.get("unsupported", 0)),
            empty=int(stats.get("empty", 0)),
            errors=int(stats.get("errors", 0)),
            skipped_unchanged=int(stats.get("skipped_unchanged", 0)),
            removed_stale=int(stats.get("removed_stale", 0)),
            over_budget=bool(stats.get("over_budget", False)),
            error_code=job.error_code,
            message=job.message,
        )
