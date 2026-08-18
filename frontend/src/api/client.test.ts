import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, playgroundApi } from "./client";
import type { FileUploadResponse, PlaygroundSettingsResponse, ProposalDraftGenerated } from "@/types";

const settingsResponse: PlaygroundSettingsResponse = {
  upload: {
    enabled: true,
    configured: true,
    relative_directory: "playground/uploads",
    destination_label: "공유폴더 업로드 영역",
    max_size_bytes: 1024,
    allowed_extensions: [".md"],
  },
  proposal_draft: {
    relative_directory: "drafts/proposals",
    destination_label: "기안 초안 저장 영역",
  },
  local_llm_configured: true,
};

const uploadResponse: FileUploadResponse = {
  file_name: "[업로드] synthetic_20260805_v1.0.md",
  size_bytes: 9,
  uploaded_at: "2026-08-05T00:00:00Z",
  destination_label: "공유폴더 업로드 영역",
  indexed: false,
  conversation_ready: true,
  selected_file: {
    source: "upload",
    doc_id: "33333333-3333-3333-3333-333333333333",
    revision_id: "44444444-4444-4444-4444-444444444444",
    file_name: "[업로드] synthetic-note_20260805_v1.0.md",
    title: "synthetic-note",
    extension: ".md",
    size_bytes: 9,
    score: 1,
    match_source: "content",
  },
};

