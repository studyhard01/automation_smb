<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";

import { ApiError, playgroundApi } from "@/api/client";
import AppHeader from "@/components/AppHeader.vue";
import ChatWorkspace from "@/components/ChatWorkspace.vue";
import FeatureSidebar from "@/components/FeatureSidebar.vue";
import FileInspectorDialog from "@/components/FileInspectorDialog.vue";
import FileSidebar from "@/components/FileSidebar.vue";
import SettingsDialog from "@/components/SettingsDialog.vue";
import { createUiId } from "@/utils/uiId";
import {
  PROPOSAL_CLARIFICATION_ANSWER_MAX_LENGTH,
  PROPOSAL_TEXT_MAX_LENGTH,
} from "@/types";
import type {
  ChatUiMessage,
  ConversationDefinition,
  ConversationStatus,
  DocumentPreviewResponse,
  DocumentSearchHit,
  DocumentVersionGraphResponse,
  FunctionDefinition,
  FunctionId,
  PlaygroundSettingsResponse,
  ProposalDraftGenerated,
  ProposalClarificationAnswer,
  ProposalTypeRequest,
  SelectedFilePayload,
  StoresStatusResponse,
  UploadStatus,
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
    requiresFiles: true,
    resultDescription: "요약 답변과 근거 문서는 중앙 대화 영역에서 확인할 수 있습니다.",
  },
  {
    id: "proposal_draft",
    label: "기안 초안 작성",
    title: "기안 초안 작성",
    subtitle: "선택 문서를 참고해 유형별 구조와 표가 반영된 기안 엑셀을 생성합니다.",
    placeholder: "기안 목적, 요청 내용, 꼭 포함할 사항을 입력하세요",
    description: "유형에 맞춘 줄바꿈 본문과 실제 표 기안 엑셀을 만들어요",
    icon: "▤",
    requiresFiles: true,
    resultDescription: "공유폴더 저장 후 대화창의 버튼으로 엑셀을 내려받을 수 있습니다.",
  },
];

const chatDefinition: ConversationDefinition = {
  label: "선택 문서 대화",
  title: "선택 문서 대화",
  subtitle: "선택한 문서 안에서 근거를 찾아 답합니다.",
  placeholder: "선택한 문서에 대해 궁금한 내용을 입력하세요",
};

const proposalDescriptionDefinition: ConversationDefinition = {
  label: "기안 설명 입력",
  title: "기안 초안 작성",
  subtitle: "선택한 문서를 참고할 수 있도록 기안 목적과 요청 내용을 설명해 주세요.",
  placeholder: "예: 교육 참석 목적, 필요한 비용, 기대 효과를 포함해 기안해 줘",
};

const proposalRevisionDefinition: ConversationDefinition = {
  label: "기안 수정 피드백",
  title: "기안 초안 수정",
  subtitle: "생성된 초안에 반영할 변경 사항만 구체적으로 입력해 주세요.",
  placeholder: "예: 행사 개요와 상세 일정은 빼고 참가 목적, 참가 내용, 행사 주요 내용만 남겨 줘",
};

interface ProposalRevisionContext {
  sourceDraft: ProposalDraftGenerated;
  selectedFiles: SelectedFilePayload[];
}

const stores = ref<StoresStatusResponse | null>(null);
const conversationStatus = ref<ConversationStatus>("ready");

const fileResults = ref<DocumentSearchHit[]>([]);
const selectedFiles = ref<DocumentSearchHit[]>([]);
const fileSearchPending = ref(false);
const fileSearchFeedback = ref("자연어로 찾을 파일을 설명해 주세요.");
const uploadPending = ref(false);
const uploadStatus = ref<UploadStatus>("idle");
const uploadFeedback = ref("");

const settings = ref<PlaygroundSettingsResponse | null>(null);
const settingsPending = ref(false);
const settingsSavePending = ref(false);
const settingsFeedback = ref("");
const settingsFeedbackStatus = ref<UploadStatus>("idle");

