"""Playground이 제공하는 기본 도구 ID 계약."""

from __future__ import annotations

from typing import Literal

KnownToolId = Literal[
    "find_folder",
    "search_content",
    "search_rag_chunks",
    "cytogenetics_karyotype_summary",
    "cytogenetics_report",
    "ngs_report",
    "refresh_content",
]

KNOWN_TOOL_IDS: tuple[KnownToolId, ...] = (
    "find_folder",
    "search_content",
    "search_rag_chunks",
    "cytogenetics_karyotype_summary",
    "cytogenetics_report",
    "ngs_report",
    "refresh_content",
)
