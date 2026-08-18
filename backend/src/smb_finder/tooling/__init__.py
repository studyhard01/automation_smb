"""MCP 문서 metadata 도구의 공개 계약."""

from .catalog import GET_DOCUMENT_METADATA_TOOL_DESCRIPTION, GET_DOCUMENT_METADATA_TOOL_NAME
from .contracts import DocumentMetadataOutput, GetDocumentMetadataInput
from .errors import ToolExecutionError
from .executor import MetadataReader, ToolExecutor

__all__ = [
    "GET_DOCUMENT_METADATA_TOOL_DESCRIPTION",
    "GET_DOCUMENT_METADATA_TOOL_NAME",
    "DocumentMetadataOutput",
    "GetDocumentMetadataInput",
    "MetadataReader",
    "ToolExecutionError",
    "ToolExecutor",
]