const selectedFunction = ref<FunctionId>("summary");
const proposalDraftPending = ref(false);
const proposalDescriptionMode = ref(false);
const proposalRevisionContext = ref<ProposalRevisionContext | null>(null);
const proposalType = ref<ProposalTypeRequest>("auto");
const functionFeedback = ref("");
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
const settingsDialog = ref<{ open: () => void; close: () => void } | null>(null);

const uploadEnabled = computed(() => Boolean(settings.value?.upload.enabled && settings.value.upload.configured));
const allowedExtensions = computed(() => settings.value?.upload.allowed_extensions || []);
const workspaceDefinition = computed(() => (
  proposalRevisionContext.value
    ? proposalRevisionDefinition
    : proposalDescriptionMode.value
      ? proposalDescriptionDefinition
      : chatDefinition
));

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "알 수 없는 오류가 발생했습니다.";
}

function userFacingApiError(
  error: unknown,
  context: "search" | "chat" | "preview" | "graph" | "upload" | "settings" | "proposal_draft" | "proposal_revision" | "proposal_clarification",
): string {
  if (error instanceof ApiError) {
    const proposalMessages: Record<string, string> = {
      llmops_embedding_timeout: "문서 분석용 로컬 모델을 준비하는 시간이 초과됐습니다. 잠시 후 다시 시도해 주세요.",
      llmops_embedding_unavailable: "문서 분석용 로컬 모델을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
      llmops_embedding_dimension_mismatch: "문서 분석 모델과 검색 인덱스 설정이 맞지 않습니다. 관리자에게 문의해 주세요.",
      llmops_retrieval_budget_exhausted: "선택 문서의 근거를 찾는 시간이 초과됐습니다. 잠시 후 다시 시도해 주세요.",
      proposal_llm_unavailable: "기안 생성용 로컬 LLM을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
      proposal_verification_unavailable: "기안 근거 검증용 로컬 LLM을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
      local_llm_not_configured: "기안 생성용 로컬 LLM 설정이 필요합니다. 관리자에게 문의해 주세요.",
      proposal_relevant_evidence_unavailable: "첨부 문서에서 기안에 반영할 관련 근거를 찾지 못했습니다. 요청서나 행사 안내문처럼 목적과 직접 관련된 문서를 선택해 새 초안을 만들어 주세요.",
      proposal_evidence_unavailable: "기안 근거를 다시 확인할 수 없습니다. 참고 문서를 다시 선택해 새 초안을 만들어 주세요.",
      proposal_context_limit_exceeded: "선택 문서가 기안 처리 범위를 넘었습니다. 참고 문서 수나 내용을 줄여 다시 시도해 주세요.",
      proposal_revision_scope_mismatch: "원본 초안의 참고 문서 범위가 변경되었습니다. 원본 초안 카드에서 다시 수정을 시작해 주세요.",
      proposal_clarification_scope_mismatch: "확인할 초안의 참고 문서 범위가 변경되었습니다. 새 초안을 생성해 주세요.",
      proposal_clarification_answers_incomplete: "표시된 모든 확인 질문에 답한 뒤 다시 시도해 주세요.",
      proposal_clarification_required: "이 초안은 필수 확인 질문에 먼저 답해야 합니다.",
      proposal_clarification_not_required: "이미 확인이 완료된 초안입니다. 완성된 초안 카드의 다운로드를 이용해 주세요.",
      proposal_draft_not_found: "기안 초안 정보가 만료되었거나 없습니다. 새 초안을 생성해 주세요.",
    };
    if (proposalMessages[error.errorCode]) return proposalMessages[error.errorCode];
  }
  if (error instanceof ApiError && error.status === 409) {
    if (context === "upload") {
      return "오늘 같은 이름으로 첨부한 파일이 이미 있습니다. 원본 파일명을 바꾼 뒤 다시 첨부해 주세요.";
    }
    if (context === "proposal_draft") {
      return "같은 이름의 기안 파일이 이미 있습니다. 제목을 구분해 다시 생성해 주세요.";
    }
    if (context === "proposal_revision" || context === "proposal_clarification") {
      return "원본 초안의 참고 문서 범위가 변경되었습니다. 원본 초안 카드에서 다시 수정을 시작해 주세요.";
    }
    return "선택한 문서 버전이 변경됐습니다. 파일을 다시 검색해 주세요.";
  }
  if (error instanceof ApiError && error.status === 503) {
    const label = context === "graph"
      ? "버전 저장소"
      : context === "preview" || context === "upload"
        ? "문서 저장소"
        : context === "proposal_draft" || context === "proposal_revision" || context === "proposal_clarification"
          ? "기안 초안 서비스"
        : context === "settings"
          ? "설정 서비스"
          : "문서 DB";
    return `${label}에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.`;
  }
  if (error instanceof ApiError && error.status === 413) {
    return context === "upload" ? "첨부할 수 있는 파일 크기를 넘었습니다." : "미리보기 허용 크기를 넘었습니다.";
  }
  if (
    error instanceof ApiError
    && error.status === 422
    && (context === "proposal_draft" || context === "proposal_revision" || context === "proposal_clarification")
  ) {
    return "기안 요청의 입력값을 확인할 수 없습니다. 입력 길이와 필수 항목을 확인한 뒤 다시 시도해 주세요.";
  }
  if (error instanceof ApiError && error.status === 404) return "선택한 문서를 찾지 못했습니다. 다시 검색해 주세요.";
  return errorMessage(error);
}

