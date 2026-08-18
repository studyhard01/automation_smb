"""LLM 호출 없이 Bot Core 경로를 결정하는 규칙."""

from __future__ import annotations

from .contracts import BotRoute, SelectedDocumentId

_METADATA_MARKERS = (
    "metadata",
    "메타데이터",
    "문서 정보",
    "파일 정보",
    "revision status",
    "리비전 상태",
    "버전 상태",
)


def route_request(message: str, selected_ids: tuple[SelectedDocumentId, ...]) -> BotRoute:
    """명시적 metadata 의도를 우선하고 선택 문서 질문을 fast path로 보낸다."""

    normalized = " ".join(message.casefold().split())
    if normalized and any(marker in normalized for marker in _METADATA_MARKERS):
        return "metadata"
    if normalized and selected_ids:
        return "document_qa"
    return "unsupported"
