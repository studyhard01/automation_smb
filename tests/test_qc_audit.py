"""PDF/Markdown 첨부와 합성 SOP QC 감사 수직 슬라이스 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from smb_finder.config import Settings
from smb_finder.playground.api import create_playground_router
from smb_finder.playground.tools import PlaygroundRuntime, build_tool_registry
from smb_finder.qc_audit import AttachmentStore, AttachmentStoreError, QcAuditService


def _rules_path() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "qc_audit" / "synthetic_sop_rules.json"


def _markdown(*, temperature: str = "21", include_status: bool = True) -> bytes:
    status_line = "| SYN-QC-STATUS | Orion self-check | PASS |\n" if include_status else ""
    return (
        "# Synthetic QC Report\n"
        f"| SYN-QC-TEMP | Aurora chamber temperature | {temperature} | C |\n"
        "| SYN-QC-YIELD | Nova recovery rate | 95 | % |\n"
        f"{status_line}"
    ).encode()


def _text_pdf() -> bytes:
    """추가 생성 의존성 없이 pypdf가 읽을 수 있는 합성 1페이지 PDF를 만든다."""

    lines = [
        "Synthetic QC Report",
        "SYN-QC-TEMP | Aurora chamber temperature | 21 | C",
        "SYN-QC-YIELD | Nova recovery rate | 95 | %",
        "SYN-QC-STATUS | Orion self-check | PASS",
    ]
    commands = [f"BT /F1 12 Tf 72 {760 - index * 28} Td ({line}) Tj ET" for index, line in enumerate(lines)]
    stream = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> "
        b"/Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(pdf)


def _service(tmp_path: Path) -> tuple[AttachmentStore, QcAuditService]:
    store = AttachmentStore(tmp_path, max_bytes=1024 * 1024, max_text_chars=100_000)
    return store, QcAuditService(store, _rules_path(), budget_ms=1000)


def test_markdown_qc_audit_returns_pass_and_evidence(tmp_path: Path):
    store, service = _service(tmp_path)
    attachment = store.save("synthetic-qc.md", _markdown(), "text/markdown")

    result = service.audit(attachment.id)

    assert result.status == "PASS"
    assert result.counts == {"PASS": 3, "WARNING": 0, "FAIL": 0, "UNVERIFIABLE": 0}
    assert {finding.evidence for finding in result.findings} == {
        "SYN-SOP-001 §2.1",
        "SYN-SOP-001 §2.2",
        "SYN-SOP-001 §2.3",
    }
    assert result.elapsed_ms < 1000


def test_qc_audit_distinguishes_fail_and_missing_required_item(tmp_path: Path):
    store, service = _service(tmp_path)
    attachment = store.save("synthetic-qc.md", _markdown(temperature="26.5", include_status=False))

    result = service.audit(attachment.id)

    assert result.status == "FAIL"
    findings = {finding.rule_id: finding for finding in result.findings}
    assert findings["SYN-QC-TEMP"].status == "FAIL"
    assert findings["SYN-QC-STATUS"].status == "UNVERIFIABLE"


def test_qc_audit_marks_in_range_boundary_as_warning(tmp_path: Path):
    store, service = _service(tmp_path)
    attachment = store.save("synthetic-qc.md", _markdown(temperature="23.6"))

    result = service.audit(attachment.id)

    assert result.status == "WARNING"
    assert next(item for item in result.findings if item.rule_id == "SYN-QC-TEMP").status == "WARNING"


def test_sop_search_is_local_and_returns_structured_rule(tmp_path: Path):
    _store, service = _service(tmp_path)

    hits, elapsed_ms = service.search_sop("오로라 온도 허용 기준", limit=2)

    assert hits[0].rule.id == "SYN-QC-TEMP"
    assert hits[0].rule.criterion == "18.0–24.0 °C"
    assert elapsed_ms < 100


def test_attachment_store_rejects_unsupported_and_disguised_pdf(tmp_path: Path):
    store, _service_instance = _service(tmp_path)

    with pytest.raises(AttachmentStoreError, match="PDF 또는 Markdown"):
        store.save("report.txt", b"text")
    with pytest.raises(AttachmentStoreError, match="PDF 시그니처"):
        store.save("report.pdf", b"not-a-pdf", "application/pdf")


def test_pdf_attachment_uses_existing_local_extractor(tmp_path: Path):
    store, service = _service(tmp_path)
    attachment = store.save("synthetic-qc.pdf", _text_pdf(), "application/pdf")

    result = service.audit(attachment.id)

    assert result.status == "PASS"
    assert result.attachment.extension == ".pdf"


def test_registry_exposes_high_level_audit_and_support_tools(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        playground_upload_dir=str(tmp_path),
        playground_qc_sop_path=str(_rules_path()),
    )
    registry = build_tool_registry(PlaygroundRuntime(settings=settings))

    assert registry["audit_qc_report"].returns_final_answer is True
    assert registry["audit_qc_report"].definition.default_selected is True
    assert registry["draft_qc_report"].returns_final_answer is True
    assert registry["draft_qc_report"].definition.execution_type == "llm"
    assert registry["draft_qc_report"].definition.default_selected is True
    assert registry["extract_uploaded_document"].definition.category == "report"
    assert registry["search_sop_knowledge"].definition.category == "database"


def test_upload_then_chat_audits_without_llm(tmp_path: Path):
    async def scenario() -> None:
        settings = Settings(
            _env_file=None,
            llm_model="",
            playground_upload_dir=str(tmp_path),
            playground_qc_sop_path=str(_rules_path()),
        )
        app = FastAPI()
        app.include_router(create_playground_router(lambda: PlaygroundRuntime(settings=settings)))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            upload = await client.post(
                "/api/playground/attachments",
                content=_markdown(),
                headers={"Content-Type": "text/markdown", "X-Playground-Filename": "synthetic-qc.md"},
            )
            assert upload.status_code == 201
            attachment_id = upload.json()["id"]

            chat = await client.post(
                "/api/playground/chat",
                json={
                    "message": "첨부 보고서를 감사해줘",
                    "selected_tool_ids": ["audit_qc_report"],
                    "attachment_ids": [attachment_id],
                    "provider": "local",
                    "model": "",
                },
            )

            assert chat.status_code == 200
            body = chat.json()
            assert body["provider_used"] == "local-code"
            assert body["assistant_message"].startswith("QC 감사 결과: PASS")
            assert body["tool_calls"][0]["tool_id"] == "audit_qc_report"
            assert "llm_bypassed_local_qc_audit" in body["warnings"]

    asyncio.run(scenario())
