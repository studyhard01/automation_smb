import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SettingsDialog from "./SettingsDialog.vue";
import type { PlaygroundSettingsResponse, StoresStatusResponse } from "@/types";

const settings: PlaygroundSettingsResponse = {
  upload: {
    enabled: true,
    configured: true,
    relative_directory: "playground/uploads",
    destination_label: "공유폴더 업로드 영역",
    max_size_bytes: 10 * 1024 * 1024,
    allowed_extensions: [".pdf", ".md"],
  },
  local_llm_configured: true,
};

const stores: StoresStatusResponse = {
  overall: "degraded",
  postgresql: { configured: true, connected: true, degraded: false, message: "ok", latency_ms: 1, metadata: {} },
  minio: { configured: true, connected: true, degraded: true, message: "slow", latency_ms: 10, metadata: {} },
  neo4j: { configured: true, connected: false, degraded: true, message: "down", latency_ms: 0, metadata: {} },
};

function mountDialog() {
  return mount(SettingsDialog, {
    props: {
      settings,
      stores,
      loading: false,
      savePending: false,
      feedback: "",
      feedbackStatus: "idle",
    },
  });
}

describe("SettingsDialog", () => {
  beforeEach(() => {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: vi.fn() });
    Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: vi.fn() });
  });

  it("비밀 설정 없이 업로드 정책과 네 저장소 상태만 표시한다", () => {
    const wrapper = mountDialog();

    expect(wrapper.get<HTMLInputElement>("#uploadRelativeDirectory").element.value).toBe("playground/uploads");
    expect(wrapper.text()).toContain(".pdf");
    expect(wrapper.text()).toContain("10 MB");
    expect(wrapper.text()).toContain("PostgreSQL");
    expect(wrapper.text()).toContain("MinIO");
    expect(wrapper.text()).toContain("Neo4j");
    expect(wrapper.text()).toContain("로컬 LLM");
    expect(wrapper.find('input[type="password"]').exists()).toBe(false);
    expect(wrapper.find('input[name*="host"]').exists()).toBe(false);
  });

  it("절대·상위 경로는 차단하고 검증된 상대 경로만 저장 요청한다", async () => {
    const wrapper = mountDialog();
    const input = wrapper.get<HTMLInputElement>("#uploadRelativeDirectory");

    await input.setValue("C:\\private");
    await wrapper.get("form.upload-settings-form").trigger("submit");
    expect(wrapper.emitted("saveUploadDirectory")).toBeUndefined();
    expect(wrapper.text()).toContain("상대 경로만 사용할 수 있습니다");

    await input.setValue("playground/reviewed");
    await wrapper.get("form.upload-settings-form").trigger("submit");
    expect(wrapper.emitted("saveUploadDirectory")?.[0]).toEqual(["playground/reviewed"]);
  });
});
