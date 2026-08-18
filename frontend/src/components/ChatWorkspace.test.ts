import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ChatWorkspace from "./ChatWorkspace.vue";
import type { DocumentSearchHit, FunctionDefinition, ProposalDraftGenerated } from "@/types";

const definition: FunctionDefinition = {
  id: "summary",
  label: "문서 요약",
  title: "문서 요약",
  subtitle: "선택 문서를 요약합니다.",
  placeholder: "선택 문서를 요약해 줘",
  description: "선택 문서의 핵심을 정리해요",
  icon: "Σ",
  requiresFiles: true,
  resultDescription: "요약 결과를 대화에서 확인합니다.",
};

const selectedFile: DocumentSearchHit = {
  source: "llmops",
  doc_id: "11111111-1111-1111-1111-111111111111",
  revision_id: "22222222-2222-2222-2222-222222222222",
  file_name: "synthetic_wbs.xlsx",
  title: "Synthetic WBS",
  extension: ".xlsx",
  size_bytes: 1024,
  score: 1,
  match_source: "metadata",
};

const proposalContract = {
  title: "합성 자동화 구매 계획",
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
} satisfies Pick<
  ProposalDraftGenerated,
  "title" | "document" | "proposal_type" | "proposal_type_source" | "evidence_filter" | "context_usage" | "completion"
>;

