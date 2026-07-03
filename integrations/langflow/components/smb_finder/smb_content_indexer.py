"""Langflow 커스텀 컴포넌트 — 폴더 내용 DB화(인덱싱) 트리거.

사용자가 **인덱싱할 폴더 경로**를 입력하면 `smb_finder` 서비스의 관리자 job API를
호출해 그 폴더 아래 파일 본문을 추출·DB화한다. 이후 'SMB 파일 내용 검색' 컴포넌트로 찾는다.

공유폴더 전체를 한 번에 도는 대신 **원하는 폴더만** 범위로 DB화한다(지연·범위 통제).
경로를 비우면 공유 전체가 대상이 되지만, 보통은 특정 폴더를 지정한다.

설계 원칙 (CLAUDE.md):
- **보안**: 사내 localhost의 smb_finder만 호출한다. 본문은 로컬 인덱스에만 적재(외부 전송 없음).
- **지연**: DB화는 무거운 작업이라 검색 hot-path가 아니다. 사용자가 명시적으로 누를 때만 돈다.
  추출이 오래 걸릴 수 있어 timeout을 넉넉히 둔다(초과 시 흐름은 끊지 않고 상태로 보고).
- **읽기 전용**: 공유폴더 파일을 읽기만 한다. 쓰기/이동/삭제 없음.
"""

from __future__ import annotations

import asyncio

import httpx
from lfx.custom.custom_component.component import Component
from lfx.io import IntInput, MessageTextInput, Output
from lfx.schema.message import Message


