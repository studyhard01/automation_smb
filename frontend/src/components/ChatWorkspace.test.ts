import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ChatWorkspace from "./ChatWorkspace.vue";
import type { DocumentSearchHit, FunctionDefinition } from "@/types";

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
    expect(wrapper.get("button[type='submit']").text()).toBe("기안 생성");
    await wrapper.get("form.composer").trigger("submit");
    expect(wrapper.emitted("send")).toBeUndefined();
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

  it("생성한 기안 파일을 대화 메시지의 다운로드 버튼으로 표시한다", () => {
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
          },
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
  });
});
