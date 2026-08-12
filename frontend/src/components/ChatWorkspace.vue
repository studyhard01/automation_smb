<script setup lang="ts">
import { computed, nextTick, onUpdated, ref } from "vue";

import {
  PROPOSAL_CLARIFICATION_ANSWER_MAX_LENGTH,
  PROPOSAL_TEXT_MAX_LENGTH,
} from "@/types";
import type {
  ChatUiMessage,
  ConversationDefinition,
  ConversationStatus,
  DocumentSearchHit,
  ProposalClarificationAnswer,
  ProposalTypeRequest,
} from "@/types";

const props = defineProps<{
  definition: ConversationDefinition;
  messages: ChatUiMessage[];
  selectedFiles: DocumentSearchHit[];
  pending: boolean;
  conversationStatus: ConversationStatus;
  inputMode?: "chat" | "proposal_description" | "proposal_revision";
  proposalType?: ProposalTypeRequest;
  revisionSourceTitle?: string;
  revisionSourceFileCount?: number;
}>();

const emit = defineEmits<{
  send: [message: string];
  toggleFile: [file: DocumentSearchHit];
  previewFile: [file: DocumentSearchHit];
  inspectVersions: [file: DocumentSearchHit];
  updateProposalType: [proposalType: ProposalTypeRequest];
  startProposalRevision: [messageId: string];
  submitProposalClarification: [messageId: string, answers: ProposalClarificationAnswer[]];
  cancelProposalRevision: [];
}>();

const message = ref("");
const stream = ref<HTMLElement | null>(null);
const clarificationAnswers = ref<Record<string, string>>({});
const canSubmit = computed(() => (
  props.inputMode === "proposal_revision"
    ? Boolean(props.revisionSourceFileCount)
    : Boolean(props.selectedFiles.length)
));
const proposalComposer = computed(() => (
  props.inputMode === "proposal_description" || props.inputMode === "proposal_revision"
));

function fileKey(file: DocumentSearchHit): string {
  return `${file.doc_id}:${file.revision_id}`;
}

function isSelected(file: DocumentSearchHit): boolean {
  return props.selectedFiles.some((item) => fileKey(item) === fileKey(file));
}

function submit(): void {
  const value = message.value.trim() || (
    props.inputMode === "proposal_description" || props.inputMode === "proposal_revision"
      ? ""
      : props.definition.placeholder
  );
  if (!props.pending && value && (!proposalComposer.value || value.length <= PROPOSAL_TEXT_MAX_LENGTH)) {
    emit("send", value);
    message.value = "";
  }
}

function handleComposerKeydown(event: KeyboardEvent): void {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  submit();
}

function updateProposalType(event: Event): void {
  emit("updateProposalType", (event.target as HTMLSelectElement).value as ProposalTypeRequest);
}

function proposalTypeLabel(value: "purchase" | "event_attendance" | "general"): string {
  return {
    purchase: "구매",
    event_attendance: "박람회·행사 참석",
    general: "일반",
  }[value];
}

function proposalTypeSourceLabel(value: "user" | "rule" | "fallback"): string {
  return { user: "사용자 선택", rule: "자동 분류", fallback: "기본 분류" }[value];
}

function proposalRevisionLabel(revisionNumber?: string): string {
  return revisionNumber ? `수정본 ${revisionNumber}` : "원본 초안";
}

function clarificationKey(messageId: string, questionId: string): string {
  return `${messageId}:${questionId}`;
}

function submitClarification(item: ChatUiMessage): void {
  if (item.proposalClarificationCompleted) return;
  const questions = item.proposalDraft?.completion?.questions ?? [];
  const answers = questions.map((question) => ({
    question_id: question.question_id,
    answer: clarificationAnswers.value[clarificationKey(item.id, question.question_id)]?.trim() ?? "",
  }));
  if (
    !props.pending
    && answers.length
    && answers.every((answer) => (
      answer.answer && answer.answer.length <= PROPOSAL_CLARIFICATION_ANSWER_MAX_LENGTH
    ))
  ) {
    emit("submitProposalClarification", item.id, answers);
  }
}