class SMBContentIndexerComponent(Component):
    """지정 폴더 아래 파일 본문을 DB화한다 (smb-finder admin job 래퍼)."""

    display_name = "SMB 폴더 내용 DB화"
    description = "입력한 폴더 경로 아래 파일 본문을 추출해 검색용으로 DB화한다 (사내 smb-finder 호출)."
    documentation = "https://github.com/langflow-ai/langflow"  # 내부 문서로 교체 가능
    icon = "database"
    name = "SMBContentIndexer"

    inputs = [
        MessageTextInput(
            name="path",
            display_name="DB화할 폴더 경로",
            info="공유 루트 기준 상대 경로 (예: '검사결과/2026/OO검사'). 비우면 공유 전체(느림).",
            tool_mode=True,  # 에이전트 도구로도 노출
            required=False,
        ),
        # SMB 접속 대상(선택). 비밀번호·아이디는 보안상 여기서 받지 않는다 — 항상 서버 .env에서만.
        MessageTextInput(
            name="host",
            display_name="SMB 호스트(IP)",
            info="접속할 SMB 서버 IP. 비우면 서버 .env의 SMB_HOST 사용. (※ 내부망 정보라 플로우에 저장됨)",
            value="",
            required=False,
        ),
        MessageTextInput(
            name="share_name",
            display_name="공유폴더 이름",
            info="접속할 공유폴더명. 비우면 서버 .env의 SMB_SHARE_NAME 사용.",
            value="",
            required=False,
        ),
        MessageTextInput(
            name="service_url",
            display_name="smb-finder 주소",
            info="사내 smb-finder 서비스의 base URL. 보안상 localhost 등 사내망만 사용한다.",
            value="http://localhost:8010",
            advanced=True,
        ),
        MessageTextInput(
            name="admin_api_token",
            display_name="관리자 API 토큰",
            info="/admin/content-index-jobs 호출용 토큰. 실제 값은 Langflow secret/env에서 주입하고 저장소에 남기지 않는다.",
            value="",
            advanced=True,
        ),
        IntInput(
            name="timeout_ms",
            display_name="요청 timeout(ms)",
            info="job 생성/상태조회 1회 HTTP 요청 timeout(ms).",
            value=1500,
            advanced=True,
        ),
        IntInput(
            name="poll_interval_ms",
            display_name="job 조회 간격(ms)",
            info="관리자 job API 상태 조회 간격.",
            value=1000,
            advanced=True,
        ),
        IntInput(
            name="poll_timeout_ms",
            display_name="job 조회 예산(ms)",
            info="Langflow 컴포넌트가 job 완료를 기다리는 최대 시간. 초과하면 job_id와 현재 상태를 반환한다.",
            value=30_000,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="결과 메시지", name="message", method="run_indexing"),
    ]

    def _admin_headers(self) -> dict[str, str]:
        token = (self.admin_api_token or "").strip()
        return {"X-Admin-Token": token} if token else {}

    def _format_job_status(self, stats: dict, scope: str) -> str:
        status = stats.get("status", "unknown")
        job_id = stats.get("job_id", "")
        indexed = stats.get("indexed", 0)
        elapsed_ms = stats.get("elapsed_ms", 0)
        if status == "succeeded":
            over = stats.get("over_budget", False)
            parts = [
                f"'{scope}' DB화 완료: {indexed}개 파일 적재",
                f"(건너뜀 {stats.get('skipped_unchanged', 0)}, 삭제 {stats.get('removed_stale', 0)}, "
                f"미지원 {stats.get('unsupported', 0)}, 빈문서 {stats.get('empty', 0)}, "
                f"오류 {stats.get('errors', 0)}, {elapsed_ms}ms"
                + (", 시간예산초과 — 부분 적재" if over else "")
                + ")",
            ]
            if indexed == 0:
                parts.append("※ 0건입니다. 경로가 맞는지, .env의 SMB 자격증명이 채워졌는지 확인하세요.")
            return " ".join(parts)
        if status == "failed":
            msg = stats.get("message") or stats.get("error_code") or "알 수 없는 오류"
            return f"'{scope}' DB화 job 실패: {msg}"
        return f"'{scope}' DB화 job {status}: {job_id} ({elapsed_ms}ms). 아직 실행 중이면 잠시 뒤 상태를 다시 확인하세요."

    async def _poll_job(self, client: httpx.AsyncClient, status_url: str, scope: str) -> dict:
        deadline = asyncio.get_running_loop().time() + max(0.1, int(self.poll_timeout_ms) / 1000)
        interval_s = max(0.1, int(self.poll_interval_ms) / 1000)
        last_status: dict = {}

        while True:
            resp = await client.get(status_url, headers=self._admin_headers())
            resp.raise_for_status()
            last_status = resp.json()
            if last_status.get("status") in {"succeeded", "failed"}:
                return last_status
            if asyncio.get_running_loop().time() + interval_s > deadline:
                return last_status
            await asyncio.sleep(interval_s)

    async def run_indexing(self) -> Message:
        """smb-finder 관리자 job API로 폴더 DB화를 시작하고 상태를 요약한다."""
        path = (self.path or "").strip()
        base_url = str(self.service_url).rstrip("/")
        timeout_s = max(0.1, int(self.timeout_ms) / 1000)
        payload = {"path": path, "host": (self.host or "").strip(), "share_name": (self.share_name or "").strip()}
        scope = path or "(공유 전체)"
        if not (self.admin_api_token or "").strip():
            msg = "ADMIN_API_TOKEN이 필요합니다. 내용 DB화는 /admin/content-index-jobs job API로만 실행합니다."
            self.status = msg
            return Message(text=msg)

        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
                resp = await client.post(
                    "/admin/content-index-jobs",
                    json=payload,
                    headers=self._admin_headers(),
                )
                resp.raise_for_status()
                created = resp.json()
                status_url = created.get("status_url") or f"/admin/content-index-jobs/{created.get('job_id', '')}"
                stats = await self._poll_job(client, status_url, scope)
                text = self._format_job_status(stats, scope)
        except httpx.TimeoutException:
            msg = f"timeout({self.timeout_ms}ms) — DB화 job 생성/상태조회가 지연됩니다. 잠시 뒤 다시 확인하세요."
            self.status = msg
            return Message(text=msg)
        except Exception as e:  # noqa: BLE001 — 노코드 캔버스에서 흐름이 끊기지 않게 흡수
            msg = f"DB화 호출 실패: {e}"
            self.status = msg
            return Message(text=msg)

        self.status = text
        return Message(text=text)
