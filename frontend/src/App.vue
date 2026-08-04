<script setup lang="ts">
import { nextTick, onMounted, ref } from "vue";

import { ApiError, playgroundApi } from "@/api/client";
import AppHeader from "@/components/AppHeader.vue";
import ChatWorkspace from "@/components/ChatWorkspace.vue";
import FeatureSidebar from "@/components/FeatureSidebar.vue";
import FileInspectorDialog from "@/components/FileInspectorDialog.vue";
import FileSidebar from "@/components/FileSidebar.vue";
import type {
  ChatUiMessage,
  ConversationDefinition,
  ConversationStatus,
  DocumentPreviewResponse,
  DocumentSearchHit,
  DocumentVersionGraphResponse,
  FunctionDefinition,
  FunctionId,
  SelectedFilePayload,
  StoresStatusResponse,
  WorkspaceStatus,
} from "@/types";

const functions: FunctionDefinition[] = [
  {
    id: "summary",
    label: "문서 요약",
    title: "문서 요약",
    subtitle: "선택한 문서의 핵심 내용을 근거와 함께 정리합니다.",
    placeholder: "선택한 문서의 핵심 내용을 항목별로 요약해 줘",
    description: "선택 문서의 핵심을 정리해요",
    icon: "Σ",
  },
  {
    id: "report",
    label: "보고서 초안",
    title: "보고서 초안",
    subtitle: "선택한 문서만 근거로 검토용 보고서 초안을 작성합니다.",
    placeholder: "선택 문서만 근거로 검토용 보고서 초안을 작성해 줘",
    description: "근거가 표시된 검토용 초안을 만들어요",
    icon: "▤",
  },
];

const chatDefinition: ConversationDefinition = {
  label: "선택 문서 대화",
  title: "선택 문서 대화",
  subtitle: "선택한 문서 안에서 근거를 찾아 답합니다.",
  placeholder: "선택한 문서에 대해 궁금한 내용을 입력하세요",
};

const workspaceStatus = ref<WorkspaceStatus>("loading");
const workspaceTitle = ref("저장소 확인 중");
const workspaceDetail = ref("문서 DB 연결 상태를 확인하고 있습니다.");
const stores = ref<StoresStatusResponse | null>(null);
const conversationStatus = ref<ConversationStatus>("ready");

const fileResults = ref<DocumentSearchHit[]>([]);
const selectedFiles = ref<DocumentSearchHit[]>([]);
const fileSearchPending = ref(false);
const fileSearchFeedback = ref("자연어로 찾을 파일을 설명해 주세요.");

const selectedFunction = ref<FunctionId>("summary");
const messages = ref<ChatUiMessage[]>([]);
const history = ref<Array<{ role: "user" | "assistant"; content: string }>>([]);
const sessionId = ref("");
const chatPending = ref(false);

const inspectedFile = ref<DocumentSearchHit | null>(null);
const filePreview = ref<DocumentPreviewResponse | null>(null);
const fileGraph = ref<DocumentVersionGraphResponse | null>(null);
const previewPending = ref(false);
const graphPending = ref(false);
const inspectorError = ref("");
const fileInspectorDialog = ref<{ open: () => void; openVersions: () => void; close: () => void } | null>(null);

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "알 수 없는 오류가 발생했습니다.";
}

function userFacingApiError(error: unknown, context: "search" | "chat" | "preview" | "graph"): string {
  if (error instanceof ApiError && error.status === 409) {
    return "선택한 문서 버전이 변경됐습니다. 파일을 다시 검색해 주세요.";
  }
  if (error instanceof ApiError && error.status === 503) {
    const label = context === "graph" ? "버전 저장소" : context === "preview" ? "문서 저장소" : "문서 DB";
    return `${label}에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.`;
  }
  if (error instanceof ApiError && error.status === 413) return "미리보기 허용 크기를 넘었습니다.";
  if (error instanceof ApiError && error.status === 404) return "선택한 문서를 찾지 못했습니다. 다시 검색해 주세요.";
  return errorMessage(error);
}

function fileKey(file: DocumentSearchHit): string {
  return `${file.doc_id}:${file.revision_id}`;
}

function selectedFilePayload(file: DocumentSearchHit): SelectedFilePayload {
  return {
    source: "llmops",
    file_name: file.file_name,
    title: file.title || file.file_name,
    doc_id: file.doc_id,
    revision_id: file.revision_id,
  };
}