function proposalRetryGuidance(error: unknown): string {
  if (error instanceof ApiError && error.status === 422) {
    return "위 안내에 따라 설명을 보완하거나 참고 문서를 바꿔 다시 전송해 주세요.";
  }
  if (error instanceof ApiError && error.status === 409) {
    return "위 안내에 따라 파일 또는 문서 선택을 갱신한 뒤 다시 전송해 주세요.";
  }
  return "설명 부족으로 발생한 오류가 아닙니다. 잠시 후 다시 전송해 주세요.";
}

function fileKey(file: DocumentSearchHit): string {
  return `${file.doc_id}:${file.revision_id}`;
}

function selectedFilePayload(file: DocumentSearchHit): SelectedFilePayload {
  return {
    source: file.source,
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
      return;
    }
  } catch {
    stores.value = null;
  }
}

async function loadSettings(): Promise<void> {
  if (settingsPending.value) return;
  settingsPending.value = true;
  settingsFeedback.value = "";
  settingsFeedbackStatus.value = "idle";
  try {
    settings.value = await playgroundApi.getSettings();
  } catch (error) {
    settingsFeedback.value = `설정 확인 실패: ${userFacingApiError(error, "settings")}`;
    settingsFeedbackStatus.value = "error";
  } finally {
    settingsPending.value = false;
  }
}

async function reloadSettings(): Promise<void> {
  await Promise.all([loadSettings(), loadStoreStatus()]);
}

async function saveUploadDirectory(relativeDirectory: string): Promise<void> {
  if (settingsSavePending.value) return;
  settingsSavePending.value = true;
  settingsFeedback.value = "업로드 경로를 저장하고 있습니다.";
  settingsFeedbackStatus.value = "pending";
  try {
    settings.value = await playgroundApi.updateUploadDirectory(relativeDirectory);
    settingsFeedback.value = "업로드 상대 경로를 저장했습니다.";
    settingsFeedbackStatus.value = "success";
  } catch (error) {
    settingsFeedback.value = `설정 저장 실패: ${userFacingApiError(error, "settings")}`;
    settingsFeedbackStatus.value = "error";
  } finally {
    settingsSavePending.value = false;
  }
}

