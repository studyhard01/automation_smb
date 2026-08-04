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
});
