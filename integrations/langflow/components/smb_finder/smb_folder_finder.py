"""Langflow 커스텀 컴포넌트 — 공유 폴더 찾기(smb-finder) 래퍼.

지금까지 만든 `smb_finder` FastAPI 서비스(`POST /find`)를 Langflow 노코드 캔버스
'밖에서 감싸는' 컴포넌트다. 사용자는 코딩 없이 이 블록을 끌어다 워크플로에 연결한다.

설계 원칙 (CLAUDE.md):
- **보안**: 환자/검사 데이터는 외부로 나가면 안 된다. 이 컴포넌트는 **사내 localhost의
  smb_finder 서비스만** 호출한다(기본 http://localhost:8010). 외부 LLM·검색 API로
  폴더 경로나 내용을 보내지 않는다. service_url을 외부 호스트로 바꾸지 말 것.
- **지연**: 검색 자체는 smb_finder가 인메모리 인덱스로 처리한다(빠름). 여기서는
  timeout을 강제해 무한 대기를 막고, 초과 시 빈 결과 + 상태 메시지로 즉시 반환한다.
- **읽기 전용**: 폴더를 찾기만 한다. 쓰기/이동/삭제 없음.
"""

from __future__ import annotations

import httpx
from lfx.custom.custom_component.component import Component
from lfx.io import IntInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message


class SMBFolderFinderComponent(Component):
    """자연어 명령으로 사내 SMB 공유폴더를 찾는다 (smb-finder 서비스 래퍼)."""

    display_name = "SMB 공유폴더 찾기"
    description = "자연어 명령으로 온프레미스 SMB 공유폴더를 즉시 찾는다 (사내 smb-finder 서비스 호출)."
    documentation = "https://github.com/langflow-ai/langflow"  # 내부 문서로 교체 가능
    icon = "folder-search"
    name = "SMBFolderFinder"

    inputs = [
        MessageTextInput(
            name="query",
            display_name="질의",
            info="찾을 폴더의 자연어 명령 또는 키워드 (예: 'OO검사 결과 폴더 찾아줘').",
            tool_mode=True,  # 에이전트 도구로도 노출
            required=True,
        ),
        MessageTextInput(
            name="service_url",
            display_name="smb-finder 주소",
            info="사내 smb-finder 서비스의 base URL. 보안상 localhost 등 사내망만 사용한다.",
            value="http://localhost:8010",
            advanced=True,
        ),
        IntInput(
            name="limit",
            display_name="최대 결과 수",
            info="반환할 상위 폴더 개수.",
            value=5,
        ),
        IntInput(
            name="timeout_ms",
            display_name="요청 timeout(ms)",
            info="이 시간을 넘기면 빈 결과로 즉시 반환한다(지연 사수).",
            value=1500,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="폴더 목록", name="folders", method="find_folders"),
        Output(display_name="요약 메시지", name="message", method="as_message"),
    ]

    # ── 내부: 서비스 호출(요청당 1회, 출력 2개가 공유) ──────────────
    _cache: dict | None = None

    async def _call_service(self) -> dict:
        """smb-finder `POST /find`를 호출해 응답 dict를 돌려준다. 실패해도 예외 없이 빈 결과."""
        query = (self.query or "").strip()
        if not query:
            self.status = "질의가 비어 있습니다."
            return {"hits": [], "normalized_query": "", "elapsed_ms": 0, "error": "empty_query"}

        cache_key = (query, self.service_url, int(self.limit))
        if self._cache is not None and self._cache.get("_key") == cache_key:
            return self._cache

        url = f"{str(self.service_url).rstrip('/')}/find"
        payload = {"query": query, "limit": int(self.limit)}
        timeout_s = max(0.1, int(self.timeout_ms) / 1000)

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                result = resp.json()
        except httpx.TimeoutException:
            self.status = f"timeout({self.timeout_ms}ms) — 빈 결과 반환"
            result = {"hits": [], "normalized_query": query, "elapsed_ms": self.timeout_ms, "error": "timeout"}
        except Exception as e:  # noqa: BLE001 — 노코드 캔버스에서 흐름이 끊기지 않게 흡수
            self.status = f"smb-finder 호출 실패: {e}"
            result = {"hits": [], "normalized_query": query, "elapsed_ms": 0, "error": str(e)}
        else:
            hit_n = len(result.get("hits", []))
            over = result.get("over_budget", False)
            self.status = f"{hit_n}건 / {result.get('elapsed_ms', '?')}ms" + (" (예산초과)" if over else "")

        result["_key"] = cache_key
        self._cache = result
        return result

    # ── 출력 1: 표(DataFrame) — 시각 워크플로/표시용 ────────────────
    async def find_folders(self) -> DataFrame:
        """검색된 폴더를 표로 반환한다 (path/name/score/depth)."""
        result = await self._call_service()
        hits = result.get("hits", [])
        rows = [
            Data(data={"path": h["path"], "name": h["name"], "score": h["score"], "depth": h["depth"]})
            for h in hits
        ]
        return DataFrame(rows)

    # ── 출력 2: 메시지 — 챗봇/에이전트 도구용 요약 텍스트 ──────────
    async def as_message(self) -> Message:
        """검색 결과를 사람이 읽기 좋은 한 줄/목록 텍스트로 반환한다."""
        result = await self._call_service()
        hits = result.get("hits", [])
        if not hits:
            err = result.get("error")
            text = "관련 폴더를 찾지 못했습니다." + (f" ({err})" if err else "")
            return Message(text=text)
        lines = [f"'{result.get('normalized_query', '')}' 검색 결과 ({len(hits)}건):"]
        lines += [f"{i + 1}. {h['name']}  —  {h['path']}" for i, h in enumerate(hits)]
        return Message(text="\n".join(lines))