async function loadStoreStatus(): Promise<void> {
  try {
    stores.value = await playgroundApi.getStoresStatus();
    if (!stores.value.postgresql.connected) {
      workspaceStatus.value = "error";
      workspaceTitle.value = "문서 DB 연결 필요";
      workspaceDetail.value = stores.value.postgresql.message;
      return;
    }
    workspaceStatus.value = "ready";
    workspaceTitle.value = stores.value.overall === "degraded" ? "검색 준비 · 일부 기능 제한" : "검색 준비 완료";
    const optional = [stores.value.minio, stores.value.neo4j].filter((store) => store.connected).length;
    workspaceDetail.value = `PostgreSQL 연결 · 미리보기/버전 저장소 ${optional}/2 연결`;
  } catch (error) {
    workspaceStatus.value = "error";
    workspaceTitle.value = "저장소 상태 확인 실패";
    workspaceDetail.value = userFacingApiError(error, "search");
  }
}

async function searchFiles(query: string): Promise<void> {
  if (fileSearchPending.value) return;
  fileSearchPending.value = true;
  fileSearchFeedback.value = "문서 DB에서 파일을 찾고 있습니다.";
  fileResults.value = [];
  try {
    const response = await playgroundApi.searchFiles(query, 10);
    fileResults.value = response.hits;
    fileSearchFeedback.value = `${response.result_count}건을 ${response.elapsed_ms.toFixed(1)}ms에 찾았습니다. 후보를 선택해 주세요.`;
    messages.value.push(
      { id: crypto.randomUUID(), role: "user", content: query },
      {
        id: crypto.randomUUID(),
        role: "assistant",
        content: response.result_count ? `관련 파일 ${response.result_count}개를 찾았습니다.` : "조건에 맞는 파일을 찾지 못했습니다.",
        searchResponse: response,
      },
    );
  } catch (error) {
    const message = userFacingApiError(error, "search");
    fileSearchFeedback.value = `파일 검색 실패: ${message}`;
    messages.value.push(
      { id: crypto.randomUUID(), role: "user", content: query },
      { id: crypto.randomUUID(), role: "assistant", content: message, error: true },
    );
  } finally {
    fileSearchPending.value = false;
  }
}

function toggleFile(file: DocumentSearchHit): void {
  const key = fileKey(file);
  if (selectedFiles.value.some((item) => fileKey(item) === key)) {
    selectedFiles.value = selectedFiles.value.filter((item) => fileKey(item) !== key);
    fileSearchFeedback.value = `${file.file_name} 선택을 해제했습니다.`;
    return;
  }
  if (selectedFiles.value.length >= 5) {
    fileSearchFeedback.value = "대화에 반영할 파일은 최대 5개까지 선택할 수 있습니다.";
    return;
  }
  selectedFiles.value = [...selectedFiles.value, file];
  fileSearchFeedback.value = `${file.file_name}을 대화 범위에 추가했습니다.`;
}

function removeFile(file: DocumentSearchHit): void {
  selectedFiles.value = selectedFiles.value.filter((item) => fileKey(item) !== fileKey(file));
}

async function previewFile(file: DocumentSearchHit): Promise<void> {
  inspectedFile.value = file;
  filePreview.value = null;
  fileGraph.value = null;
  inspectorError.value = "";
  previewPending.value = true;
  await nextTick();
  fileInspectorDialog.value?.open();
  try {
    filePreview.value = await playgroundApi.previewFile(selectedFilePayload(file));
  } catch (error) {
    inspectorError.value = userFacingApiError(error, "preview");
  } finally {
    previewPending.value = false;
  }
}

async function loadFileGraph(): Promise<void> {
  if (!inspectedFile.value || graphPending.value) return;
  graphPending.value = true;
  inspectorError.value = "";
  try {
    fileGraph.value = await playgroundApi.getFileGraph(selectedFilePayload(inspectedFile.value));
  } catch (error) {
    inspectorError.value = userFacingApiError(error, "graph");
  } finally {
    graphPending.value = false;
  }
}

async function inspectFileVersions(file: DocumentSearchHit): Promise<void> {
  inspectedFile.value = file;
  filePreview.value = null;
  fileGraph.value = null;
  inspectorError.value = "";
  await nextTick();
  fileInspectorDialog.value?.openVersions();
  await loadFileGraph();
}