describe("ChatWorkspace", () => {
  it("선택 문서를 대화 범위로 표시하고 기본 요약 요청을 보낸다", async () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition,
        messages: [],
        selectedFiles: [selectedFile],
        pending: false,
        conversationStatus: "ready",
      },
    });
    expect(wrapper.text()).toContain("선택 파일 1개");
    expect(wrapper.text()).toContain("synthetic_wbs.xlsx");
    await wrapper.get("form.composer").trigger("submit");
    expect(wrapper.emitted("send")?.[0]).toEqual(["선택 문서를 요약해 줘"]);
  });

  it("검색 응답 후보를 선택 이벤트로 전달한다", async () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition,
        messages: [{
          id: "search-response",
          role: "assistant",
          content: "관련 파일 1개를 찾았습니다.",
          searchResponse: {
            query: "합성 WBS",
            normalized_query: "합성 WBS",
            hits: [selectedFile],
            result_count: 1,
            elapsed_ms: 42,
            over_budget: false,
            source: "llmops",
          },
        }],
        selectedFiles: [],
        pending: false,
        conversationStatus: "success",
      },
    });
    await wrapper.get('[aria-label="synthetic_wbs.xlsx 선택한 파일에 추가"]').trigger("click");
    expect(wrapper.emitted("toggleFile")?.[0]).toEqual([selectedFile]);

    await wrapper.get('[aria-label="synthetic_wbs.xlsx 버전 확인"]').trigger("click");
    expect(wrapper.emitted("inspectVersions")?.[0]).toEqual([selectedFile]);
  });

  it("Enter는 전송하고 Shift+Enter는 줄바꿈 입력을 유지한다", async () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition,
        messages: [],
        selectedFiles: [selectedFile],
        pending: false,
        conversationStatus: "ready",
      },
    });
    const textarea = wrapper.get("textarea");
    await textarea.setValue("첨부 문서 질문");
    await textarea.trigger("keydown", { key: "Enter", shiftKey: true });
    expect(wrapper.emitted("send")).toBeUndefined();

    await textarea.trigger("keydown", { key: "Enter", shiftKey: false });
    expect(wrapper.emitted("send")?.[0]).toEqual(["첨부 문서 질문"]);
  });

  it("기안 설명 모드는 빈 설명을 전송하지 않고 전용 안내와 생성 버튼을 표시한다", async () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition: {
          ...definition,
          label: "기안 설명 입력",
          placeholder: "기안 목적과 요청 내용을 입력하세요",
        },
        messages: [],
        selectedFiles: [selectedFile],
        pending: false,
        conversationStatus: "ready",
        inputMode: "proposal_description",
      },
    });

    expect(wrapper.text()).toContain("기안 설명 입력 대기");
    expect(wrapper.text()).toContain("기안 목적과 요청 내용을 입력하면");
    expect(wrapper.text()).toContain("기안 설명은 최대 2,000자입니다.");
    expect(wrapper.get("textarea").attributes("maxlength")).toBe("2000");
    expect(wrapper.get("button[type='submit']").text()).toBe("기안 생성");
    await wrapper.get("form.composer").trigger("submit");
    expect(wrapper.emitted("send")).toBeUndefined();
  });

  it("기안 설명 모드에서 유형을 선택해 변경 이벤트를 전달한다", async () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition,
        messages: [],
        selectedFiles: [selectedFile],
        pending: false,
        conversationStatus: "ready",
        inputMode: "proposal_description",
        proposalType: "auto",
      },
    });

    const selector = wrapper.get<HTMLSelectElement>('select[aria-label="기안 유형"]');
    expect(selector.element.value).toBe("auto");
    expect(selector.findAll("option").map((option) => option.text())).toEqual([
      "자동 분류",
      "구매",
      "박람회·행사 참석",
      "일반",
    ]);

    await selector.setValue("event_attendance");
    expect(wrapper.emitted("updateProposalType")?.[0]).toEqual(["event_attendance"]);
  });

  it("답변 근거는 기본으로 닫힌 details 패널에 표시한다", () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition,
        messages: [{
          id: "grounded-answer",
          role: "assistant",
          content: "근거가 있는 답변입니다.",
          response: {
            request_id: "request-1",
            session_id: "session-1",
            provider_used: "local",
            model_used: "synthetic-model",
            assistant_message: "근거가 있는 답변입니다.",
            tool_calls: [],
            elapsed_ms: 20,
            warnings: [],
            error_code: "",
            over_budget: false,
            citations: [{
              index: 1,
              doc_id: selectedFile.doc_id,
              revision_id: selectedFile.revision_id,
              chunk_id: "55555555-5555-5555-5555-555555555555",
              title: "Synthetic WBS",
              section_path: ["Sheet1"],
              location: {},
              excerpt: "합성 근거 본문",
              scores: { rrf: 0.1 },
            }],
            retrieval: {
              trace_id: "66666666-6666-6666-6666-666666666666",
              scope: [{ doc_id: selectedFile.doc_id, revision_id: selectedFile.revision_id }],
              candidate_count: 1,
              result_count: 1,
              grounded: true,
              decision: "answerable",
              degraded_dependencies: [],
              timings_ms: {},
              elapsed_ms: 3,
              over_budget: false,
            },
            artifacts: [],
          },
        }],
        selectedFiles: [selectedFile],
        pending: false,
        conversationStatus: "success",
      },
    });
    const details = wrapper.get("details.citation-panel");
    expect(details.attributes("open")).toBeUndefined();
    expect(details.get("summary").text()).toContain("답변 근거");
  });

  it("생성한 기안 파일과 근거 선별 집계를 표시하고 초안 수정 이벤트를 전달한다", async () => {
    const downloadUrl = "/api/playground/drafts/proposal/77777777-7777-7777-7777-777777777777";
    const fileName = "[기안] 합성 자동화 구매 계획_20260807_v1.0.xlsx";
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition,
        messages: [{
          id: "proposal-draft",
          role: "assistant",
          content: "기안 초안을 만들었습니다.\n제목: 합성 자동화 구매 계획",
          proposalDraft: {
            draft_id: "77777777-7777-7777-7777-777777777777",
            ...proposalContract,
            fields: {
              title: "합성 자동화 구매 계획",
              approval_request: "검토 후 재가하여 주시기 바랍니다.",
              body: "합성 자동화 교육 참석을 요청합니다.",
            },
            file_name: fileName,
            download_url: downloadUrl,
            destination_label: "기안 문서 폴더",
            saved_to_smb: true,
            model_used: "synthetic-model",
            elapsed_ms: 321,
            timings_ms: { llm: 300, workbook: 10, smb: 11 },
            evidence_filter: {
              schema_version: "proposal-evidence-filter-v1",
              input_document_count: 3,
              included_document_count: 2,
              excluded_document_count: 1,
              input_citation_count: 8,
              included_citation_count: 5,
              excluded_citation_count: 3,
              excluded_reason_counts: { irrelevant_document: 1 },
              fallback_used: false,
            },
          },
          proposalDraftSelectedFiles: [{
            source: selectedFile.source,
            file_name: selectedFile.file_name,
            title: selectedFile.title,
            doc_id: selectedFile.doc_id,
            revision_id: selectedFile.revision_id,
          }],
        }],
        selectedFiles: [],
        pending: false,
        conversationStatus: "success",
      },
    });

    const download = wrapper.get(`[aria-label="${fileName} 다운로드"]`);
    expect(download.attributes("href")).toBe(downloadUrl);
    expect(download.attributes("download")).toBe(fileName);
    expect(wrapper.text()).toContain("기안 문서 폴더 저장 완료");
    expect(wrapper.text()).toContain("첨부 근거 검토: 전체 3개 · 반영 2개 · 제외 1개");
    expect(wrapper.text()).not.toContain("irrelevant_document");

    await wrapper.get(`[aria-label="${fileName} 초안 수정"]`).trigger("click");
    expect(wrapper.emitted("startProposalRevision")?.[0]).toEqual(["proposal-draft"]);
  });

  it("필수 확인 질문만 표시하고 모든 답변을 한 번에 전달한다", async () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition,
        messages: [{
          id: "proposal-clarification",
          role: "assistant",
          content: "기안 완성 전에 필수 정보 확인이 필요합니다.",
          proposalDraft: {
            draft_id: "99999999-9999-9999-9999-999999999999",
            ...proposalContract,
            fields: {
              title: "합성 구매 요청",
              approval_request: "검토 후 재가하여 주시기 바랍니다.",
              body: "필수 정보를 확인한 뒤 기안 본문을 완성합니다.",
            },
            file_name: null,
            download_url: null,
            destination_label: null,
            saved_to_smb: false,
            model_used: "synthetic-model",
            elapsed_ms: 120,
            timings_ms: { llm: 80, verification: 40 },
            completion: {
              schema_version: "proposal-completion-v1",
              status: "needs_clarification",
              questions: [
                { question_id: "Q001", field_key: "amount", prompt: "승인 금액은 얼마인가요?", reason: "missing" },
                { question_id: "Q002", field_key: "quantity", prompt: "구매 수량은 몇 개인가요?", reason: "missing" },
              ],
              supported_claim_count: 1,
              derived_claim_count: 0,
              omitted_claim_count: 1,
              conflicting_claim_count: 0,
              clarification_round: 0,
            },
          },
          proposalDraftSelectedFiles: [{
            source: selectedFile.source,
            file_name: selectedFile.file_name,
            title: selectedFile.title,
            doc_id: selectedFile.doc_id,
            revision_id: selectedFile.revision_id,
          }],
        }],
        selectedFiles: [],
        pending: false,
        conversationStatus: "ready",
      },
    });

    expect(wrapper.text()).toContain("필수 답변 전에는 엑셀을 만들거나 공유폴더에 저장하지 않습니다.");
    expect(wrapper.find("a").exists()).toBe(false);
    const answers = wrapper.findAll(".proposal-clarification-questions textarea");
    expect(answers).toHaveLength(2);
    expect(wrapper.text()).toContain("각 답변은 최대 500자입니다.");
    expect(answers[0].attributes("maxlength")).toBe("500");
    await answers[0].setValue("120만원입니다.");
    await answers[1].setValue("2개입니다.");
    await wrapper.get(".proposal-clarification-questions button").trigger("click");

    expect(wrapper.emitted("submitProposalClarification")?.[0]).toEqual([
      "proposal-clarification",
      [
        { question_id: "Q001", answer: "120만원입니다." },
        { question_id: "Q002", answer: "2개입니다." },
      ],
    ]);

    const completedMessage = {
      ...wrapper.props("messages")[0],
      proposalClarificationCompleted: true,
    };
    await wrapper.setProps({ messages: [completedMessage] });
    expect(wrapper.text()).toContain("답변을 반영해 최종 기안을 완성했습니다.");
    expect((answers[0].element as HTMLTextAreaElement).disabled).toBe(true);
    expect(wrapper.get(".proposal-clarification-questions button").text()).toBe("답변 반영 완료");
    await wrapper.get(".proposal-clarification-questions button").trigger("click");
    expect(wrapper.emitted("submitProposalClarification")).toHaveLength(1);
  });

  it("수정 피드백 모드는 원본 참고 문서 스냅샷과 원본 보존을 안내한다", async () => {
    const wrapper = mount(ChatWorkspace, {
      props: {
        definition: {
          ...definition,
          label: "기안 수정 피드백",
          title: "기안 초안 수정",
          placeholder: "수정할 내용을 입력하세요",
        },
        messages: [],
        selectedFiles: [],
        pending: false,
        conversationStatus: "ready",
        inputMode: "proposal_revision",
        revisionSourceTitle: "합성 자동화 구매 계획",
        revisionSourceFileCount: 2,
      },
    });

    expect(wrapper.text()).toContain("수정 피드백 입력 대기");
    expect(wrapper.text()).toContain("원본 초안의 참고 문서 2개를 그대로 사용");
    expect(wrapper.text()).toContain("원본 엑셀은 유지됩니다");
    expect(wrapper.text()).toContain("수정 피드백은 최대 2,000자입니다.");
    expect(wrapper.get("textarea").attributes("maxlength")).toBe("2000");
    expect(wrapper.get("button[type='submit']").text()).toBe("수정본 생성");

    await wrapper.get("textarea").setValue("일정을 제외해 줘");
    await wrapper.get("form.composer").trigger("submit");
    expect(wrapper.emitted("send")?.[0]).toEqual(["일정을 제외해 줘"]);

    await wrapper.get("button.composer-cancel-button").trigger("click");
    expect(wrapper.emitted("cancelProposalRevision")).toHaveLength(1);
  });
});
