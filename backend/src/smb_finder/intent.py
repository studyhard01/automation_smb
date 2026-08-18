"""파일 찾기 자연어에서 검색에 필요한 핵심 토큰만 남긴다."""

from __future__ import annotations

import re

_FILLER = {
    "파일",
    "폴더",
    "디렉터리",
    "찾아줘",
    "찾아",
    "찾기",
    "검색",
    "해줘",
    "알려줘",
    "보여줘",
    "어디",
    "있는",
    "관련",
    "관련된",
}
_TOKEN = re.compile(r"[0-9A-Za-z가-힣_+.-]+")


def normalize_rule(query: str) -> str:
    """LLM 호출 없이 자연어 명령의 군더더기 토큰을 제거한다."""

    tokens = _TOKEN.findall(query)
    kept = [token for token in tokens if token.casefold() not in _FILLER]
    return " ".join(kept) if kept else query.strip()
