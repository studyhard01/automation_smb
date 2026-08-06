import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App.vue";
import { playgroundApi } from "@/api/client";
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
  StoresStatusResponse,
} from "@/types";

vi.mock("@/api/client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status = 500) { super(message); this.status = status; }
  },
  playgroundApi: {
    getStoresStatus: vi.fn(),
    searchFiles: vi.fn(),
    sendChat: vi.fn(),
    previewFile: vi.fn(),
    getFileGraph: vi.fn(),
    getSettings: vi.fn(),
    updateUploadDirectory: vi.fn(),
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

  it("오른쪽 기능은 요약과 보고서만 제공하고 검색 결과에서 버전을 바로 조회한다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.findComponent(FeatureSidebar).props("functions").map((item: { id: string }) => item.id))
      .toEqual(["summary", "report"]);

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

  it("설정 대화상자에서 상대 경로만 PATCH하고 전체 설정 응답을 반영한다", async () => {
    const wrapper = mount(App);
    await flushPromises();

    await wrapper.findComponent(SettingsDialog).vm.$emit("saveUploadDirectory", "playground/reviewed");
    await flushPromises();

    expect(playgroundApi.updateUploadDirectory).toHaveBeenCalledWith("playground/reviewed");
    expect(wrapper.findComponent(SettingsDialog).props("feedbackStatus")).toBe("success");
  });
});
