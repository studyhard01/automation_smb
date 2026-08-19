"""Playground 설정·SMB 파일 첨부의 계약과 쓰기 경계를 검증한다."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from xml.etree import ElementTree

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from smbprotocol.exceptions import NtStatus, SMBOSError

from smb_finder.model_gateway import ModelGatewayError, ModelGatewayResult, ModelTokenUsage
from smb_finder.config import Settings
from smb_finder.models import DocumentCitation, RetrievalScores
from smb_finder.playground.document_api import DocumentRuntime
from smb_finder.playground.proposal_draft import (
    LocalProposalDraftGenerator,
    ProposalClaimVerdict,
    ProposalDraftError,
    ProposalDraftFields,
    ProposalDraftResult,
    ProposalDraftService,
    ProposalEvidenceVerification,
    ProposalQuestionCandidate,
)
from smb_finder.playground.upload_api import create_upload_router
from smb_finder.playground.upload_models import FileUploadResponse
from smb_finder.playground.upload_service import (
    RuntimeUploadSettingsStore,
    SmbUploadWriter,
    UploadError,
    UploadManager,
    build_upload_filename,
    normalize_relative_directory,
    sanitize_filename,
)


def _settings(tmp_path: Path, **overrides) -> Settings:  # noqa: ANN003
    values = {
        "_env_file": None,
        "smb_upload_enabled": True,
        "smb_host": "synthetic-server",
        "smb_share_name": "synthetic-share",
        "smb_username": "synthetic-user",
        "smb_password": "synthetic-password",
        "smb_upload_runtime_settings_path": str(tmp_path / "settings.json"),
        "smb_upload_registry_path": str(tmp_path / "upload-registry.json"),
        "smb_upload_default_relative_directory": "team/inbox",
        "smb_upload_allowed_extensions": ".pdf,.txt",
        "smb_upload_max_size_bytes": 32,
        "smb_upload_timeout_ms": 1000,
        "ollama_base_url": "http://local-llm.invalid",
        "llmops_chat_model": "synthetic-model",
    }
    values.update(overrides)
    return Settings(**values)


class StubWriter:
    allowed_extensions = (".pdf", ".txt")

    def __init__(self) -> None:
        self.closed = False
        self.received = b""
        self.proposal_content = b""
        self.proposal_file_name = ""
        self.proposal_writes: list[tuple[str, bytes]] = []
        self.proposal_deadlines: list[float | None] = []
        self.read_deadlines: list[float | None] = []

    def close(self) -> None:
        self.closed = True

    def upload(self, source, original_name: str, relative_directory: str) -> FileUploadResponse:  # noqa: ANN001
        self.received = source.read()
        assert relative_directory == "team/inbox"
        uploaded_at = datetime(2026, 1, 1, tzinfo=UTC)
        return FileUploadResponse(
            file_name=build_upload_filename(original_name, uploaded_at),
            size_bytes=len(self.received),
            uploaded_at=uploaded_at,
            destination_label="관리 공유폴더",
            indexed=False,
        )

    def read(
        self,
        file_name: str,
        relative_directory: str,
        *,
        expected_size: int,
        deadline: float | None = None,
    ) -> bytes:
        self.read_deadlines.append(deadline)
        assert file_name
        assert relative_directory == "team/inbox"
        assert expected_size == len(self.received)
        return self.received

    def create_xlsx(
        self,
        content: bytes,
        file_name: str,
        relative_directory: str,
        *,
        deadline: float | None = None,
    ) -> FileUploadResponse:
        assert relative_directory == "team/proposal"
        self.proposal_deadlines.append(deadline)
        self.proposal_content = content
        self.proposal_file_name = file_name
        self.proposal_writes.append((file_name, content))
        return FileUploadResponse(
            file_name=file_name,
            size_bytes=len(content),
            uploaded_at=datetime(2026, 7, 29, 15, 30, tzinfo=UTC),
            destination_label="기안 문서 폴더",
            indexed=False,
        )


class StubDraftGenerator:
    def __init__(self, fields: ProposalDraftFields) -> None:
        self.fields = fields
        self.instructions: list[str] = []
        self.evidence: list[list[DocumentCitation]] = []
        self.proposal_types: list[str] = []
        self.revisions: list[tuple[str, str, str]] = []
        self.deadlines: list[float | None] = []
        self.closed = False

    def generate(
        self,
        instruction: str,
        evidence: list[DocumentCitation],
        *,
        proposal_type: str = "general",
        deadline: float | None = None,
    ) -> ProposalDraftResult:
        self.deadlines.append(deadline)
        self.instructions.append(instruction)
        self.evidence.append(evidence)
        self.proposal_types.append(proposal_type)
        return ProposalDraftResult(fields=self.fields, model="synthetic-draft-model")

    def revise(
        self,
        feedback: str,
        document,  # noqa: ANN001
        evidence: list[DocumentCitation],
        *,
        proposal_type: str,
        deadline: float | None = None,
    ) -> ProposalDraftResult:
        self.deadlines.append(deadline)
        self.revisions.append((feedback, document.title, proposal_type))
        self.evidence.append(evidence)
        revised_fields = self.fields.model_copy(update={"body": f"{self.fields.body}\n수정 사항: {feedback}"})
        return ProposalDraftResult(fields=revised_fields, model="synthetic-draft-model")

    def close(self) -> None:
        self.closed = True


class AssessingStubDraftGenerator(StubDraftGenerator):
    """첫 생성은 질문하고 답변 반영본은 근거 있음으로 판정하는 합성 double."""

    def __init__(self, fields: ProposalDraftFields) -> None:
        super().__init__(fields)
        self.assessment_count = 0

    def assess(self, instruction, document, evidence, **kwargs):  # noqa: ANN001, ANN003
        self.assessment_count += 1
        if kwargs.get("allow_questions", True):
            return ProposalEvidenceVerification(
                schema_version="proposal-evidence-verification-v1",
                claims=[
                    ProposalClaimVerdict(
                        claim_id="C0001",
                        status="unsupported",
                        action="ask",
                        citations=[],
                    )
                ],
                questions=[
                    ProposalQuestionCandidate(
                        field_key="purchase_amount",
                        prompt="구매 승인 금액은 얼마인가요?",
                        reason="missing",
                        priority=1,
                    )
                ],
            )
        return ProposalEvidenceVerification(
            schema_version="proposal-evidence-verification-v1",
            claims=[
                ProposalClaimVerdict(
                    claim_id="C0001",
                    status="supported",
                    action="keep",
                    citations=["U002"],
                )
            ],
            questions=[],
        )


def _citation() -> DocumentCitation:
    return DocumentCitation(
        index=1,
        doc_id=uuid4(),
        revision_id=uuid4(),
        chunk_id=uuid4(),
        title="합성 구매 근거",
        excerpt="합성 장비 구매가 필요합니다.",
        scores=RetrievalScores(rrf=0.9),
    )


class StubFileSearcher:
    def __init__(self) -> None:
        self.deadlines: list[float | None] = []

    def validate_active_selections(self, selections, *, deadline=None):  # noqa: ANN001, ANN202
        self.deadlines.append(deadline)
        return set(selections)


class StubRetriever:
    def __init__(self, citations: list[DocumentCitation] | None = None) -> None:
        self.citations = citations if citations is not None else [_citation()]
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.deadlines: list[float | None] = []

    def retrieve(
        self,
        instruction: str,
        selections: list[tuple[str, str]],
        *,
        deadline: float | None = None,
    ):
        self.calls.append((instruction, selections))
        self.deadlines.append(deadline)
        return SimpleNamespace(citations=self.citations)


class StubUploadContextManager:
    def __init__(self, citations: list[DocumentCitation] | None = None) -> None:
        self.citations = citations if citations is not None else [_citation()]
        self.validation_deadlines: list[float | None] = []
        self.retrieval_deadlines: list[float | None] = []

    def validate_selections(self, _selections, *, deadline=None):  # noqa: ANN001, ANN202
        self.validation_deadlines.append(deadline)
        return True

    def retrieve(self, _query, _selections, *, deadline=None):  # noqa: ANN001, ANN202
        self.retrieval_deadlines.append(deadline)
        return SimpleNamespace(citations=self.citations)


class CountingRegistry:
    def __init__(self) -> None:
        self.lookups: list[tuple[UUID, UUID]] = []

    def find(self, doc_id: UUID, revision_id: UUID):  # noqa: ANN201
        self.lookups.append((doc_id, revision_id))
        return None


class StubModelGateway:
    def __init__(self, response_payload: dict) -> None:
        self.response_payload = response_payload
        self.calls: list[dict] = []

    def invoke_json(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        payload = self.response_payload
        if "message" in payload:
            payload = json.loads(payload["message"]["content"])
        try:
            validated = kwargs["response_model"].model_validate(payload)
        except ValidationError as exc:
            raise ModelGatewayError("output_schema_invalid", retryable=False) from exc
        return ModelGatewayResult(
            payload=validated,
            provider="ollama",
            model=kwargs["model"],
            usage=ModelTokenUsage(),
            elapsed_ms=1.0,
        )

    def close(self) -> None:
        return None


def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    files: dict | None = None,
) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json_body, files=files)

    return asyncio.run(send())


def test_settings_api_returns_only_public_contract_and_persists_relative_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = RuntimeUploadSettingsStore(Path(settings.smb_upload_runtime_settings_path), "team/inbox")
    manager = UploadManager(settings, store=store, writer=StubWriter())
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager))

    response = _request(app, "GET", "/api/playground/settings")
    assert response.status_code == 200
    assert response.json() == {
        "upload": {
            "enabled": True,
            "configured": True,
            "relative_directory": "team/inbox",
            "destination_label": "관리 공유폴더",
            "max_size_bytes": 32,
            "allowed_extensions": [".pdf", ".txt"],
        },
        "proposal_draft": {
            "relative_directory": "",
            "destination_label": "기안 문서 폴더",
        },
        "local_llm_configured": True,
    }
    serialized = response.text
    assert "synthetic-server" not in serialized
    assert "synthetic-user" not in serialized
    assert "synthetic-password" not in serialized

    patched = _request(
        app,
        "PATCH",
        "/api/playground/settings/upload",
        json_body={"relative_directory": "team/review"},
    )
    assert patched.status_code == 200
    assert patched.json()["upload"]["relative_directory"] == "team/review"
    assert json.loads(Path(settings.smb_upload_runtime_settings_path).read_text(encoding="utf-8")) == {
        "upload": {"relative_directory": "team/review"}
    }


def test_proposal_draft_settings_patch_preserves_upload_and_unknown_runtime_settings(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime_path = Path(settings.smb_upload_runtime_settings_path)
    runtime_path.write_text(
        json.dumps(
            {
                "upload": {"relative_directory": "team/inbox", "future_upload_key": True},
                "future_section": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    manager = UploadManager(settings, writer=StubWriter())
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager))

    response = _request(
        app,
        "PATCH",
        "/api/playground/settings/proposal-draft",
        json_body={"relative_directory": r"team\proposal"},
    )

    assert response.status_code == 200
    assert response.json()["proposal_draft"]["relative_directory"] == "team/proposal"
    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert payload == {
        "upload": {"relative_directory": "team/inbox", "future_upload_key": True},
        "future_section": {"enabled": True},
        "proposal_draft": {"relative_directory": "team/proposal"},
    }

    upload_response = _request(
        app,
        "PATCH",
        "/api/playground/settings/upload",
        json_body={"relative_directory": "team/review"},
    )
    assert upload_response.status_code == 200
    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert payload["upload"] == {"relative_directory": "team/review", "future_upload_key": True}
    assert payload["proposal_draft"] == {"relative_directory": "team/proposal"}
    assert payload["future_section"] == {"enabled": True}


def test_proposal_draft_settings_rejects_absolute_or_parent_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = FastAPI()
    app.include_router(create_upload_router(settings, UploadManager(settings, writer=StubWriter())))

    for value in (r"\\synthetic-server\synthetic-share", r"C:\synthetic", "../synthetic"):
        response = _request(
            app,
            "PATCH",
            "/api/playground/settings/proposal-draft",
            json_body={"relative_directory": value},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_relative_directory"


def test_local_proposal_generator_accepts_legacy_three_field_json_and_includes_evidence(tmp_path: Path) -> None:
    gateway = StubModelGateway(
        {
            "message": {
                "content": json.dumps(
                    {
                        "title": "합성 구매 기안",
                        "approval_request": "검토 후 재가하여 주시기 바랍니다.",
                        "body": "1. 합성 목적\n2. 합성 범위",
                    },
                    ensure_ascii=False,
                )
            }
        }
    )
    generator = LocalProposalDraftGenerator(
        _settings(tmp_path, ollama_base_url="http://127.0.0.1:11434"),
        gateway=gateway,
    )
    citation = _citation()

    result = generator.generate("합성 구매 기안을 작성해 줘", [citation])

    assert result.fields.title == "합성 구매 기안"
    call = gateway.calls[0]
    assert call["purpose"] == "proposal_generation"
    assert call["model"] == "synthetic-model"
    assert "proposal-document-v2" in call["messages"][0]["content"]
    assert "빈 배열 []" in call["messages"][0]["content"]
    assert citation.title in call["messages"][1]["content"]
    assert citation.excerpt in call["messages"][1]["content"]


def test_local_proposal_generator_maps_missing_json_field_to_502(tmp_path: Path) -> None:
    gateway = StubModelGateway({"message": {"content": '{"title":"합성","approval_request":"검토 바랍니다."}'}})
    generator = LocalProposalDraftGenerator(
        _settings(tmp_path, ollama_base_url="http://127.0.0.1:11434"),
        gateway=gateway,
    )

    with pytest.raises(ProposalDraftError) as raised:
        generator.generate("합성 기안을 작성해 줘", [_citation()])

    assert raised.value.code == "proposal_llm_response_invalid"
    assert raised.value.status_code == 502


def test_local_proposal_generator_rejects_extra_json_field(tmp_path: Path) -> None:
    gateway = StubModelGateway(
        {
            "message": {
                "content": (
                    '{"title":"합성","approval_request":"검토 바랍니다.","body":"합성 본문","unexpected":"거부 대상"}'
                )
            }
        }
    )
    generator = LocalProposalDraftGenerator(
        _settings(tmp_path, ollama_base_url="http://127.0.0.1:11434"),
        gateway=gateway,
    )

    with pytest.raises(ProposalDraftError) as raised:
        generator.generate("합성 기안을 작성해 줘", [_citation()])

    assert raised.value.code == "proposal_llm_response_invalid"
    assert raised.value.status_code == 502


def test_proposal_draft_generation_calls_llm_saves_new_xlsx_and_returns_download(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    fields = ProposalDraftFields(
        title="합성 자동화 구매 계획",
        approval_request="관련 내용을 검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 자동화 장비를 구매합니다.",
    )
    draft_generator = StubDraftGenerator(fields)
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "playground" / "templates" / "proposal_draft.xlsx"
    )
    service = ProposalDraftService(
        settings,
        manager,
        template_path=template_path,
        clock=lambda: datetime(2026, 7, 29, 15, 30, tzinfo=UTC),
        draft_generator=draft_generator,
    )
    retriever = StubRetriever()
    file_searcher = StubFileSearcher()
    runtime = DocumentRuntime(
        settings=settings,
        file_searcher=file_searcher,
        scoped_retriever=retriever,
        upload_manager=manager,
    )
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager, service, runtime_getter=lambda: runtime))

    selected = retriever.citations[0]

    generated = _request(
        app,
        "POST",
        "/api/playground/drafts/proposal",
        json_body={
            "instruction": "합성 구매 계획 기안을 작성해 줘",
            "selected_files": [
                {
                    "source": "llmops",
                    "doc_id": str(selected.doc_id),
                    "revision_id": str(selected.revision_id),
                    "file_name": "synthetic.xlsx",
                    "title": "합성 구매 근거",
                }
            ],
        },
    )

    assert generated.status_code == 201
    payload = generated.json()
    assert draft_generator.instructions == ["합성 구매 계획 기안을 작성해 줘"]
    assert draft_generator.evidence == [retriever.citations]
    assert draft_generator.proposal_types == ["purchase"]
    assert retriever.deadlines[0] is not None
    assert file_searcher.deadlines == retriever.deadlines
    assert draft_generator.deadlines == retriever.deadlines
    assert writer.proposal_deadlines == retriever.deadlines
    assert payload["title"] == "합성 자동화 구매 계획"
    assert payload["fields"] == fields.model_dump()
    assert payload["document"]["schema_version"] == "proposal-document-v2"
    assert payload["document"]["proposal_type"] == "purchase"
    assert payload["document"]["sections"][0]["semantic_role"] == "details"
    assert payload["proposal_type"] == "purchase"
    assert payload["proposal_type_source"] == "rule"
    assert payload["evidence_filter"] == {
        "schema_version": "proposal-evidence-filter-v1",
        "input_document_count": 1,
        "included_document_count": 1,
        "excluded_document_count": 0,
        "input_citation_count": 1,
        "included_citation_count": 1,
        "excluded_citation_count": 0,
        "excluded_reason_counts": {},
        "fallback_used": False,
    }
    assert payload["context_usage"]["schema_version"] == "proposal-context-usage-v1"
    assert payload["model_used"] == "synthetic-draft-model"
    assert payload["saved_to_smb"] is True
    assert payload["destination_label"] == "기안 문서 폴더"
    assert payload["file_name"] == "[기안] 합성 자동화 구매 계획_20260730_v1.0.xlsx"
    assert payload["download_url"].endswith(payload["draft_id"])
    assert writer.proposal_file_name == payload["file_name"]

    download = _request(app, "GET", payload["download_url"])
    assert download.status_code == 200
    assert download.content == writer.proposal_content
    with zipfile.ZipFile(io.BytesIO(download.content)) as workbook:
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    assert sheet.findtext(".//main:c[@r='C8']/main:is/main:t", namespaces=namespace) == "합성 자동화 구매 계획"
    assert (
        sheet.findtext(".//main:c[@r='A10']/main:is/main:t", namespaces=namespace)
        == "관련 내용을 검토 후 재가하여 주시기 바랍니다."
    )
    assert sheet.findtext(".//main:c[@r='A15']/main:is/main:t", namespaces=namespace) == "1. 주요 내용"
    assert sheet.findtext(".//main:c[@r='A16']/main:is/main:t", namespaces=namespace) == "1. 목적"
    assert (
        sheet.findtext(".//main:c[@r='A17']/main:is/main:t", namespaces=namespace) == "합성 자동화 장비를 구매합니다."
    )
    assert set(payload["timings_ms"]) == {
        "validation",
        "retrieval",
        "evidence_filter",
        "llm",
        "verification",
        "workbook",
        "smb",
    }


def test_proposal_upload_context_shares_validation_retrieval_model_and_write_deadline(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    generator = StubDraftGenerator(
        ProposalDraftFields(
            title="합성 구매 계획",
            approval_request="합성 구매를 승인하여 주시기 바랍니다.",
            body="합성 장비 구매 근거를 반영합니다.",
        )
    )
    service = ProposalDraftService(settings, manager, draft_generator=generator)
    upload_context = StubUploadContextManager()
    runtime = DocumentRuntime(settings=settings, upload_manager=upload_context)
    citation = upload_context.citations[0]
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager, service, runtime_getter=lambda: runtime))

    response = _request(
        app,
        "POST",
        "/api/playground/drafts/proposal",
        json_body={
            "instruction": "합성 구매 기안을 작성해 줘",
            "selected_files": [
                {
                    "source": "upload",
                    "doc_id": str(citation.doc_id),
                    "revision_id": str(citation.revision_id),
                    "file_name": "synthetic.txt",
                    "title": "합성 구매 근거",
                }
            ],
        },
    )

    assert response.status_code == 201
    assert upload_context.validation_deadlines[0] is not None
    assert upload_context.validation_deadlines == upload_context.retrieval_deadlines
    assert generator.deadlines == upload_context.retrieval_deadlines
    assert writer.proposal_deadlines == upload_context.retrieval_deadlines


def test_expired_upload_selection_validation_stops_before_registry_and_retrieval(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    writer = StubWriter()
    registry = CountingRegistry()
    manager = UploadManager(settings, writer=writer, registry=registry, clock=lambda: float("inf"))
    generator = StubDraftGenerator(
        ProposalDraftFields(title="합성 계획", approval_request="합성 승인 요청", body="합성 본문")
    )
    service = ProposalDraftService(settings, manager, draft_generator=generator)
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager, service, runtime_getter=lambda: runtime))
    runtime = DocumentRuntime(settings=settings, upload_manager=manager)

    response = _request(
        app,
        "POST",
        "/api/playground/drafts/proposal",
        json_body={
            "instruction": "합성 계획 기안을 작성해 줘",
            "selected_files": [
                {
                    "source": "upload",
                    "doc_id": str(uuid4()),
                    "revision_id": str(uuid4()),
                    "file_name": "synthetic.txt",
                    "title": "합성 근거",
                }
            ],
        },
    )

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "uploaded_retrieval_budget_exhausted"
    assert registry.lookups == []
    assert writer.read_deadlines == []
    assert generator.deadlines == []


def test_proposal_generation_defers_save_until_required_clarification_is_answered(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    generator = AssessingStubDraftGenerator(
        ProposalDraftFields(
            title="합성 장비 구매",
            approval_request="검토 후 재가하여 주시기 바랍니다.",
            body="합성 장비를 구매하며 승인 금액은 확인이 필요합니다.",
        )
    )
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "playground" / "templates" / "proposal_draft.xlsx"
    )
    service = ProposalDraftService(settings, manager, template_path=template_path, draft_generator=generator)
    retriever = StubRetriever()
    runtime = DocumentRuntime(
        settings=settings,
        file_searcher=StubFileSearcher(),
        scoped_retriever=retriever,
        upload_manager=manager,
    )
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager, service, runtime_getter=lambda: runtime))
    selected = retriever.citations[0]
    selected_files = [
        {
            "source": "llmops",
            "doc_id": str(selected.doc_id),
            "revision_id": str(selected.revision_id),
            "file_name": "synthetic.xlsx",
            "title": "합성 구매 근거",
        }
    ]

    generated = _request(
        app,
        "POST",
        "/api/playground/drafts/proposal",
        json_body={"instruction": "합성 장비 구매 기안을 작성해 줘", "selected_files": selected_files},
    )

    assert generated.status_code == 201
    pending = generated.json()
    assert pending["completion"]["status"] == "needs_clarification"
    assert pending["completion"]["questions"] == [
        {
            "question_id": "Q001",
            "field_key": "purchase_amount",
            "prompt": "구매 승인 금액은 얼마인가요?",
            "reason": "missing",
        }
    ]
    assert pending["saved_to_smb"] is False
    assert pending["file_name"] is None
    assert pending["download_url"] is None
    assert writer.proposal_writes == []
    blocked_download = _request(app, "GET", f"/api/playground/drafts/proposal/{pending['draft_id']}")
    assert blocked_download.status_code == 409
    assert blocked_download.json()["detail"]["code"] == "proposal_clarification_required"

    clarified = _request(
        app,
        "POST",
        f"/api/playground/drafts/proposal/{pending['draft_id']}/clarifications",
        json_body={
            "answers": [{"question_id": "Q001", "answer": "승인 금액은 120만원입니다."}],
            "selected_files": selected_files,
        },
    )

    assert clarified.status_code == 201
    completed = clarified.json()
    assert completed["clarification_of_draft_id"] == pending["draft_id"]
    assert completed["answered_question_count"] == 1
    assert completed["completion"]["status"] == "completed"
    assert completed["completion"]["clarification_round"] == 1
    assert completed["saved_to_smb"] is True
    assert completed["download_url"]
    assert len(writer.proposal_writes) == 1
    assert writer.proposal_deadlines == [generator.deadlines[-1]]
    assert generator.assessment_count == 2


def test_proposal_revision_inherits_scope_and_type_creates_v11_v12_and_preserves_base(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    fields = ProposalDraftFields(
        title="합성 자동화 구매 계획",
        approval_request="관련 내용을 검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 자동화 장비를 구매합니다.",
    )
    draft_generator = StubDraftGenerator(fields)
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "playground" / "templates" / "proposal_draft.xlsx"
    )
    service = ProposalDraftService(
        settings,
        manager,
        template_path=template_path,
        clock=lambda: datetime(2026, 7, 29, 15, 30, tzinfo=UTC),
        draft_generator=draft_generator,
    )
    retriever = StubRetriever()
    runtime = DocumentRuntime(
        settings=settings,
        file_searcher=StubFileSearcher(),
        scoped_retriever=retriever,
        upload_manager=manager,
    )
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager, service, runtime_getter=lambda: runtime))
    selected = retriever.citations[0]
    selected_files = [
        {
            "source": "llmops",
            "doc_id": str(selected.doc_id),
            "revision_id": str(selected.revision_id),
            "file_name": "synthetic.xlsx",
            "title": "합성 구매 근거",
        }
    ]
    generated = _request(
        app,
        "POST",
        "/api/playground/drafts/proposal",
        json_body={"instruction": "합성 구매 계획 기안을 작성해 줘", "selected_files": selected_files},
    ).json()
    original_bytes = _request(app, "GET", generated["download_url"]).content

    first = _request(
        app,
        "POST",
        f"/api/playground/drafts/proposal/{generated['draft_id']}/revisions",
        json_body={"feedback": "요청 문장을 간결하게 수정해 줘", "selected_files": selected_files},
    )

    assert first.status_code == 201
    first_payload = first.json()
    assert first_payload["revision_of_draft_id"] == generated["draft_id"]
    assert first_payload["revision_number"] == "1.1"
    assert first_payload["file_name"].endswith("_v1.1.xlsx")
    assert first_payload["proposal_type"] == "purchase"
    assert first_payload["proposal_type_source"] == "rule"
    assert first_payload["revision_summary"] == "피드백을 반영한 v1.1 수정본입니다. 변경 항목: 본문."
    assert set(first_payload["timings_ms"]) == {
        "validation",
        "retrieval",
        "evidence_filter",
        "llm",
        "verification",
        "workbook",
        "smb",
    }
    assert _request(app, "GET", generated["download_url"]).content == original_bytes
    assert _request(app, "GET", first_payload["download_url"]).content != original_bytes

    second = _request(
        app,
        "POST",
        f"/api/playground/drafts/proposal/{first_payload['draft_id']}/revisions",
        json_body={"feedback": "본문을 한 번 더 다듬어 줘", "selected_files": selected_files},
    )
    assert second.status_code == 201
    assert second.json()["revision_number"] == "1.2"
    assert second.json()["file_name"].endswith("_v1.2.xlsx")
    assert [name for name, _ in writer.proposal_writes] == [
        generated["file_name"],
        first_payload["file_name"],
        second.json()["file_name"],
    ]
    assert writer.proposal_deadlines == draft_generator.deadlines


def test_event_proposal_does_not_call_llm_when_only_receipt_evidence_remains(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    generator = StubDraftGenerator(
        ProposalDraftFields(title="합성 행사 참가", approval_request="검토 바랍니다.", body="합성 본문")
    )
    service = ProposalDraftService(settings, manager, draft_generator=generator)
    receipt = _citation().model_copy(update={"title": "합성 행사 결제 영수증", "excerpt": "합성 카드 전표 승인 내역"})
    retriever = StubRetriever([receipt])
    runtime = DocumentRuntime(
        settings=settings,
        file_searcher=StubFileSearcher(),
        scoped_retriever=retriever,
        upload_manager=manager,
    )
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager, service, runtime_getter=lambda: runtime))

    response = _request(
        app,
        "POST",
        "/api/playground/drafts/proposal",
        json_body={
            "instruction": "합성 박람회 참가 기안을 작성해 줘",
            "proposal_type": "event_attendance",
            "selected_files": [
                {
                    "source": "llmops",
                    "doc_id": str(receipt.doc_id),
                    "revision_id": str(receipt.revision_id),
                    "file_name": "synthetic-receipt.pdf",
                    "title": receipt.title,
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "proposal_relevant_evidence_unavailable"
    assert response.json()["detail"]["evidence_filter"]["excluded_reason_counts"] == {"receipt": 1}
    assert generator.instructions == []
    assert writer.proposal_writes == []


def test_proposal_revision_rejects_a_different_selected_file_snapshot_before_retrieval(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    generator = StubDraftGenerator(
        ProposalDraftFields(title="합성 계획", approval_request="검토 바랍니다.", body="합성 본문")
    )
    service = ProposalDraftService(settings, manager, draft_generator=generator)
    retriever = StubRetriever()
    runtime = DocumentRuntime(
        settings=settings,
        file_searcher=StubFileSearcher(),
        scoped_retriever=retriever,
        upload_manager=manager,
    )
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager, service, runtime_getter=lambda: runtime))
    citation = retriever.citations[0]
    selected_files = [
        {
            "source": "llmops",
            "doc_id": str(citation.doc_id),
            "revision_id": str(citation.revision_id),
            "file_name": "synthetic.xlsx",
            "title": "합성 근거",
        }
    ]
    generated = _request(
        app,
        "POST",
        "/api/playground/drafts/proposal",
        json_body={"instruction": "합성 계획 기안을 작성해 줘", "selected_files": selected_files},
    ).json()
    calls_before = len(retriever.calls)
    changed_snapshot = [dict(selected_files[0], doc_id=str(uuid4()))]

    response = _request(
        app,
        "POST",
        f"/api/playground/drafts/proposal/{generated['draft_id']}/revisions",
        json_body={"feedback": "문구를 수정해 줘", "selected_files": changed_snapshot},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "proposal_revision_scope_mismatch"
    assert len(retriever.calls) == calls_before


def test_legacy_nas_fallback_is_usable_only_with_safe_relative_directory(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        smb_host="",
        smb_share_name="",
        smb_username="",
        smb_password="",
        smb_upload_default_relative_directory="",
        nas_url="smb://synthetic-server",
        nas_source_name="legacy-source-name",
        nas_fold_path="synthetic-share/team",
        nas_user="synthetic-user",
        nas_pw="synthetic-password",
    )
    manager = UploadManager(settings, writer=StubWriter())

    response = manager.get_settings()

    assert settings.effective_smb_upload_share_name == "synthetic-share"
    assert response.upload.configured is True
    assert response.upload.relative_directory == "team"

    unsafe = _settings(
        tmp_path,
        smb_upload_default_relative_directory="",
        nas_fold_path=r"\\synthetic-server\synthetic-share\team",
    )
    assert UploadManager(unsafe, writer=StubWriter()).get_settings().upload.relative_directory == ""


def test_legacy_runtime_value_migrates_from_old_fold_path_interpretation(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        smb_host="",
        smb_share_name="",
        smb_username="",
        smb_password="",
        smb_upload_default_relative_directory="",
        nas_url="smb://synthetic-server",
        nas_source_name="legacy-source-name",
        nas_fold_path="synthetic-share/team",
        nas_user="synthetic-user",
        nas_pw="synthetic-password",
    )
    runtime_path = Path(settings.smb_upload_runtime_settings_path)
    runtime_path.write_text(
        json.dumps({"upload": {"relative_directory": "synthetic-share/team"}}),
        encoding="utf-8",
    )

    response = UploadManager(settings, writer=StubWriter()).get_settings()

    assert response.upload.relative_directory == "team"


@pytest.mark.parametrize(
    "value",
    [r"\\synthetic-server\synthetic-share", r"C:\synthetic", "../synthetic", "team/../synthetic", "/synthetic"],
)
def test_settings_api_rejects_non_relative_or_traversal_paths(tmp_path: Path, value: str) -> None:
    settings = _settings(tmp_path)
    manager = UploadManager(settings, writer=StubWriter())
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager))

    response = _request(
        app,
        "PATCH",
        "/api/playground/settings/upload",
        json_body={"relative_directory": value},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_relative_directory"


def test_upload_api_uses_multipart_file_field_and_returns_no_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager))

    response = _request(
        app,
        "POST",
        "/api/playground/files/upload",
        files={"file": ("synthetic.txt", b"synthetic body", "text/plain")},
    )

    assert response.status_code == 201
    assert response.json()["file_name"] == "[업로드] synthetic_20260101_v1.0.txt"
    assert response.json()["size_bytes"] == 14
    assert response.json()["indexed"] is False
    assert response.json()["conversation_ready"] is True
    assert response.json()["selected_file"]["source"] == "upload"
    assert response.json()["selected_file"]["file_name"] == "[업로드] synthetic_20260101_v1.0.txt"
    assert "path" not in response.text.lower()
    assert writer.received == b"synthetic body"


def test_uploaded_file_is_immediately_retrievable_as_conversation_evidence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)

    response = manager.upload(io.BytesIO(b"Synthetic milestone is Friday."), "synthetic.txt")
    assert response.selected_file is not None

    result = manager.retrieve(
        "When is the synthetic milestone?",
        [(response.selected_file.doc_id, response.selected_file.revision_id)],
    )

    assert result.metadata.grounded is True
    assert result.metadata.scope[0].doc_id == response.selected_file.doc_id
    assert result.citations[0].location["source"] == "upload"
    assert "Friday" in result.citations[0].excerpt
    assert writer.read_deadlines[0] is not None


def test_upload_retrieval_does_not_read_after_deadline(tmp_path: Path) -> None:
    writer = StubWriter()
    manager = UploadManager(_settings(tmp_path), writer=writer, clock=lambda: 100.0)
    response = manager.upload(io.BytesIO(b"Synthetic evidence."), "synthetic.txt")
    assert response.selected_file is not None

    with pytest.raises(UploadError) as captured:
        manager.retrieve(
            "synthetic",
            [(response.selected_file.doc_id, response.selected_file.revision_id)],
            deadline=100.0,
        )

    assert captured.value.code == "uploaded_retrieval_budget_exhausted"
    assert writer.read_deadlines == []


def test_upload_selection_validation_checks_budget_after_registry_completion(tmp_path: Path) -> None:
    ticks = iter([99.0, 100.0])
    registry = CountingRegistry()
    manager = UploadManager(
        _settings(tmp_path),
        writer=StubWriter(),
        registry=registry,
        clock=lambda: next(ticks),
    )
    selection = (uuid4(), uuid4())

    with pytest.raises(UploadError) as captured:
        manager.validate_selections([selection], deadline=100.0)

    assert captured.value.code == "uploaded_retrieval_budget_exhausted"
    assert registry.lookups == [selection]


def test_upload_retrieval_stops_before_extraction_when_read_consumes_deadline(tmp_path: Path) -> None:
    now = [100.0]

    class BudgetConsumingWriter(StubWriter):
        def read(
            self,
            file_name: str,
            relative_directory: str,
            *,
            expected_size: int,
            deadline: float | None = None,
        ) -> bytes:
            data = super().read(
                file_name,
                relative_directory,
                expected_size=expected_size,
                deadline=deadline,
            )
            assert deadline is not None
            now[0] = deadline
            return data

    writer = BudgetConsumingWriter()
    manager = UploadManager(_settings(tmp_path), writer=writer, clock=lambda: now[0])
    response = manager.upload(io.BytesIO(b"Synthetic evidence."), "synthetic.txt")
    assert response.selected_file is not None

    with pytest.raises(UploadError) as captured:
        manager.retrieve(
            "synthetic",
            [(response.selected_file.doc_id, response.selected_file.revision_id)],
            deadline=101.0,
        )

    assert captured.value.code == "uploaded_retrieval_budget_exhausted"


def test_directory_and_filename_validation_rejects_path_semantics() -> None:
    assert normalize_relative_directory(r"team\inbox") == "team/inbox"
    assert sanitize_filename("synthetic report.pdf") == "synthetic report.pdf"

    for name in (r"..\synthetic.pdf", "folder/synthetic.pdf", "CON.txt", "synthetic.pdf. ", "bad?.pdf"):
        with pytest.raises(UploadError) as raised:
            sanitize_filename(name)
        assert raised.value.code == "invalid_file_name"


def test_upload_filename_uses_korean_date_initial_version_and_preserves_extension() -> None:
    uploaded_at = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)

    assert build_upload_filename("문서버전관리.docx", uploaded_at) == "[업로드] 문서버전관리_20260730_v1.0.docx"
    assert (
        build_upload_filename("[업로드] 문서버전관리_20260701_v1.0.docx", uploaded_at)
        == "[업로드] 문서버전관리_20260730_v1.0.docx"
    )


class _RemoteBuffer(io.BytesIO):
    def __init__(self, fake: "FakeSmb", path: str) -> None:
        super().__init__()
        self._fake = fake
        self._path = path

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self._fake.files[self._path] = self.getvalue()
        self.close()


class _FakePath:
    def __init__(self, fake: "FakeSmb") -> None:
        self._fake = fake

    def isdir(self, _path: str, **kwargs) -> bool:  # noqa: ANN003
        self._fake.path_connection_timeouts.append(kwargs.get("connection_timeout"))
        return True

    def exists(self, path: str, **_kwargs) -> bool:  # noqa: ANN003
        return path in self._fake.files


class FakeSmb:
    def __init__(self) -> None:
        self.path = _FakePath(self)
        self.files: dict[str, bytes] = {}
        self.removed: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.opened: list[tuple[str, str]] = []
        self.session_calls = 0
        self.session_connection_timeouts: list[int | None] = []
        self.path_connection_timeouts: list[int | None] = []
        self.open_connection_timeouts: list[int | None] = []

    def register_session(self, _host: str, **kwargs) -> None:  # noqa: ANN003
        self.session_calls += 1
        self.session_connection_timeouts.append(kwargs.get("connection_timeout"))
        return None

    def reset_connection_cache(self) -> None:
        return None

    def open_file(self, path: str, *, mode: str, **_kwargs) -> _RemoteBuffer:  # noqa: ANN003
        assert mode == "xb"
        self.open_connection_timeouts.append(_kwargs.get("connection_timeout"))
        self.opened.append((path, mode))
        if path in self.files:
            raise FileExistsError(path)
        return _RemoteBuffer(self, path)

    def remove(self, path: str) -> None:
        self.removed.append(path)
        self.files.pop(path, None)

    def rename(self, source: str, destination: str) -> None:
        self.renamed.append((source, destination))
        raise AssertionError("공유폴더 쓰기 경로는 rename을 호출하면 안 됩니다.")


class _DeadlineCrossingRemoteBuffer(_RemoteBuffer):
    def __init__(self, fake: "FakeSmb", path: str, on_close) -> None:  # noqa: ANN001
        super().__init__(fake, path)
        self._on_close = on_close

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        super().__exit__(exc_type, exc, traceback)
        self._on_close()


class DeadlineCrossingSmb(FakeSmb):
    def __init__(self, on_close) -> None:  # noqa: ANN001
        super().__init__()
        self._on_close = on_close

    def open_file(self, path: str, *, mode: str, **kwargs) -> _RemoteBuffer:  # noqa: ANN003
        assert mode == "xb"
        self.open_connection_timeouts.append(kwargs.get("connection_timeout"))
        self.opened.append((path, mode))
        if path in self.files:
            raise FileExistsError(path)
        return _DeadlineCrossingRemoteBuffer(self, path, self._on_close)


class CollisionSmb(FakeSmb):
    """실제 smbclient처럼 충돌을 FileExistsError가 아닌 SMBOSError로 반환한다."""

    def open_file(self, path: str, *, mode: str, **_kwargs) -> _RemoteBuffer:  # noqa: ANN003
        assert mode == "xb"
        self.opened.append((path, mode))
        raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_COLLISION, path)


def test_smb_writer_rejects_oversize_before_creating_remote_file(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(_settings(tmp_path, smb_upload_max_size_bytes=4), smb_module=fake)

    with pytest.raises(UploadError) as raised:
        writer.upload(io.BytesIO(b"12345"), "synthetic.txt", "team/inbox")

    assert raised.value.code == "file_too_large"
    assert fake.files == {}
    assert fake.opened == []
    assert fake.removed == []
    assert fake.renamed == []


def test_smb_reader_does_not_connect_after_deadline(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(
        _settings(tmp_path),
        smb_module=fake,
        monotonic=lambda: 100.0,
    )

    with pytest.raises(UploadError) as captured:
        writer.read("synthetic.txt", "team/inbox", expected_size=4, deadline=100.0)

    assert captured.value.code == "uploaded_file_read_timeout"
    assert fake.session_calls == 0
    assert fake.opened == []


def test_proposal_writer_does_not_connect_or_create_after_deadline(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(_settings(tmp_path), smb_module=fake, monotonic=lambda: 100.0)

    with pytest.raises(UploadError) as captured:
        writer.create_xlsx(b"PK\x03\x04synthetic", "proposal.xlsx", "team/proposal", deadline=100.0)

    assert captured.value.code == "upload_timeout"
    assert fake.session_calls == 0
    assert fake.opened == []


def test_proposal_writer_bounds_smb_timeouts_to_request_deadline(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(
        _settings(tmp_path, smb_upload_timeout_ms=5000),
        smb_module=fake,
        monotonic=lambda: 100.0,
    )

    writer.create_xlsx(b"PK\x03\x04synthetic", "proposal.xlsx", "team/proposal", deadline=102.9)

    assert fake.session_connection_timeouts == [2]
    assert fake.path_connection_timeouts == [2]
    assert fake.open_connection_timeouts == [2]


def test_committed_proposal_crossing_deadline_returns_success_and_registers_download(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [100.0]
    settings = _settings(
        tmp_path,
        proposal_draft_default_relative_directory="team/proposal",
        smb_upload_timeout_ms=5000,
        smb_upload_max_size_bytes=5_000_000,
    )
    fake = DeadlineCrossingSmb(lambda: now.__setitem__(0, 102.0))
    writer = SmbUploadWriter(settings, smb_module=fake, monotonic=lambda: now[0])
    manager = UploadManager(settings, writer=writer)
    generator = StubDraftGenerator(
        ProposalDraftFields(
            title="합성 기한 초과 저장",
            approval_request="합성 요청을 승인하여 주시기 바랍니다.",
            body="합성 근거를 반영한 본문입니다.",
        )
    )
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "playground" / "templates" / "proposal_draft.xlsx"
    )
    service = ProposalDraftService(
        settings,
        manager,
        template_path=template_path,
        draft_generator=generator,
        monotonic=lambda: now[0],
    )
    caplog.set_level("WARNING")

    response = service.generate(
        "합성 구매 기안을 작성해 줘",
        [_citation()],
        selected_files=[],
        proposal_type="purchase",
        deadline=102.0,
    )

    download = service.get_download(response.draft_id)
    assert response.saved_to_smb is True
    assert download.file_name == response.file_name
    assert len(fake.files) == 1
    assert "over_budget=true" in caplog.text
    assert response.file_name not in caplog.text


def test_smb_writer_explicit_gate_blocks_before_any_smb_write(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(_settings(tmp_path, smb_upload_enabled=False), smb_module=fake)

    with pytest.raises(UploadError) as raised:
        writer.upload(io.BytesIO(b"safe"), "synthetic.txt", "team/inbox")

    assert raised.value.code == "upload_disabled"
    assert fake.files == {}
    assert fake.removed == []


def test_smb_writer_creates_final_name_exclusively_without_rename_or_remove(tmp_path: Path) -> None:
    fake = FakeSmb()
    uploaded_at = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
    writer = SmbUploadWriter(_settings(tmp_path), smb_module=fake, clock=lambda: uploaded_at)

    response = writer.upload(io.BytesIO(b"safe"), "synthetic.txt", "team/inbox")

    expected_name = build_upload_filename("synthetic.txt", uploaded_at)
    assert response.file_name == expected_name
    assert fake.opened == [(next(iter(fake.files)), "xb")]
    assert next(iter(fake.files)).endswith(expected_name)
    assert fake.files[next(iter(fake.files))] == b"safe"
    assert fake.removed == []
    assert fake.renamed == []


def test_smb_writer_returns_conflict_and_preserves_existing_final_file(tmp_path: Path) -> None:
    fake = FakeSmb()
    uploaded_at = datetime(2026, 7, 29, 15, 30, tzinfo=UTC)
    expected_name = build_upload_filename("synthetic.txt", uploaded_at)
    expected_path = rf"\\synthetic-server\synthetic-share\team\inbox\{expected_name}"
    fake.files[expected_path] = b"existing"
    writer = SmbUploadWriter(_settings(tmp_path), smb_module=fake, clock=lambda: uploaded_at)

    with pytest.raises(UploadError) as raised:
        writer.upload(io.BytesIO(b"new"), "synthetic.txt", "team/inbox")

    assert raised.value.code == "file_already_exists"
    assert raised.value.status_code == 409
    assert fake.files == {expected_path: b"existing"}
    assert fake.removed == []
    assert fake.renamed == []


def test_smb_writer_maps_smbprotocol_name_collision_to_conflict(tmp_path: Path) -> None:
    fake = CollisionSmb()
    writer = SmbUploadWriter(_settings(tmp_path), smb_module=fake)

    with pytest.raises(UploadError) as raised:
        writer.upload(io.BytesIO(b"new"), "synthetic.txt", "team/inbox")

    assert raised.value.code == "file_already_exists"
    assert raised.value.status_code == 409
    assert len(fake.opened) == 1
    assert fake.files == {}
    assert fake.removed == []
    assert fake.renamed == []


def test_proposal_writer_maps_smbprotocol_name_collision_to_conflict(tmp_path: Path) -> None:
    fake = CollisionSmb()
    writer = SmbUploadWriter(_settings(tmp_path), smb_module=fake)

    with pytest.raises(UploadError) as raised:
        writer.create_xlsx(b"synthetic-workbook", "proposal.xlsx", "team/proposal")

    assert raised.value.code == "file_already_exists"
    assert raised.value.status_code == 409
    assert len(fake.opened) == 1
    assert fake.files == {}
    assert fake.removed == []
    assert fake.renamed == []


def test_proposal_xlsx_writer_uses_final_name_without_rename_remove_or_overwrite(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(_settings(tmp_path), smb_module=fake)
    file_name = "[기안] 합성 자동화 계획_20260807_v1.0.xlsx"

    response = writer.create_xlsx(b"PK\x03\x04synthetic", file_name, "team/proposal")

    assert response.file_name == file_name
    assert response.destination_label == "기안 문서 폴더"
    assert fake.opened == [(next(iter(fake.files)), "xb")]
    assert fake.files[next(iter(fake.files))] == b"PK\x03\x04synthetic"
    assert fake.removed == []
    assert fake.renamed == []


def test_proposal_save_conflict_does_not_retry_or_change_the_final_name(tmp_path: Path) -> None:
    settings = _settings(tmp_path, proposal_draft_default_relative_directory="team/proposal")
    fake = FakeSmb()
    writer = SmbUploadWriter(settings, smb_module=fake)
    manager = UploadManager(settings, writer=writer)
    existing_name = "[기안] 합성 자동화 계획_20260807_v1.0.xlsx"
    existing_path = rf"\\synthetic-server\synthetic-share\team\proposal\{existing_name}"
    fake.files[existing_path] = b"existing"

    with pytest.raises(UploadError) as raised:
        manager.save_proposal_draft(b"PK\x03\x04new", existing_name)

    assert raised.value.code == "file_already_exists"
    assert raised.value.status_code == 409
    assert fake.files[existing_path] == b"existing"
    assert len(fake.files) == 1
    assert fake.opened == [(existing_path, "xb")]
    assert fake.removed == []
    assert fake.renamed == []