async function saveProposalDraftDirectory(relativeDirectory: string): Promise<void> {
  if (settingsSavePending.value) return;
  settingsSavePending.value = true;
  settingsFeedback.value = "기안 저장 경로를 저장하고 있습니다.";
  settingsFeedbackStatus.value = "pending";
  try {
    settings.value = await playgroundApi.updateProposalDraftDirectory(relativeDirectory);
    settingsFeedback.value = "기안 저장 상대 경로를 저장했습니다.";
    settingsFeedbackStatus.value = "success";
  } catch (error) {
    settingsFeedback.value = `설정 저장 실패: ${userFacingApiError(error, "settings")}`;
    settingsFeedbackStatus.value = "error";
  } finally {
    settingsSavePending.value = false;
  }
}

function openSettings(): void {
  settingsDialog.value?.open();
  if (!settings.value && !settingsPending.value) void reloadSettings();
}

function fileExtension(fileName: string): string {
  const index = fileName.lastIndexOf(".");
  return index >= 0 ? fileName.slice(index).toLowerCase() : "";
}

async function uploadFile(file: File): Promise<void> {
  if (uploadPending.value) return;
  if (!settings.value || !uploadEnabled.value) {
    uploadStatus.value = "error";
    uploadFeedback.value = "설정에서 업로드 상대 경로를 먼저 확인해 주세요.";
    return;
  }

  const allowed = new Set(settings.value.upload.allowed_extensions.map((extension) => (
    extension.startsWith(".") ? extension.toLowerCase() : `.${extension.toLowerCase()}`
  )));
  if (!allowed.has(fileExtension(file.name))) {
    uploadStatus.value = "error";
    uploadFeedback.value = "허용되지 않은 파일 형식입니다. 설정에서 허용 확장자를 확인해 주세요.";
    return;
  }
  if (file.size > settings.value.upload.max_size_bytes) {
    uploadStatus.value = "error";
    uploadFeedback.value = "파일이 설정된 최대 크기를 넘었습니다.";
    return;
  }

  uploadPending.value = true;
  uploadStatus.value = "pending";
  uploadFeedback.value = "파일을 공유폴더에 업로드하고 있습니다.";
  try {
    const response = await playgroundApi.uploadFile(file);
    if (response.selected_file) {
      const uploadedFile = response.selected_file;
      const withoutDuplicate = selectedFiles.value.filter((item) => fileKey(item) !== fileKey(uploadedFile));
      selectedFiles.value = [...withoutDuplicate.slice(-4), uploadedFile];
    }
    uploadStatus.value = "success";
    uploadFeedback.value = response.selected_file
      ? "업로드 완료 · 대화 참고 파일에 추가했습니다."
      : "업로드는 완료됐지만 대화 참고 파일에 추가하지 못했습니다.";
  } catch (error) {
    uploadStatus.value = "error";
    uploadFeedback.value = `파일 첨부 실패: ${userFacingApiError(error, "upload")}`;
  } finally {
    uploadPending.value = false;
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
    const storeNames = { postgresql: "PostgreSQL", minio: "MinIO", neo4j: "Neo4j" } as const;
    const storesUsed = (response.queried_stores || ["postgresql"]).map((store) => storeNames[store]).join(" · ");
    const llmState = response.llm_expanded ? " · 로컬 LLM 검색어 확장" : "";
    const degraded = response.warnings?.length ? " · 일부 검색 경로 제한" : "";
    fileSearchFeedback.value = `${response.result_count}건 · ${response.elapsed_ms.toFixed(1)}ms · ${storesUsed}${llmState}${degraded}`;
    messages.value.push(
      { id: createUiId(), role: "user", content: query },
      {
        id: createUiId(),
        role: "assistant",
        content: response.result_count ? `관련 파일 ${response.result_count}개를 찾았습니다.` : "조건에 맞는 파일을 찾지 못했습니다.",
        searchResponse: response,
      },
    );
  } catch (error) {
    const message = userFacingApiError(error, "search");
    fileSearchFeedback.value = `파일 검색 실패: ${message}`;
    messages.value.push(
      { id: createUiId(), role: "user", content: query },
      { id: createUiId(), role: "assistant", content: message, error: true },
    );
  } finally {
    fileSearchPending.value = false;
  }
}

