import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App.vue";
import { ApiError, playgroundApi } from "@/api/client";
import ChatWorkspace from "@/components/ChatWorkspace.vue";
import FeatureSidebar from "@/components/FeatureSidebar.vue";
import FileSidebar from "@/components/FileSidebar.vue";
import SettingsDialog from "@/components/SettingsDialog.vue";
import type {
  ChatResponse,
  DocumentSearchHit,
  DocumentSearchResponse,
  FileUploadResponse,
  PlaygroundSettingsResponse,
  ProposalDraftGenerated,
  StoresStatusResponse,
} from "@/types";

vi.mock("@/api/client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    errorCode: string;
    constructor(message: string, status = 500, _requestId = "", errorCode = "") {
      super(message);
      this.status = status;
      this.errorCode = errorCode;
    }
  },
  playgroundApi: {
    getStoresStatus: vi.fn(),
    searchFiles: vi.fn(),
    sendChat: vi.fn(),
    previewFile: vi.fn(),
    getFileGraph: vi.fn(),
    getSettings: vi.fn(),
    updateUploadDirectory: vi.fn(),
    updateProposalDraftDirectory: vi.fn(),
    generateProposalDraft: vi.fn(),
    reviseProposalDraft: vi.fn(),
    clarifyProposalDraft: vi.fn(),
    uploadFile: vi.fn(),
  },
}));

const selectedFile: DocumentSearchHit = {
  source: "llmops",
  doc_id: "11111111-1111-1111-1111-111111111111",
  revision_id: "22222222-2222-2222-2222-222222222222",
  file_name: "synthetic-plan.md",
  title: "합성 프로젝트 계획",
  extension: ".md",
  size_bytes: 128,
  score: 0.92,
  match_source: "content",
};

const searchResponse: DocumentSearchResponse = {
  query: "합성 프로젝트 계획 찾아줘",
  normalized_query: "합성 프로젝트 계획",
  hits: [selectedFile],
  result_count: 1,
  elapsed_ms: 12,
  over_budget: false,
  source: "llmops",
};

const storesStatus: StoresStatusResponse = {
  overall: "ok",
  postgresql: { configured: true, connected: true, degraded: false, message: "ok", latency_ms: 1, metadata: {} },
  minio: { configured: true, connected: true, degraded: false, message: "ok", latency_ms: 1, metadata: {} },
  neo4j: { configured: true, connected: true, degraded: false, message: "ok", latency_ms: 1, metadata: {} },
};

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
  file_name: "[업로드] synthetic-note_20260805_v1.0.md",
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

