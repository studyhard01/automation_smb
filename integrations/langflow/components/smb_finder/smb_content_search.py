"""Langflow 커스텀 컴포넌트 — 파일 내용 검색(smb-finder) 래퍼.

`smb_finder` FastAPI 서비스의 `POST /search-content`를 Langflow 노코드 캔버스에서
끌어다 쓰게 감싸는 컴포넌트다. (폴더 이름 검색은 별도 컴포넌트 'SMB 공유폴더 찾기' → /find)

설계 원칙 (CLAUDE.md):
- **보안**: 환자/검사 **본문**이 외부로 나가면 안 된다. 이 컴포넌트는 **사내 localhost의
  smb_finder 서비스만** 호출한다(기본 http://localhost:8010). service_url을 외부 호스트로
  바꾸지 말 것. 같은 캔버스에 외부 LLM 노드(OpenAI 등)를 두지 말 것(스니펫이 새어나갈 수 있다).
- **지연**: 검색은 smb_finder가 FTS5 인덱스로 처리한다(빠름). 여기서는 timeout을 강제해
  무한 대기를 막고, 초과 시 빈 결과 + 상태 메시지로 즉시 반환한다.
- **읽기 전용**: 파일 내용을 찾기만 한다. 쓰기/이동/삭제 없음.
"""

from __future__ import annotations

import httpx
from lfx.custom.custom_component.component import Component
from lfx.io import IntInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame
from lfx.schema.message import Message


class SMBContentSearchComponent(Component):
    """파일 본문 키워드/내용으로 사내 SMB 파일을 찾는다 (smb-finder 서비스 래퍼)."""

    display_name = "SMB 파일 내용 검색"
    description = "파일 본문에 들어있는 키워드/내용으로 온프레미스 SMB 파일을 찾는다 (사내 smb-finder 호출)."
    documentation = "https://github.com/langflow-ai/langflow"  # 내부 문서로 교체 가능
    icon = "file-search"
    name = "SMBContentSearch"

    inputs = [
        MessageTextInput(
            name="query",
            display_name="질의",
            info="파일 본문에서 찾을 키워드/내용 (예: 'BRCA1 변이 보고서', '음성 판정 결과').",
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
            info="반환할 상위 파일 개수.",
            value=10,
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
        Output(display_name="파일 목록", name="files", method="search_content"),
        Output(display_name="요약 메시지", name="message", method="as_message"),
    ]

    # ── 내부: 서비스 호출(요청당 1회, 출력 2개가 공유) ──────────────
    _cache: dict | None = None

    async def _call_service(self) -> dict:
        """smb-finder `POST /search-content`를 호출해 응답 dict를 돌려준다. 실패해도 예외 없이 빈 결과."""
        query = (self.query or "").strip()
        if not query:
            self.status = "질의가 비어 있습니다."
            return {"hits": [], "terms": [], "elapsed_ms": 0, "indexed_files": 0, "error": "empty_query"}

        cache_key = (query, self.service_url, int(self.limit))
        if self._cache is not None and self._cache.get("_key") == cache_key:
            return self._cache

        url = f"{str(self.service_url).rstrip('/')}/search-content"
        payload = {"query": query, "limit": int(self.limit)}
        timeout_s = max(0.1, int(self.timeout_ms) / 1000)

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                result = resp.json()
        except httpx.TimeoutException:
            self.status = f"timeout({self.timeout_ms}ms) — 빈 결과 반환"
            result = {"hits": [], "terms": [], "elapsed_ms": self.timeout_ms, "indexed_files": 0, "error": "timeout"}
        except Exception as e:  # noqa: BLE001 — 노코드 캔버스에서 흐름이 끊기지 않게 흡수
            self.status = f"smb-finder 호출 실패: {e}"
            result = {"hits": [], "terms": [], "elapsed_ms": 0, "indexed_files": 0, "error": str(e)}
        else:
            hit_n = len(result.get("hits", []))
            over = result.get("over_budget", False)
            indexed = result.get("indexed_files", 0)
            if indexed == 0 and "error" not in result:
                self.status = "내용 인덱스가 비어 있음 — SMB 폴더 내용 DB화 컴포넌트를 먼저 실행"
            else:
                self.status = (
                    f"{hit_n}건 / {result.get('elapsed_ms', '?')}ms (인덱스 {indexed}파일)"
                    + (" (예산초과)" if over else "")
                )

        result["_key"] = cache_key
        self._cache = result
        return result

    # ── 출력 1: 표(DataFrame) — 시각 워크플로/표시용 ────────────────
    async def search_content(self) -> DataFrame:
        """검색된 파일을 표로 반환한다 (path/name/ext/size/score/snippet)."""
        result = await self._call_service()
        hits = result.get("hits", [])
        rows = [
            Data(data={
                "path": h["path"],
                "name": h["name"],
                "ext": h.get("ext", ""),
                "size": h.get("size", 0),
                "score": h.get("score", 0),
                "snippet": h.get("snippet", ""),
            })
            for h in hits
        ]
        return DataFrame(rows)

    # ── 출력 2: 메시지 — 챗봇/에이전트 도구용 요약 텍스트 ──────────
    async def as_message(self) -> Message:
        """검색 결과를 사람이 읽기 좋은 목록 텍스트(파일명 — 경로 + 스니펫)로 반환한다."""
        result = await self._call_service()
        hits = result.get("hits", [])
        if not hits:
            err = result.get("error")
            text = "본문이 일치하는 파일을 찾지 못했습니다." + (f" ({err})" if err else "")
            return Message(text=text)
        terms = " ".join(result.get("terms", []))
        lines = [f"'{terms}' 내용 검색 결과 ({len(hits)}건):"]
        for i, h in enumerate(hits):
            snip = (h.get("snippet") or "").replace("\n", " ").strip()
            line = f"{i + 1}. {h['name']}  —  {h['path']}"
            if snip:
                line += f"\n    …{snip}…"
            lines.append(line)
        return Message(text="\n".join(lines))