function toggleFile(file: DocumentSearchHit): void {
  const key = fileKey(file);
  if (selectedFiles.value.some((item) => fileKey(item) === key)) {
    selectedFiles.value = selectedFiles.value.filter((item) => fileKey(item) !== key);
    return;
  }
  if (selectedFiles.value.length >= 5) {
    fileSearchFeedback.value = "대화에 반영할 파일은 최대 5개까지 선택할 수 있습니다.";
    return;
  }
  selectedFiles.value = [...selectedFiles.value, file];
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
  if (chatPending.value || proposalDraftPending.value) return;
  sessionId.value = "";
  history.value = [];
  messages.value = [];
  fileResults.value = [];
  selectedFiles.value = [];
  fileSearchFeedback.value = "자연어로 찾을 파일을 설명해 주세요.";
  selectedFunction.value = "summary";
  proposalDescriptionMode.value = false;
  proposalRevisionContext.value = null;
  proposalType.value = "auto";
  functionFeedback.value = "";
  conversationStatus.value = "ready";
  inspectedFile.value = null;
  filePreview.value = null;
  fileGraph.value = null;
  inspectorError.value = "";
  fileInspectorDialog.value?.close();
}

async function sendChat(message: string): Promise<void> {
  if (proposalRevisionContext.value) {
    await reviseProposalDraft(message);
    return;
  }
  if (proposalDescriptionMode.value) {
    await generateProposalDraft(message);
    return;
  }
  if (chatPending.value || !selectedFiles.value.length) return;
  chatPending.value = true;
  conversationStatus.value = "loading";
  const pendingId = createUiId();
  messages.value.push(
    {
      id: createUiId(),
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

async function generateProposalDraft(description: string): Promise<void> {
  const normalizedDescription = description.trim();
  if (proposalDraftPending.value || !selectedFiles.value.length || !normalizedDescription) return;
  if (normalizedDescription.length > PROPOSAL_TEXT_MAX_LENGTH) {
    conversationStatus.value = "error";
    functionFeedback.value = "기안 설명은 최대 2,000자까지 입력할 수 있습니다.";
    return;
  }
  proposalDraftPending.value = true;
  conversationStatus.value = "loading";
  functionFeedback.value = "로컬 LLM이 기안 내용을 작성하고 엑셀에 반영하고 있습니다.";
  const pendingId = createUiId();
  const selectedFileNames = selectedFiles.value.map((file) => file.file_name);
  const selectedFileSnapshot = selectedFiles.value.map(selectedFilePayload);
  messages.value.push(
    {
      id: createUiId(),
      role: "user",
      content: normalizedDescription,
      selectedFiles: selectedFileNames,
    },
    { id: pendingId, role: "assistant", content: "제목·결재 부탁 멘트·본문을 작성하고 있습니다.", pending: true },
  );
  try {
    const proposalDraft = await playgroundApi.generateProposalDraft({
      instruction: normalizedDescription,
      selected_files: selectedFileSnapshot,
      proposal_type: proposalType.value,
    });
    const index = messages.value.findIndex((item) => item.id === pendingId);
    const needsClarification = proposalDraft.completion?.status === "needs_clarification";
    messages.value.splice(index, 1, {
      id: pendingId,
      role: "assistant",
      content: [
        needsClarification ? "기안 완성 전에 필수 정보 확인이 필요합니다." : "기안 초안을 만들었습니다.",
        `제목: ${proposalDraft.fields.title}`,
        `결재 부탁 멘트: ${proposalDraft.fields.approval_request}`,
      ].join("\n"),
      selectedFiles: selectedFileNames,
      proposalDraft,
      proposalDraftSelectedFiles: selectedFileSnapshot,
    });
    proposalDescriptionMode.value = false;
    conversationStatus.value = needsClarification ? "ready" : "success";
    functionFeedback.value = needsClarification
      ? `필수 확인 질문 ${proposalDraft.completion?.questions.length ?? 0}개에 답하면 엑셀을 생성합니다.`
      : `${proposalDraft.file_name} 생성 및 공유폴더 저장을 완료했습니다.`;
  } catch (error) {
    conversationStatus.value = "error";
    const failureMessage = `기안 초안 생성 실패: ${userFacingApiError(error, "proposal_draft")}`;
    const retryGuidance = proposalRetryGuidance(error);
    const index = messages.value.findIndex((item) => item.id === pendingId);
    messages.value.splice(index, 1, {
      id: pendingId,
      role: "assistant",
      content: `${failureMessage}\n${retryGuidance}`,
      error: true,
    });
    functionFeedback.value = `${failureMessage} ${retryGuidance}`;
  } finally {
    proposalDraftPending.value = false;
  }
}

async function submitProposalClarification(messageId: string, answers: ProposalClarificationAnswer[]): Promise<void> {
  if (proposalDraftPending.value || !answers.length) return;
  const sourceMessage = messages.value.find((item) => item.id === messageId);
  if (sourceMessage?.proposalClarificationCompleted) return;
  if (!sourceMessage?.proposalDraft || !sourceMessage.proposalDraftSelectedFiles?.length) {
    conversationStatus.value = "error";
    functionFeedback.value = "확인할 초안의 참고 문서 정보를 찾지 못했습니다. 새 초안을 생성해 주세요.";
    return;
  }
  if (answers.some((answer) => (
    !answer.answer.trim() || answer.answer.trim().length > PROPOSAL_CLARIFICATION_ANSWER_MAX_LENGTH
  ))) {
    conversationStatus.value = "error";
    functionFeedback.value = "확인 답변은 질문마다 1~500자로 입력해 주세요.";
    return;
  }
  proposalDraftPending.value = true;
  conversationStatus.value = "loading";
  functionFeedback.value = "답변을 근거로 다시 검증한 뒤 최종 엑셀을 만들고 있습니다.";
  const pendingId = createUiId();
  messages.value.push(
    {
      id: createUiId(),
      role: "user",
      content: `필수 정보 답변\n${answers.map((answer) => `- ${answer.answer}`).join("\n")}`,
    },
    { id: pendingId, role: "assistant", content: "답변 반영본의 근거를 확인하고 있습니다.", pending: true },
  );
  try {
    const completed = await playgroundApi.clarifyProposalDraft(sourceMessage.proposalDraft.draft_id, {
      answers,
      selected_files: sourceMessage.proposalDraftSelectedFiles,
    });
    const index = messages.value.findIndex((item) => item.id === pendingId);
    messages.value.splice(index, 1, {
      id: pendingId,
      role: "assistant",
      content: ["확인 답변을 반영해 기안을 완성했습니다.", `제목: ${completed.fields.title}`].join("\n"),
      proposalDraft: completed,
      proposalDraftSelectedFiles: sourceMessage.proposalDraftSelectedFiles.map((file) => ({ ...file })),
    });
    sourceMessage.proposalClarificationCompleted = true;
    conversationStatus.value = "success";
    functionFeedback.value = `${completed.file_name} 생성 및 공유폴더 저장을 완료했습니다.`;
  } catch (error) {
    conversationStatus.value = "error";
    const failureMessage = `기안 완성 실패: ${userFacingApiError(error, "proposal_clarification")}`;
    const index = messages.value.findIndex((item) => item.id === pendingId);
    messages.value.splice(index, 1, {
      id: pendingId,
      role: "assistant",
      content: `${failureMessage}\n답변을 확인한 뒤 다시 시도해 주세요.`,
      error: true,
    });
    functionFeedback.value = failureMessage;
  } finally {
    proposalDraftPending.value = false;
  }
}

function startProposalRevision(messageId: string): void {
  if (proposalDraftPending.value) return;
  const sourceMessage = messages.value.find((item) => item.id === messageId);
  if (!sourceMessage?.proposalDraft || !sourceMessage.proposalDraftSelectedFiles?.length) {
    conversationStatus.value = "error";
    functionFeedback.value = "수정할 초안의 참고 문서 정보를 찾지 못했습니다. 새 초안을 생성해 주세요.";
    return;
  }
  proposalDescriptionMode.value = false;
  proposalRevisionContext.value = {
    sourceDraft: sourceMessage.proposalDraft,
    selectedFiles: sourceMessage.proposalDraftSelectedFiles.map((file) => ({ ...file })),
  };
  selectedFunction.value = "proposal_draft";
  conversationStatus.value = "ready";
  functionFeedback.value = "초안에 반영할 수정 피드백 입력을 기다리고 있습니다.";
}

function cancelProposalRevision(): void {
  if (proposalDraftPending.value) return;
  proposalRevisionContext.value = null;
  conversationStatus.value = "ready";
  functionFeedback.value = "기안 초안 수정을 취소했습니다.";
}

async function reviseProposalDraft(feedback: string): Promise<void> {
  const context = proposalRevisionContext.value;
  const normalizedFeedback = feedback.trim();
  if (proposalDraftPending.value || !context || !normalizedFeedback) return;
  if (normalizedFeedback.length > PROPOSAL_TEXT_MAX_LENGTH) {
    conversationStatus.value = "error";
    functionFeedback.value = "수정 피드백은 최대 2,000자까지 입력할 수 있습니다.";
    return;
  }
  proposalDraftPending.value = true;
  conversationStatus.value = "loading";
  functionFeedback.value = "피드백을 반영해 별도의 수정본 엑셀을 만들고 있습니다.";
  const pendingId = createUiId();
  messages.value.push(
    {
      id: createUiId(),
      role: "user",
      content: normalizedFeedback,
    },
    { id: pendingId, role: "assistant", content: "원본은 유지하고 수정본을 생성하고 있습니다.", pending: true },
  );
  try {
    const revisedDraft = await playgroundApi.reviseProposalDraft(context.sourceDraft.draft_id, {
      feedback: normalizedFeedback,
      selected_files: context.selectedFiles,
    });
    const index = messages.value.findIndex((item) => item.id === pendingId);
    messages.value.splice(index, 1, {
      id: pendingId,
      role: "assistant",
      content: [
        "기안 수정본을 만들었습니다.",
        `제목: ${revisedDraft.fields.title}`,
      ].join("\n"),
      proposalDraft: revisedDraft,
      proposalDraftSelectedFiles: context.selectedFiles.map((file) => ({ ...file })),
    });
    proposalRevisionContext.value = null;
    conversationStatus.value = "success";
    functionFeedback.value = `${revisedDraft.file_name} 수정본 생성 및 공유폴더 저장을 완료했습니다.`;
  } catch (error) {
    conversationStatus.value = "error";
    const failureMessage = `기안 수정 실패: ${userFacingApiError(error, "proposal_revision")}`;
    const index = messages.value.findIndex((item) => item.id === pendingId);
    messages.value.splice(index, 1, {
      id: pendingId,
      role: "assistant",
      content: `${failureMessage}\n피드백을 보완해 다시 전송해 주세요.`,
      error: true,
    });
    functionFeedback.value = `${failureMessage} 수정 피드백을 바꿔 다시 시도할 수 있습니다.`;
  } finally {
    proposalDraftPending.value = false;
  }
}

async function runFunction(functionId: FunctionId): Promise<void> {
  selectedFunction.value = functionId;
  functionFeedback.value = "";
  proposalRevisionContext.value = null;

  if (functionId === "proposal_draft") {
    if (proposalDraftPending.value || proposalDescriptionMode.value) return;
    if (!selectedFiles.value.length) {
      conversationStatus.value = "error";
      functionFeedback.value = "왼쪽 검색 결과에서 기안에 참고할 파일을 먼저 선택해 주세요.";
      fileSearchFeedback.value = "먼저 검색 결과에서 기안에 참고할 파일을 선택해 주세요.";
      return;
    }
    proposalType.value = "auto";
    proposalDescriptionMode.value = true;
    conversationStatus.value = "ready";
    functionFeedback.value = "기안 목적과 요청 내용 입력을 기다리고 있습니다.";
    messages.value.push({
      id: createUiId(),
      role: "assistant",
      content: "기안 목적과 요청 내용을 입력해 주세요.",
      selectedFiles: selectedFiles.value.map((file) => file.file_name),
    });
    return;
  }

  proposalDescriptionMode.value = false;

  if (!selectedFiles.value.length) {
    conversationStatus.value = "error";
    functionFeedback.value = "요약할 파일을 먼저 선택해 주세요.";
    fileSearchFeedback.value = "먼저 검색 결과에서 파일을 선택해 주세요.";
    return;
  }
  functionFeedback.value = "선택 문서의 핵심 내용을 요약하고 있습니다.";
  await sendChat("선택한 문서의 핵심 내용을 근거와 함께 항목별로 요약해 줘");
  functionFeedback.value = conversationStatus.value === "success"
    ? "문서 요약을 완료했습니다."
    : "문서 요약 실행 상태를 확인해 주세요.";
}

onMounted(() => Promise.all([loadStoreStatus(), loadSettings()]));
</script>

<template>
  <AppHeader />
  <main class="app-shell">
    <FileSidebar
      :results="fileResults"
      :selected-files="selectedFiles"
      :search-pending="fileSearchPending"
      :search-feedback="fileSearchFeedback"
      :upload-enabled="uploadEnabled"
      :upload-pending="uploadPending"
      :upload-status="uploadStatus"
      :upload-feedback="uploadFeedback"
      :allowed-extensions="allowedExtensions"
      @new-conversation="startNewConversation"
      @search="searchFiles"
      @toggle-file="toggleFile"
      @remove-file="removeFile"
      @preview-file="previewFile"
      @inspect-versions="inspectFileVersions"
      @upload-file="uploadFile"
      @open-settings="openSettings"
    />
    <ChatWorkspace
      :definition="workspaceDefinition"
      :messages="messages"
      :selected-files="selectedFiles"
      :pending="chatPending || proposalDraftPending"
      :conversation-status="conversationStatus"
      :input-mode="proposalRevisionContext ? 'proposal_revision' : proposalDescriptionMode ? 'proposal_description' : 'chat'"
      :proposal-type="proposalType"
      :revision-source-title="proposalRevisionContext?.sourceDraft.fields.title || ''"
      :revision-source-file-count="proposalRevisionContext?.selectedFiles.length || 0"
      @send="sendChat"
      @start-proposal-revision="startProposalRevision"
      @submit-proposal-clarification="submitProposalClarification"
      @cancel-proposal-revision="cancelProposalRevision"
      @update-proposal-type="proposalType = $event"
      @toggle-file="toggleFile"
      @preview-file="previewFile"
      @inspect-versions="inspectFileVersions"
    />
    <FeatureSidebar
      :functions="functions"
      :selected-function="selectedFunction"
      :selected-file-count="selectedFiles.length"
      :function-pending="chatPending || graphPending || proposalDraftPending"
      :conversation-status="conversationStatus"
      :function-feedback="functionFeedback"
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
  <SettingsDialog
    ref="settingsDialog"
    :settings="settings"
    :stores="stores"
    :loading="settingsPending"
    :save-pending="settingsSavePending"
    :feedback="settingsFeedback"
    :feedback-status="settingsFeedbackStatus"
    @save-upload-directory="saveUploadDirectory"
    @save-proposal-draft-directory="saveProposalDraftDirectory"
    @reload="reloadSettings"
  />
</template>