const proposalDraftResponse: ProposalDraftGenerated = {
  draft_id: "77777777-7777-7777-7777-777777777777",
  title: "합성 자동화 구매 계획",
  fields: {
    title: "합성 자동화 구매 계획",
    approval_request: "검토 후 재가하여 주시기 바랍니다.",
    body: "합성 자동화 교육 참석을 요청합니다.",
  },
  document: {
    schema_version: "proposal-document-v2",
    proposal_type: "purchase",
    title: "합성 자동화 구매 계획",
    approval_request: "검토 후 재가하여 주시기 바랍니다.",
    sections: [{
      heading: "주요 내용",
      semantic_role: "details",
      citations: ["E001"],
      blocks: [{ type: "paragraph", text: "합성 자동화 교육 참석을 요청합니다." }],
      missing_information: [],
    }],
    missing_information: [],
  },
  proposal_type: "purchase",
  proposal_type_source: "user",
  evidence_filter: {
    schema_version: "proposal-evidence-filter-v1",
    input_document_count: 1,
    included_document_count: 1,
    excluded_document_count: 0,
    input_citation_count: 1,
    included_citation_count: 1,
    excluded_citation_count: 0,
    excluded_reason_counts: {},
    fallback_used: false,
  },
  context_usage: {
    schema_version: "proposal-context-usage-v1",
    source_citation_count: 1,
    packed_citation_count: 1,
    deduplicated_citation_count: 0,
    source_document_count: 1,
    packed_document_count: 1,
    context_budget_chars: 4000,
    context_chars: 500,
    estimated_input_tokens: 313,
    truncated: false,
    retry_count: 0,
    first_attempt_context_chars: null,
    prompt_eval_count: null,
  },
  completion: {
    schema_version: "proposal-completion-v1",
    status: "completed",
    questions: [],
    supported_claim_count: 1,
    derived_claim_count: 0,
    omitted_claim_count: 0,
    conflicting_claim_count: 0,
    clarification_round: 0,
  },
  file_name: "[기안] 합성 자동화 구매 계획_20260807_v1.0.xlsx",
  download_url: "/api/playground/drafts/proposal/77777777-7777-7777-7777-777777777777",
  destination_label: "기안 문서 폴더",
  saved_to_smb: true,
  model_used: "synthetic-model",
  elapsed_ms: 321,
  timings_ms: { llm: 300, workbook: 10, smb: 11 },
};

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function errorResponse(status: number, body: unknown): Response {
  return {
    ok: false,
    status,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("playgroundApi settings and upload", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("비밀정보 없는 Playground 설정을 조회하고 상대 경로만 PATCH한다", async () => {
    fetchMock.mockResolvedValueOnce(response(settingsResponse));
    await expect(playgroundApi.getSettings()).resolves.toEqual(settingsResponse);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/playground/settings");

    fetchMock.mockResolvedValueOnce(response(settingsResponse));
    await playgroundApi.updateUploadDirectory("playground/reviewed");
    expect(fetchMock).toHaveBeenLastCalledWith("/api/playground/settings/upload", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ relative_directory: "playground/reviewed" }),
    });

    fetchMock.mockResolvedValueOnce(response(settingsResponse));
    await playgroundApi.updateProposalDraftDirectory("drafts/reviewed");
    expect(fetchMock).toHaveBeenLastCalledWith("/api/playground/settings/proposal-draft", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ relative_directory: "drafts/reviewed" }),
    });
  });

  it("기안 생성 지시를 POST하고 LLM·SMB·다운로드 메타데이터를 보존한다", async () => {
    fetchMock.mockResolvedValueOnce(response(proposalDraftResponse));

    const payload = {
      instruction: "합성 구매 계획 제목을 작성해 줘",
      proposal_type: "purchase" as const,
      selected_files: [uploadResponse.selected_file!].map((file) => ({
        source: file.source,
        file_name: file.file_name,
        title: file.title,
        doc_id: file.doc_id,
        revision_id: file.revision_id,
      })),
    };
    await expect(playgroundApi.generateProposalDraft(payload))
      .resolves.toEqual(proposalDraftResponse);
    expect(fetchMock).toHaveBeenCalledWith("/api/playground/drafts/proposal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  });

  it("기안 수정 피드백과 원본 문서 스냅샷을 초안별 revision endpoint로 보낸다", async () => {
    const revisedDraft = {
      ...proposalDraftResponse,
      draft_id: "88888888-8888-8888-8888-888888888888",
      revision_of_draft_id: proposalDraftResponse.draft_id,
      revision_number: "1.1",
      revision_summary: "상세 일정을 제외하고 참가 목적을 보강했습니다.",
    };
    fetchMock.mockResolvedValueOnce(response(revisedDraft));
    const payload = {
      feedback: "상세 일정은 제외하고 참가 목적을 보강해 줘",
      selected_files: [uploadResponse.selected_file!].map((file) => ({
        source: file.source,
        file_name: file.file_name,
        title: file.title,
        doc_id: file.doc_id,
        revision_id: file.revision_id,
      })),
    };

    await expect(playgroundApi.reviseProposalDraft(proposalDraftResponse.draft_id, payload))
      .resolves.toEqual(revisedDraft);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/playground/drafts/proposal/${proposalDraftResponse.draft_id}/revisions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
  });

  it("필수 확인 답변을 원본 선택 문서 snapshot과 함께 clarification endpoint로 보낸다", async () => {
    const completedDraft = {
      ...proposalDraftResponse,
      draft_id: "99999999-9999-9999-9999-999999999999",
      clarification_of_draft_id: proposalDraftResponse.draft_id,
      answered_question_count: 1,
    };
    fetchMock.mockResolvedValueOnce(response(completedDraft));
    const payload = {
      answers: [{ question_id: "Q001", answer: "승인 금액은 120만원입니다." }],
      selected_files: [uploadResponse.selected_file!].map((file) => ({
        source: file.source,
        file_name: file.file_name,
        title: file.title,
        doc_id: file.doc_id,
        revision_id: file.revision_id,
      })),
    };

    await expect(playgroundApi.clarifyProposalDraft(proposalDraftResponse.draft_id, payload))
      .resolves.toEqual(completedDraft);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/playground/drafts/proposal/${proposalDraftResponse.draft_id}/clarifications`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
  });

  it("FastAPI detail의 오류 코드와 request id를 ApiError에 보존한다", async () => {
    fetchMock.mockResolvedValueOnce(errorResponse(422, {
      detail: {
        code: "proposal_clarification_answers_incomplete",
        message: "표시된 질문에 모두 답해 주세요.",
      },
      request_id: "request-clarification",
    }));

    const request = playgroundApi.clarifyProposalDraft(proposalDraftResponse.draft_id, {
      answers: [{ question_id: "Q001", answer: "합성 답변" }],
      selected_files: [uploadResponse.selected_file!].map((file) => ({
        source: file.source,
        file_name: file.file_name,
        title: file.title,
        doc_id: file.doc_id,
        revision_id: file.revision_id,
      })),
    });

    await expect(request).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      requestId: "request-clarification",
      errorCode: "proposal_clarification_answers_incomplete",
      message: "표시된 질문에 모두 답해 주세요.",
    } satisfies Partial<ApiError>);
  });

  it("파일을 file 필드의 multipart body로 전송하고 indexed=false 응답을 보존한다", async () => {
    fetchMock.mockResolvedValueOnce(response(uploadResponse));
    const file = new File(["synthetic"], "synthetic.md", { type: "text/markdown" });

    await expect(playgroundApi.uploadFile(file)).resolves.toEqual(uploadResponse);
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/playground/files/upload");
    expect(options.method).toBe("POST");
    expect(options.headers).toBeUndefined();
    expect(options.body).toBeInstanceOf(FormData);
    expect((options.body as FormData).get("file")).toBe(file);
  });
});
