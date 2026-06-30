"""Langflow 커스텀 컴포넌트 — 폴더 내용 DB화(인덱싱) 트리거.

사용자가 **인덱싱할 폴더 경로**를 입력하면 `smb_finder` 서비스의 `POST /refresh-content`를
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

import httpx
from lfx.custom.custom_component.component import Component
from lfx.io import IntInput, MessageTextInput, Output
from lfx.schema.message import Message


class SMBContentIndexerComponent(Component):
    """지정 폴더 아래 파일 본문을 DB화한다 (smb-finder /refresh-content 래퍼)."""

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
        IntInput(
            name="timeout_ms",
            display_name="요청 timeout(ms)",
            info="DB화는 느릴 수 있어 넉넉히 둔다. 초과 시 흐름은 유지하고 상태로만 보고한다.",
            value=600_000,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="결과 메시지", name="message", method="run_indexing"),
    ]

    async def run_indexing(self) -> Message:
        """smb-finder `POST /refresh-content`를 호출해 폴더를 DB화하고 결과를 요약한다."""
        path = (self.path or "").strip()
        url = f"{str(self.service_url).rstrip('/')}/refresh-content"
        timeout_s = max(1.0, int(self.timeout_ms) / 1000)
        # host/share만 선택 전달 — 자격증명은 서버 .env에서만 온다(여기서 보내지 않음).
        payload = {"path": path, "host": (self.host or "").strip(), "share_name": (self.share_name or "").strip()}

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                stats = resp.json()
        except httpx.TimeoutException:
            msg = f"timeout({self.timeout_ms}ms) — DB화가 길어집니다. 서비스 로그를 확인하세요."
            self.status = msg
            return Message(text=msg)
        except Exception as e:  # noqa: BLE001 — 노코드 캔버스에서 흐름이 끊기지 않게 흡수
            msg = f"DB화 호출 실패: {e}"
            self.status = msg
            return Message(text=msg)

        scope = stats.get("path") or "(공유 전체)"
        share = stats.get("share", "")
        indexed = stats.get("indexed", 0)
        over = stats.get("over_budget", False)
        parts = [
            f"[{share}] '{scope}' DB화 완료: {indexed}개 파일 적재",
            f"(미지원 {stats.get('unsupported', 0)}, 빈문서 {stats.get('empty', 0)}, "
            f"오류 {stats.get('errors', 0)}, {stats.get('elapsed_sec', '?')}s"
            + (", 시간예산초과 — 부분 적재" if over else "") + ")",
        ]
        if indexed == 0:
            parts.append("※ 0건입니다. 경로가 맞는지, .env의 SMB 자격증명이 채워졌는지 확인하세요.")
        text = " ".join(parts)
        self.status = text
        return Message(text=text)
