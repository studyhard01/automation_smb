import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import FileInspectorDialog from "./FileInspectorDialog.vue";
import type { DocumentSearchHit } from "@/types";

const file: DocumentSearchHit = {
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

describe("FileInspectorDialog", () => {
  it("Backend가 반환한 안전한 preview content만 표시한다", () => {
    const wrapper = mount(FileInspectorDialog, {
      props: {
        file,
        preview: {
          doc_id: file.doc_id!,
          revision_id: file.revision_id!,
          artifact_type: "preview",
          media_type: "text/plain",
          content: "합성 문서 미리보기",
          size_bytes: 21,
          elapsed_ms: 18,
        },
        graph: null,
        previewPending: false,
        graphPending: false,
        error: "",
      },
    });

    expect(wrapper.text()).toContain("합성 문서 미리보기");
    expect(wrapper.text()).not.toContain("object_uri");
  });

  it("버전 탭 최초 선택 시 graph 조회를 요청한다", async () => {
    const wrapper = mount(FileInspectorDialog, {
      props: {
        file,
        preview: null,
        graph: null,
        previewPending: false,
        graphPending: false,
        error: "",
      },
    });

    await wrapper.get("#versionsTab").trigger("click");
    expect(wrapper.emitted("loadGraph")).toHaveLength(1);
  });
});
