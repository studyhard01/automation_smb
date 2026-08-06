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
});