const chatResponse: ChatResponse = {
  request_id: "request-1",
  session_id: "session-1",
  provider_used: "local",
  model_used: "synthetic-model",
  assistant_message: "선택한 문서를 근거로 답변했습니다.",
  tool_calls: [],
  elapsed_ms: 20,
  warnings: [],
  error_code: "",
  over_budget: false,
  citations: [],
  retrieval: null,
  artifacts: [],
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
  file_name: "[기안] 합성 자동화 구매 계획_20260807_v1.0.xlsx",
  download_url: "/api/playground/drafts/proposal/77777777-7777-7777-7777-777777777777",
  destination_label: "기안 문서 폴더",
  saved_to_smb: true,
  model_used: "synthetic-model",
  elapsed_ms: 321,
  timings_ms: { llm: 300, workbook: 10, smb: 11 },
  proposal_type: "purchase",
  proposal_type_source: "user",
  evidence_filter: {
    schema_version: "proposal-evidence-filter-v1",
    input_document_count: 2,
    included_document_count: 1,
    excluded_document_count: 1,
    input_citation_count: 6,
    included_citation_count: 4,
    excluded_citation_count: 2,
    excluded_reason_counts: { irrelevant_document: 1 },
    fallback_used: false,
  },
  context_usage: {
    schema_version: "proposal-context-usage-v1",
    source_citation_count: 6,
    packed_citation_count: 4,
    deduplicated_citation_count: 0,
    source_document_count: 2,
    packed_document_count: 1,
    context_budget_chars: 4000,
    context_chars: 1200,
    estimated_input_tokens: 750,
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
};

const pendingProposalDraftResponse: ProposalDraftGenerated = {
  ...proposalDraftResponse,
  draft_id: "99999999-9999-9999-9999-999999999999",
  file_name: null,
  download_url: null,
  destination_label: null,
  saved_to_smb: false,
  completion: {
    ...proposalDraftResponse.completion,
    status: "needs_clarification",
    questions: [{
      question_id: "Q001",
      field_key: "total_amount",
      prompt: "승인 금액은 얼마인가요?",
      reason: "missing",
    }],
    clarification_round: 0,
  },
};

const clarifiedProposalDraftResponse: ProposalDraftGenerated = {
  ...proposalDraftResponse,
  draft_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  file_name: "[기안] 합성 자동화 구매 계획_20260807_v1.0_완성.xlsx",
  download_url: "/api/playground/drafts/proposal/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  clarification_of_draft_id: pendingProposalDraftResponse.draft_id,
  answered_question_count: 1,
  completion: {
    ...proposalDraftResponse.completion,
    clarification_round: 1,
  },
};

const revisedProposalDraftResponse: ProposalDraftGenerated = {
  ...proposalDraftResponse,
  draft_id: "88888888-8888-8888-8888-888888888888",
  fields: {
    ...proposalDraftResponse.fields,
    body: "상세 일정은 제외하고 참가 목적과 참가 내용을 중심으로 작성합니다.",
  },
  file_name: "[기안] 합성 자동화 구매 계획_20260807_v1.1.xlsx",
  download_url: "/api/playground/drafts/proposal/88888888-8888-8888-8888-888888888888",
  revision_of_draft_id: proposalDraftResponse.draft_id,
  revision_number: "1.1",
  revision_summary: "상세 일정을 제외하고 참가 목적과 참가 내용을 보강했습니다.",
};

const graphResponse = {
  doc_id: selectedFile.doc_id,
  nodes: [
    { id: selectedFile.revision_id, type: "revision" as const, label: "Revision 2", status: "active" },
  ],
  edges: [],
  degraded: false,
  warnings: [],
  elapsed_ms: 8,
};

describe("App DB document flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: vi.fn() });
    vi.mocked(playgroundApi.getStoresStatus).mockResolvedValue(storesStatus);
    vi.mocked(playgroundApi.searchFiles).mockResolvedValue(searchResponse);
    vi.mocked(playgroundApi.sendChat).mockResolvedValue(chatResponse);
    vi.mocked(playgroundApi.getFileGraph).mockResolvedValue(graphResponse);
    vi.mocked(playgroundApi.getSettings).mockResolvedValue(settingsResponse);
    vi.mocked(playgroundApi.updateUploadDirectory).mockResolvedValue(settingsResponse);
    vi.mocked(playgroundApi.updateProposalDraftDirectory).mockResolvedValue(settingsResponse);
    vi.mocked(playgroundApi.generateProposalDraft).mockResolvedValue(proposalDraftResponse);
    vi.mocked(playgroundApi.reviseProposalDraft).mockResolvedValue(revisedProposalDraftResponse);
    vi.mocked(playgroundApi.clarifyProposalDraft).mockResolvedValue(clarifiedProposalDraftResponse);
    vi.mocked(playgroundApi.uploadFile).mockResolvedValue(uploadResponse);
  });

  it("자연어 검색 결과를 선택하면 document_qa 요청에 UUID scope를 보낸다", async () => {
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.findComponent(FileSidebar).vm.$emit("search", "합성 프로젝트 계획 찾아줘");
    await flushPromises();
    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.vm.$nextTick();
    await wrapper.findComponent(ChatWorkspace).vm.$emit("send", "다음 일정은 언제야?");
    await flushPromises();

    expect(playgroundApi.sendChat).toHaveBeenCalledWith(expect.objectContaining({
      message: "다음 일정은 언제야?",
      mode: "document_qa",
      selected_files: [expect.objectContaining({
        source: "llmops",
        doc_id: selectedFile.doc_id,
        revision_id: selectedFile.revision_id,
      })],
    }));
  });

  it("오른쪽 기능은 요약과 기안 초안만 제공하고 보고서 초안은 제거한다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.findComponent(FeatureSidebar).props("functions").map((item: { id: string }) => item.id))
      .toEqual(["summary", "proposal_draft"]);
    const functionButtons = wrapper.findComponent(FeatureSidebar).findAll("button.function-card");
    expect(functionButtons[0].attributes("disabled")).toBeDefined();
    expect(functionButtons[1].attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("기안 초안 작성");
    expect(wrapper.text()).not.toContain("보고서 초안");
  });

  it("검색 결과에서 문서 버전을 바로 조회한다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.findComponent(FileSidebar).vm.$emit("search", "합성 프로젝트 계획 찾아줘");
    await flushPromises();
    await wrapper.findComponent(FileSidebar).vm.$emit("inspectVersions", selectedFile);
    await flushPromises();

    expect(playgroundApi.getFileGraph).toHaveBeenCalledWith(expect.objectContaining({
      doc_id: selectedFile.doc_id,
      revision_id: selectedFile.revision_id,
    }));
    expect(wrapper.text()).toContain("Revision 2");
  });

  it("파일 선택 후 설명을 요청하고 다음 사용자 메시지로 기안 엑셀을 생성한다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await wrapper.vm.$nextTick();

    expect(playgroundApi.generateProposalDraft).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("기안 목적과 요청 내용을 입력해 주세요.");
    expect(wrapper.findComponent(ChatWorkspace).props("inputMode")).toBe("proposal_description");

    await wrapper.findComponent(ChatWorkspace).vm.$emit("updateProposalType", "purchase");
    await wrapper.vm.$nextTick();

    await wrapper.findComponent(ChatWorkspace).vm.$emit("send", "교육 참석 목적과 비용을 포함해 기안해 줘");
    await flushPromises();

    expect(playgroundApi.generateProposalDraft).toHaveBeenCalledWith({
      instruction: "교육 참석 목적과 비용을 포함해 기안해 줘",
      selected_files: [expect.objectContaining({
        doc_id: selectedFile.doc_id,
        revision_id: selectedFile.revision_id,
      })],
      proposal_type: "purchase",
    });
    const download = wrapper.findComponent(ChatWorkspace).get(`a[href="${proposalDraftResponse.download_url}"]`);
    expect(download.attributes("download")).toBe(proposalDraftResponse.file_name);
    expect(wrapper.text()).toContain(`제목: ${proposalDraftResponse.fields.title}`);
    expect(wrapper.text()).toContain(`결재 부탁 멘트: ${proposalDraftResponse.fields.approval_request}`);
    expect(wrapper.text()).toContain("기안 유형: 구매 · 사용자 선택");
    expect(wrapper.text()).toContain(`선택 문서: ${selectedFile.file_name}`);
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback"))
      .toContain(`${proposalDraftResponse.file_name} 생성 및 공유폴더 저장을 완료했습니다`);
  });

  it("기안 생성이 진행 중이면 연속 실행 요청을 한 번만 처리한다", async () => {
    let resolveGeneration!: (value: ProposalDraftGenerated) => void;
    vi.mocked(playgroundApi.generateProposalDraft).mockReturnValue(new Promise((resolve) => {
      resolveGeneration = resolve;
    }));
    const wrapper = mount(App);
    await flushPromises();
    const sidebar = wrapper.findComponent(FeatureSidebar);

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await sidebar.vm.$emit("runFunction", "proposal_draft");
    const workspace = wrapper.findComponent(ChatWorkspace);
    void workspace.vm.$emit("send", "첫 번째 기안 설명");
    await wrapper.vm.$nextTick();
    void workspace.vm.$emit("send", "두 번째 기안 설명");
    expect(playgroundApi.generateProposalDraft).toHaveBeenCalledTimes(1);

    resolveGeneration(proposalDraftResponse);
    await flushPromises();
  });

  it("필수 질문 답변으로 기안을 완성하고 원 질문 카드의 중복 제출을 막는다", async () => {
    vi.mocked(playgroundApi.generateProposalDraft).mockResolvedValueOnce(pendingProposalDraftResponse);
    const wrapper = mount(App);
    await flushPromises();
    const workspace = wrapper.findComponent(ChatWorkspace);

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await workspace.vm.$emit("send", "합성 구매 기안을 작성해 줘");
    await flushPromises();

    const sourceMessage = workspace.props("messages").find(
      (item) => item.proposalDraft?.draft_id === pendingProposalDraftResponse.draft_id,
    )!;
    expect(workspace.find(".proposal-clarification-questions").exists()).toBe(true);
    expect(workspace.find("a").exists()).toBe(false);

    await workspace.vm.$emit(
      "submitProposalClarification",
      sourceMessage.id,
      [{ question_id: "Q001", answer: "가".repeat(501) }],
    );
    await flushPromises();
    expect(playgroundApi.clarifyProposalDraft).not.toHaveBeenCalled();
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback"))
      .toBe("확인 답변은 질문마다 1~500자로 입력해 주세요.");

    await workspace.get(".proposal-clarification-questions textarea").setValue("120만원입니다.");
    await workspace.get(".proposal-clarification-questions button").trigger("click");
    await flushPromises();

    expect(playgroundApi.clarifyProposalDraft).toHaveBeenCalledWith(
      pendingProposalDraftResponse.draft_id,
      {
        answers: [{ question_id: "Q001", answer: "120만원입니다." }],
        selected_files: [expect.objectContaining({
          doc_id: selectedFile.doc_id,
          revision_id: selectedFile.revision_id,
        })],
      },
    );
    expect(workspace.props("messages").find((item) => item.id === sourceMessage.id)?.proposalClarificationCompleted)
      .toBe(true);
    expect(workspace.get(".proposal-clarification-questions textarea").attributes("disabled")).toBeDefined();
    expect(workspace.get(".proposal-clarification-questions button").text()).toBe("답변 반영 완료");
    expect(workspace.get(`a[href="${clarifiedProposalDraftResponse.download_url}"]`).attributes("download"))
      .toBe(clarifiedProposalDraftResponse.file_name);

    await workspace.vm.$emit(
      "submitProposalClarification",
      sourceMessage.id,
      [{ question_id: "Q001", answer: "다시 제출" }],
    );
    await flushPromises();
    expect(playgroundApi.clarifyProposalDraft).toHaveBeenCalledTimes(1);
  });

  it("확인 답변 실패 시 입력을 보존하고 오류 코드별 안내 후 재시도한다", async () => {
    vi.mocked(playgroundApi.generateProposalDraft).mockResolvedValueOnce(pendingProposalDraftResponse);
    vi.mocked(playgroundApi.clarifyProposalDraft).mockRejectedValueOnce(
      new ApiError("답변이 누락되었습니다.", 422, "request-clarify", "proposal_clarification_answers_incomplete"),
    );
    const wrapper = mount(App);
    await flushPromises();
    const workspace = wrapper.findComponent(ChatWorkspace);

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await workspace.vm.$emit("send", "합성 구매 기안을 작성해 줘");
    await flushPromises();
    const answer = workspace.get<HTMLTextAreaElement>(".proposal-clarification-questions textarea");
    await answer.setValue("120만원입니다.");
    await workspace.get(".proposal-clarification-questions button").trigger("click");
    await flushPromises();

    expect(answer.element.value).toBe("120만원입니다.");
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback"))
      .toContain("표시된 모든 확인 질문에 답한 뒤 다시 시도해 주세요.");

    vi.mocked(playgroundApi.clarifyProposalDraft).mockResolvedValueOnce(clarifiedProposalDraftResponse);
    await workspace.get(".proposal-clarification-questions button").trigger("click");
    await flushPromises();
    expect(playgroundApi.clarifyProposalDraft).toHaveBeenCalledTimes(2);
    expect(workspace.get(`a[href="${clarifiedProposalDraftResponse.download_url}"]`).attributes("href"))
      .toBe(clarifiedProposalDraftResponse.download_url);
  });

  it("생성 당시 문서 스냅샷으로 별도 수정본을 만들고 원본 다운로드를 유지한다", async () => {
    const wrapper = mount(App);
    await flushPromises();
    const workspace = wrapper.findComponent(ChatWorkspace);

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await workspace.vm.$emit("send", "행사 참석 기안을 작성해 줘");
    await flushPromises();

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.vm.$nextTick();
    expect(wrapper.findComponent(FileSidebar).props("selectedFiles")).toEqual([]);

    const originalMessage = workspace.props("messages").find((item) => item.proposalDraft?.draft_id === proposalDraftResponse.draft_id);
    await workspace.vm.$emit("startProposalRevision", originalMessage!.id);
    await wrapper.vm.$nextTick();
    expect(workspace.props("inputMode")).toBe("proposal_revision");
    expect(workspace.props("revisionSourceFileCount")).toBe(1);

    await workspace.vm.$emit("send", "행사 개요와 상세 일정은 제외해 줘");
    await flushPromises();

    expect(playgroundApi.reviseProposalDraft).toHaveBeenCalledWith(proposalDraftResponse.draft_id, {
      feedback: "행사 개요와 상세 일정은 제외해 줘",
      selected_files: [{
        source: selectedFile.source,
        file_name: selectedFile.file_name,
        title: selectedFile.title,
        doc_id: selectedFile.doc_id,
        revision_id: selectedFile.revision_id,
      }],
    });
    expect(workspace.props("messages").filter((item) => item.proposalDraft)).toHaveLength(2);
    expect(workspace.find(`a[href="${proposalDraftResponse.download_url}"]`).exists()).toBe(true);
    expect(workspace.find(`a[href="${revisedProposalDraftResponse.download_url}"]`).exists()).toBe(true);
    expect(wrapper.text()).toContain("수정본 1.1");
    expect(wrapper.text()).toContain(`수정 요약: ${revisedProposalDraftResponse.revision_summary}`);
    expect(workspace.props("inputMode")).toBe("chat");
  });

  it("기안 설명과 수정 피드백의 2,000자 제한을 API 호출 전에 적용한다", async () => {
    const wrapper = mount(App);
    await flushPromises();
    const workspace = wrapper.findComponent(ChatWorkspace);

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await workspace.vm.$emit("send", "가".repeat(2001));
    await flushPromises();
    expect(playgroundApi.generateProposalDraft).not.toHaveBeenCalled();
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback"))
      .toBe("기안 설명은 최대 2,000자까지 입력할 수 있습니다.");

    await workspace.vm.$emit("send", "유효한 기안 설명");
    await flushPromises();
    const originalMessage = workspace.props("messages").find(
      (item) => item.proposalDraft?.draft_id === proposalDraftResponse.draft_id,
    )!;
    await workspace.vm.$emit("startProposalRevision", originalMessage.id);
    await workspace.vm.$emit("send", "나".repeat(2001));
    await flushPromises();
    expect(playgroundApi.reviseProposalDraft).not.toHaveBeenCalled();
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback"))
      .toBe("수정 피드백은 최대 2,000자까지 입력할 수 있습니다.");
  });

  it("기안 생성과 수정 오류를 HTTP 상태보다 backend error_code에 맞춰 안내한다", async () => {
    vi.mocked(playgroundApi.generateProposalDraft).mockRejectedValueOnce(
      new ApiError("context", 422, "request-draft", "proposal_context_limit_exceeded"),
    );
    const wrapper = mount(App);
    await flushPromises();
    const workspace = wrapper.findComponent(ChatWorkspace);
    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await workspace.vm.$emit("send", "합성 구매 기안");
    await flushPromises();
    expect(wrapper.text()).toContain("선택 문서가 기안 처리 범위를 넘었습니다.");

    vi.mocked(playgroundApi.generateProposalDraft).mockResolvedValueOnce(proposalDraftResponse);
    await workspace.vm.$emit("send", "다시 생성");
    await flushPromises();
    const originalMessage = workspace.props("messages").find(
      (item) => item.proposalDraft?.draft_id === proposalDraftResponse.draft_id,
    )!;
    await workspace.vm.$emit("startProposalRevision", originalMessage.id);
    vi.mocked(playgroundApi.reviseProposalDraft).mockRejectedValueOnce(
      new ApiError("clarify first", 409, "request-revision", "proposal_clarification_required"),
    );
    await workspace.vm.$emit("send", "본문을 줄여 줘");
    await flushPromises();
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback"))
      .toContain("이 초안은 필수 확인 질문에 먼저 답해야 합니다.");
  });

  it("알 수 없는 기안 422는 근거 부족이 아닌 입력값 검증 안내로 표시한다", async () => {
    vi.mocked(playgroundApi.generateProposalDraft).mockRejectedValueOnce(
      new ApiError("validation failed", 422),
    );
    const wrapper = mount(App);
    await flushPromises();
    const workspace = wrapper.findComponent(ChatWorkspace);

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await workspace.vm.$emit("send", "합성 구매 기안");
    await flushPromises();

    const feedback = wrapper.findComponent(FeatureSidebar).props("functionFeedback");
    expect(feedback).toContain("입력 길이와 필수 항목을 확인한 뒤 다시 시도해 주세요.");
    expect(feedback).not.toContain("관련 근거를 찾지 못했습니다");
  });

  it("기안 기능은 파일이 없으면 API를 호출하지 않고 왼쪽 파일 선택을 안내한다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await flushPromises();

    expect(playgroundApi.generateProposalDraft).not.toHaveBeenCalled();
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback"))
      .toContain("왼쪽 검색 결과에서 기안에 참고할 파일을 먼저 선택해 주세요");
  });

  it("기안 생성 실패 후에도 설명 입력 모드를 유지해 재시도한다", async () => {
    vi.mocked(playgroundApi.generateProposalDraft).mockRejectedValueOnce(new Error("합성 생성 실패"));
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.findComponent(FileSidebar).vm.$emit("toggleFile", selectedFile);
    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "proposal_draft");
    await wrapper.findComponent(ChatWorkspace).vm.$emit("send", "첫 기안 설명");
    await flushPromises();

    expect(wrapper.findComponent(ChatWorkspace).props("inputMode")).toBe("proposal_description");
    expect(wrapper.text()).toContain("설명을 보완해 다시 전송해 주세요");

    await wrapper.findComponent(ChatWorkspace).vm.$emit("send", "보완한 기안 설명");
    await flushPromises();
    expect(playgroundApi.generateProposalDraft).toHaveBeenCalledTimes(2);
    expect(wrapper.findComponent(ChatWorkspace).props("inputMode")).toBe("chat");
  });

  it("문서 요약은 파일이 없으면 실행하지 않는다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.findComponent(FeatureSidebar).vm.$emit("runFunction", "summary");
    await flushPromises();

    expect(playgroundApi.sendChat).not.toHaveBeenCalled();
    expect(wrapper.findComponent(FeatureSidebar).props("functionFeedback")).toBe("요약할 파일을 먼저 선택해 주세요.");
  });

  it("파일 선택과 해제는 검색 피드백을 회색 상태 문구로 덮어쓰지 않는다", async () => {
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).not.toContain("검색 준비");
    expect(wrapper.text()).not.toContain("일부 기능 제한");
    await wrapper.findComponent(FileSidebar).vm.$emit("search", "합성 프로젝트 계획 찾아줘");
    await flushPromises();
    const sidebar = wrapper.findComponent(FileSidebar);
    const searchFeedback = sidebar.props("searchFeedback");

    await sidebar.vm.$emit("toggleFile", selectedFile);
    await wrapper.vm.$nextTick();
    expect(sidebar.props("searchFeedback")).toBe(searchFeedback);
    await sidebar.vm.$emit("toggleFile", selectedFile);
    await wrapper.vm.$nextTick();
    expect(sidebar.props("searchFeedback")).toBe(searchFeedback);
  });

  it("검증된 파일을 업로드하면 대화 참고 파일에 즉시 추가한다", async () => {
    const wrapper = mount(App);
    await flushPromises();
    const file = new File(["synthetic"], "synthetic-note.md", { type: "text/markdown" });

    await wrapper.findComponent(FileSidebar).vm.$emit("uploadFile", file);
    await flushPromises();

    expect(playgroundApi.uploadFile).toHaveBeenCalledWith(file);
    expect(wrapper.findComponent(FileSidebar).props("uploadStatus")).toBe("success");
    expect(wrapper.findComponent(FileSidebar).props("uploadFeedback")).toContain("대화 참고 파일에 추가했습니다");
    expect(wrapper.findComponent(FileSidebar).props("selectedFiles")).toEqual([uploadResponse.selected_file]);

    await wrapper.findComponent(ChatWorkspace).vm.$emit("send", "첨부 문서를 요약해 줘");
    await flushPromises();
    expect(playgroundApi.sendChat).toHaveBeenLastCalledWith(expect.objectContaining({
      selected_files: [expect.objectContaining({ source: "upload" })],
    }));
  });

  it("동명 파일 충돌은 저장소 장애가 아니라 파일명 변경 안내로 표시한다", async () => {
    vi.mocked(playgroundApi.uploadFile).mockRejectedValue(
      new ApiError("같은 이름의 파일이 이미 있습니다.", 409),
    );
    const wrapper = mount(App);
    await flushPromises();
    const file = new File(["synthetic"], "synthetic-note.md", { type: "text/markdown" });

    await wrapper.findComponent(FileSidebar).vm.$emit("uploadFile", file);
    await flushPromises();

    expect(wrapper.findComponent(FileSidebar).props("uploadStatus")).toBe("error");
    expect(wrapper.findComponent(FileSidebar).props("uploadFeedback"))
      .toBe("파일 첨부 실패: 오늘 같은 이름으로 첨부한 파일이 이미 있습니다. 원본 파일명을 바꾼 뒤 다시 첨부해 주세요.");
  });

  it("설정 대화상자에서 상대 경로만 PATCH하고 전체 설정 응답을 반영한다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.findComponent(SettingsDialog).vm.$emit("saveUploadDirectory", "playground/reviewed");
    await flushPromises();

    expect(playgroundApi.updateUploadDirectory).toHaveBeenCalledWith("playground/reviewed");
    expect(wrapper.findComponent(SettingsDialog).props("feedbackStatus")).toBe("success");

    await wrapper.findComponent(SettingsDialog).vm.$emit("saveProposalDraftDirectory", "drafts/reviewed");
    await flushPromises();
    expect(playgroundApi.updateProposalDraftDirectory).toHaveBeenCalledWith("drafts/reviewed");
    expect(wrapper.findComponent(SettingsDialog).props("feedbackStatus")).toBe("success");
  });
});
