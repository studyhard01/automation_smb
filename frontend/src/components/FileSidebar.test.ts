import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import FileSidebar from "./FileSidebar.vue";
import type { DocumentSearchHit } from "@/types";

const syntheticFile: DocumentSearchHit = {
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

function mountSidebar(selectedFiles: DocumentSearchHit[] = []) {
  return mount(FileSidebar, {
    props: {
      workspaceStatus: "ready",
      workspaceTitle: "검색 준비 완료",
      workspaceDetail: "합성 테스트 데이터 전용",
      results: [syntheticFile],
      selectedFiles,
      searchPending: false,
      searchFeedback: "1건을 찾았습니다.",
    },
  });
}

describe("FileSidebar", () => {
  it("왼쪽 패널의 자연어 검색문을 전달한다", async () => {
    const wrapper = mountSidebar();
    await wrapper.get("#sourceSearchInput").setValue("진검파트 WBS 찾아줘");
    await wrapper.get("form.source-search").trigger("submit");
    expect(wrapper.emitted("search")?.[0]).toEqual(["진검파트 WBS 찾아줘"]);
  });

  it("검색 결과를 선택 가능한 후보로 제공한다", async () => {
    const wrapper = mountSidebar();
    const result = wrapper.get('[role="option"]');
    expect(result.attributes("aria-selected")).toBe("false");
    await result.trigger("click");
    expect(wrapper.emitted("toggleFile")?.[0]).toEqual([syntheticFile]);
  });

  it("검색 결과에서 선택 없이 바로 버전 확인을 요청한다", async () => {
    const wrapper = mountSidebar();
    await wrapper.get('[aria-label="synthetic_wbs.xlsx 버전 확인"]').trigger("click");
    expect(wrapper.emitted("inspectVersions")?.[0]).toEqual([syntheticFile]);
    expect(wrapper.emitted("toggleFile")).toBeUndefined();
  });

  it("선택 문서를 대화 범위에 표시하고 미리보기를 요청한다", async () => {
    const wrapper = mountSidebar([syntheticFile]);
    expect(wrapper.text()).toContain("1개 · 대화 범위로 사용");
    await wrapper.get('[aria-label="synthetic_wbs.xlsx 미리보기"]').trigger("click");
    expect(wrapper.emitted("previewFile")?.[0]).toEqual([syntheticFile]);
  });
});