function startNewConversation(): void {
  if (chatPending.value) return;
  sessionId.value = "";
  history.value = [];
  messages.value = [];
  fileResults.value = [];
  selectedFiles.value = [];
  fileSearchFeedback.value = "자연어로 찾을 파일을 설명해 주세요.";
  selectedFunction.value = "summary";
  conversationStatus.value = "ready";
  inspectedFile.value = null;
  filePreview.value = null;
  fileGraph.value = null;
  inspectorError.value = "";
  fileInspectorDialog.value?.close();
}

async function sendChat(message: string): Promise<void> {
  if (chatPending.value || !selectedFiles.value.length) return;
  chatPending.value = true;
  conversationStatus.value = "loading";
  const pendingId = crypto.randomUUID();
  messages.value.push(
    {
      id: crypto.randomUUID(),
      role: "user",
      content: message,
      selectedFiles: selectedFiles.value.map((file) => file.file_name),
    },
    { id: pendingId, role: "assistant", content: "선택 문서에서 근거를 찾고 있습니다.", pending: true },
  );
  try {
    const response = await playgroundApi.sendChat({
      message,
      mode: "document_qa",
      selected_files: selectedFiles.value.map(selectedFilePayload),
      session_id: sessionId.value,
      history: history.value.slice(-8).map((item) => ({ ...item, content: item.content.slice(0, 1000) })),
    });
    sessionId.value = response.session_id || sessionId.value;
    const index = messages.value.findIndex((item) => item.id === pendingId);
    messages.value.splice(index, 1, {
      id: pendingId,
      role: "assistant",
      content: response.assistant_message || "답변 내용이 없습니다.",
      response,
      error: Boolean(response.error_code),
    });
    history.value = [
      ...history.value,
      { role: "user", content: message } as const,
      { role: "assistant", content: response.assistant_message || "" } as const,
    ].slice(-12);
    conversationStatus.value = response.error_code ? "error" : "success";
  } catch (error) {
    const text = `요청 실패: ${userFacingApiError(error, "chat")}`;
    const index = messages.value.findIndex((item) => item.id === pendingId);
    messages.value.splice(index, 1, { id: pendingId, role: "assistant", content: text, error: true });
    conversationStatus.value = "error";
  } finally {
    chatPending.value = false;
  }
}

async function runFunction(functionId: FunctionId): Promise<void> {
  selectedFunction.value = functionId;
  if (!selectedFiles.value.length) {
    conversationStatus.value = "error";
    fileSearchFeedback.value = "먼저 검색 결과에서 파일을 선택해 주세요.";
    return;
  }
  const prompts: Record<FunctionId, string> = {
    summary: "선택한 문서의 핵심 내용을 근거와 함께 항목별로 요약해 줘",
    report: "선택한 문서만 근거로 검토용 보고서 초안을 작성해 줘",
  };
  await sendChat(prompts[functionId]);
}

onMounted(loadStoreStatus);
</script>

<template>
  <AppHeader :workspace-status="workspaceStatus" :workspace-title="workspaceTitle" />
  <main class="app-shell">
    <FileSidebar
      :workspace-status="workspaceStatus"
      :workspace-title="workspaceTitle"
      :workspace-detail="workspaceDetail"
      :results="fileResults"
      :selected-files="selectedFiles"
      :search-pending="fileSearchPending"
      :search-feedback="fileSearchFeedback"
      @new-conversation="startNewConversation"
      @search="searchFiles"
      @toggle-file="toggleFile"
      @remove-file="removeFile"
      @preview-file="previewFile"
      @inspect-versions="inspectFileVersions"
    />
    <ChatWorkspace
      :definition="chatDefinition"
      :messages="messages"
      :selected-files="selectedFiles"
      :pending="chatPending"
      :conversation-status="conversationStatus"
      @send="sendChat"
      @toggle-file="toggleFile"
      @preview-file="previewFile"
      @inspect-versions="inspectFileVersions"
    />
    <FeatureSidebar
      :functions="functions"
      :selected-function="selectedFunction"
      :selected-file-count="selectedFiles.length"
      :function-pending="chatPending || graphPending"
      :conversation-status="conversationStatus"
      :stores="stores"
      @run-function="runFunction"
    />
  </main>
  <FileInspectorDialog
    ref="fileInspectorDialog"
    :file="inspectedFile"
    :preview="filePreview"
    :graph="fileGraph"
    :preview-pending="previewPending"
    :graph-pending="graphPending"
    :error="inspectorError"
    @load-graph="loadFileGraph"
  />
</template>
