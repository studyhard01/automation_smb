import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

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

function mountSidebar(
  selectedFiles: DocumentSearchHit[] = [],
  upload: Partial<{
    uploadEnabled: boolean;
    uploadPending: boolean;
    uploadStatus: "idle" | "pending" | "success" | "error";
    uploadFeedback: string;
  }> = {},
) {
  return mount(FileSidebar, {
    props: {
      results: [syntheticFile],
      selectedFiles,
      searchPending: false,
      searchFeedback: "1건을 찾았습니다.",
      uploadEnabled: true,
      uploadPending: false,
      uploadStatus: "idle",
      uploadFeedback: "",
      allowedExtensions: [".md", ".xlsx"],
      ...upload,
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

  it("검색 폼 바로 아래 첨부 버튼에서 선택한 파일을 전달한다", async () => {
    const wrapper = mountSidebar();
    const click = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => undefined);
    await wrapper.get("button.file-upload-button").trigger("click");
    expect(click).toHaveBeenCalledTimes(1);

    const file = new File(["synthetic"], "synthetic-note.md", { type: "text/markdown" });
    const input = wrapper.get<HTMLInputElement>('input[type="file"]');
    expect(input.attributes("accept")).toBe(".md,.xlsx");
    Object.defineProperty(input.element, "files", { configurable: true, value: [file] });
    await input.trigger("change");
    expect(wrapper.emitted("uploadFile")?.[0]).toEqual([file]);
    click.mockRestore();
  });

  it("업로드 상태와 하단 설정 진입점을 별도로 표시한다", async () => {
    const wrapper = mountSidebar([], {
      uploadPending: false,
      uploadStatus: "success",
      uploadFeedback: "업로드 완료 · 아직 검색 인덱스에는 반영되지 않았습니다.",
    });

    expect(wrapper.get(".upload-feedback").classes()).toContain("success");
    expect(wrapper.find(".workspace-card").exists()).toBe(false);
    expect(wrapper.find(".source-panel-note").exists()).toBe(false);
    await wrapper.get("button.settings-button").trigger("click");
    expect(wrapper.emitted("openSettings")).toHaveLength(1);

    await wrapper.setProps({ uploadStatus: "error", uploadFeedback: "파일 첨부 실패" });
    expect(wrapper.get(".upload-feedback").classes()).toContain("error");
    expect(wrapper.get(".upload-feedback").attributes("role")).toBe("alert");
  });

  it("업로드 중에는 첨부 버튼을 잠그고 진행 상태를 표시한다", () => {
    const wrapper = mountSidebar([], {
      uploadPending: true,
      uploadStatus: "pending",
      uploadFeedback: "파일을 공유폴더에 업로드하고 있습니다.",
    });
    expect(wrapper.get("button.file-upload-button").attributes("disabled")).toBeDefined();
    expect(wrapper.get("button.file-upload-button").text()).toContain("업로드 중");
    expect(wrapper.get(".upload-feedback").classes()).toContain("pending");
  });
});