function clarificationReady(item: ChatUiMessage): boolean {
  if (item.proposalClarificationCompleted) return false;
  const questions = item.proposalDraft?.completion?.questions ?? [];
  return Boolean(questions.length) && questions.every((question) => (
    clarificationAnswers.value[clarificationKey(item.id, question.question_id)]?.trim().length > 0
    && clarificationAnswers.value[clarificationKey(item.id, question.question_id)]!.trim().length
      <= PROPOSAL_CLARIFICATION_ANSWER_MAX_LENGTH
  ));
}

onUpdated(async () => {
  await nextTick();
  if (typeof stream.value?.scrollTo === "function") {
    stream.value.scrollTo({ top: stream.value.scrollHeight, behavior: "smooth" });
  }
});
</script>

<template>
  <section class="conversation-panel">
    <header class="conversation-header">
      <div>
        <p class="eyebrow">선택 문서 대화</p>
        <h1>{{ definition.title }}</h1>
        <p>{{ definition.subtitle }}</p>
      </div>
      <span class="conversation-state" :class="conversationStatus">
        {{ pending
          ? "답변 생성 중"
          : inputMode === "proposal_revision"
            ? "수정 피드백 입력 대기"
            : inputMode === "proposal_description"
              ? "기안 설명 입력 대기"
              : selectedFiles.length
                ? `선택 파일 ${selectedFiles.length}개`
                : "파일 선택 필요" }}
      </span>
    </header>

    <div v-if="selectedFiles.length" class="conversation-file-context" aria-label="현재 선택 문서">
      <strong>대화 범위</strong>
      <button
        v-for="file in selectedFiles"
        :key="fileKey(file)"
        type="button"
        :disabled="file.source === 'upload'"
        :title="file.source === 'upload' ? '방금 첨부한 파일은 대화 근거로 바로 사용됩니다.' : '미리보기'"
        @click="emit('previewFile', file)"
      >
        {{ file.file_name }}
      </button>
    </div>

    <div ref="stream" class="message-stream" aria-live="polite">
      <div v-if="!messages.length" class="conversation-empty-state">
        <div class="empty-state-icon" aria-hidden="true">⌕</div>
        <strong>왼쪽에서 문서를 찾아 선택해 주세요.</strong>
        <p>선택한 파일의 Chunk만 검색해 답변하고, 사용한 근거를 함께 표시합니다.</p>
      </div>

      <article v-for="item in messages" :key="item.id" class="message" :class="[item.role, { error: item.error }]">
        <div class="message-bubble">
          <p>{{ item.content }}</p>
          <small v-if="item.selectedFiles?.length">선택 문서: {{ item.selectedFiles.join(", ") }}</small>
        </div>

        <section v-if="item.proposalDraft" class="proposal-draft-result" aria-label="기안 생성 결과">
          <span class="proposal-draft-icon" aria-hidden="true">
            {{ item.proposalClarificationCompleted
              ? "완료"
              : item.proposalDraft.completion.status === "needs_clarification" ? "확인" : "XLSX" }}
          </span>
          <span class="proposal-draft-file">
            <span class="proposal-draft-heading">
              <strong>{{ item.proposalDraft.file_name || "기안 완성을 위한 필수 확인" }}</strong>
              <em>
                {{ item.proposalClarificationCompleted
                  ? "확인 완료"
                  : item.proposalDraft.completion.status === "needs_clarification"
                  ? `질문 ${item.proposalDraft.completion.questions.length}개`
                  : proposalRevisionLabel(item.proposalDraft.revision_number) }}
              </em>
            </span>
            <small v-if="item.proposalDraft.saved_to_smb">
              {{ item.proposalDraft.destination_label }} 저장 완료 · {{ item.proposalDraft.elapsed_ms.toFixed(1) }}ms
            </small>
            <small v-else>필수 답변 전에는 엑셀을 만들거나 공유폴더에 저장하지 않습니다.</small>
            <small v-if="item.proposalDraft.proposal_type && item.proposalDraft.proposal_type_source">
              기안 유형: {{ proposalTypeLabel(item.proposalDraft.proposal_type) }} ·
              {{ proposalTypeSourceLabel(item.proposalDraft.proposal_type_source) }}
            </small>
            <small v-if="item.proposalDraft.evidence_filter" class="proposal-evidence-summary">
              첨부 근거 검토: 전체 {{ item.proposalDraft.evidence_filter.input_document_count }}개 ·
              반영 {{ item.proposalDraft.evidence_filter.included_document_count }}개 ·
              제외 {{ item.proposalDraft.evidence_filter.excluded_document_count }}개
            </small>
            <small v-if="item.proposalDraft.revision_summary" class="proposal-revision-summary">
              수정 요약: {{ item.proposalDraft.revision_summary }}
            </small>
            <small
              v-if="item.proposalDraft.completion && item.proposalDraft.completion.omitted_claim_count"
              class="proposal-evidence-summary"
            >
              근거가 부족한 선택 내용 {{ item.proposalDraft.completion.omitted_claim_count }}개는 본문에서 제외했습니다.
            </small>
          </span>
          <div
            v-if="item.proposalDraft.completion?.status === 'needs_clarification'"
            class="proposal-clarification-questions"
          >
            <p v-if="item.proposalClarificationCompleted">답변을 반영해 최종 기안을 완성했습니다.</p>
            <p v-else>기안에 꼭 필요한 정보만 묻습니다. 아래 질문에 한 번에 답해 주세요. 각 답변은 최대 500자입니다.</p>
            <label
              v-for="question in item.proposalDraft.completion.questions"
              :key="question.question_id"
            >
              <span>{{ question.prompt }}</span>
              <textarea
                v-model="clarificationAnswers[clarificationKey(item.id, question.question_id)]"
                rows="2"
                :maxlength="PROPOSAL_CLARIFICATION_ANSWER_MAX_LENGTH"
                :disabled="pending || item.proposalClarificationCompleted"
                :aria-label="question.prompt"
                placeholder="답변을 입력하세요"
              ></textarea>
            </label>
            <button
              type="button"
              :disabled="pending || item.proposalClarificationCompleted || !clarificationReady(item)"
              @click="submitClarification(item)"
            >{{ item.proposalClarificationCompleted ? "답변 반영 완료" : "답변 반영해 기안 완성" }}</button>
          </div>
          <span v-if="item.proposalDraft.download_url" class="proposal-draft-actions">
            <a
              :href="item.proposalDraft.download_url"
              :download="item.proposalDraft.file_name || undefined"
              :aria-label="`${item.proposalDraft.file_name} 다운로드`"
            >엑셀 다운로드</a>
            <button
              type="button"
              :disabled="pending || !item.proposalDraftSelectedFiles?.length"
              :aria-label="`${item.proposalDraft.file_name} 초안 수정`"
              @click="emit('startProposalRevision', item.id)"
            >초안 수정</button>
          </span>
        </section>

        <section v-if="item.searchResponse" class="conversation-search-results">
          <header class="conversation-result-header">
            <span class="conversation-result-icon" aria-hidden="true">⌕</span>
            <span>
              <strong>파일 검색 결과</strong>
              <small>{{ item.searchResponse.result_count }}건 · {{ item.searchResponse.elapsed_ms.toFixed(1) }}ms</small>
            </span>
          </header>
          <div class="conversation-result-grid">
            <article v-for="file in item.searchResponse.hits" :key="fileKey(file)" class="conversation-file-result">
              <div>
                <strong>{{ file.file_name }}</strong>
                <span>{{ file.title || file.file_name }}</span>
              </div>
              <div class="conversation-file-result-actions">
                <button
                  type="button"
                  :class="{ selected: isSelected(file) }"
                  :aria-label="`${file.file_name} 선택한 파일에 추가`"
                  @click="emit('toggleFile', file)"
                >
                  {{ isSelected(file) ? "선택됨" : "+ 선택" }}
                </button>
                <button
                  class="secondary"
                  type="button"
                  :aria-label="`${file.file_name} 버전 확인`"
                  @click="emit('inspectVersions', file)"
                >버전 확인</button>
              </div>
            </article>
          </div>
        </section>

        <template v-if="item.response">
          <details v-if="item.response.citations.length" class="citation-panel">
            <summary>
              <span>
                <strong>답변 근거</strong>
                <small>{{ item.response.citations.length }}개 · 검색 {{ item.response.retrieval?.elapsed_ms.toFixed(1) }}ms</small>
              </span>
              <span class="citation-toggle-label">펼쳐보기</span>
            </summary>
            <ol>
              <li v-for="citation in item.response.citations" :key="citation.chunk_id">
                <strong>[{{ citation.index }}] {{ citation.title }}</strong>
                <span>{{ citation.section_path.join(" › ") || "본문" }}</span>
                <p>{{ citation.excerpt }}</p>
              </li>
            </ol>
          </details>
          <footer class="message-meta">
            <span>{{ item.response.model_used || "local LLM" }}</span>
            <span>{{ item.response.elapsed_ms.toFixed(1) }}ms</span>
            <span v-if="item.response.over_budget">지연 예산 초과</span>
          </footer>
        </template>
      </article>
    </div>

    <form class="composer" @submit.prevent="submit">
      <div class="composer-context">
        <div class="composer-context-primary">
          <span id="functionContextLabel">{{ definition.label }}</span>
          <small v-if="inputMode === 'proposal_revision'">
            {{ revisionSourceFileCount ? `원본 초안의 참고 문서 ${revisionSourceFileCount}개를 그대로 사용` : "수정할 초안을 다시 선택하세요" }}
          </small>
          <small v-else>{{ selectedFiles.length ? `${selectedFiles.length}개 문서 안에서 검색` : "파일을 먼저 선택하세요" }}</small>
        </div>
        <button
          v-if="inputMode === 'proposal_revision'"
          class="composer-cancel-button"
          type="button"
          :disabled="pending"
          @click="emit('cancelProposalRevision')"
        >수정 취소</button>
        <label v-if="inputMode === 'proposal_description'" class="proposal-type-select">
          <span>기안 유형</span>
          <select
            :value="proposalType || 'auto'"
            :disabled="pending"
            aria-label="기안 유형"
            @change="updateProposalType"
          >
            <option value="auto">자동 분류</option>
            <option value="purchase">구매</option>
            <option value="event_attendance">박람회·행사 참석</option>
            <option value="general">일반</option>
          </select>
        </label>
      </div>
      <div class="composer-input-row">
        <textarea
          v-model="message"
          rows="2"
          :maxlength="proposalComposer ? PROPOSAL_TEXT_MAX_LENGTH : undefined"
          :placeholder="definition.placeholder"
          :disabled="pending"
          @keydown="handleComposerKeydown"
        ></textarea>
        <button type="submit" :disabled="pending || !canSubmit">
          {{ pending ? "생성 중" : inputMode === "proposal_revision" ? "수정본 생성" : inputMode === "proposal_description" ? "기안 생성" : "전송" }}
        </button>
      </div>
      <p class="composer-note">
        {{ inputMode === "proposal_revision"
          ? `원본 엑셀은 유지됩니다. ${revisionSourceTitle || "선택한 초안"}을 기준으로 별도의 수정본을 만듭니다. 수정 피드백은 최대 2,000자입니다.`
          : inputMode === "proposal_description"
            ? "기안 목적과 요청 내용을 입력하면 선택 문서를 참고해 엑셀 초안을 생성합니다. 기안 설명은 최대 2,000자입니다."
            : "Enter로 전송 · Shift + Enter로 줄바꿈 · 선택한 문서만 답변 근거로 사용합니다." }}
      </p>
    </form>
  </section>
</template>
