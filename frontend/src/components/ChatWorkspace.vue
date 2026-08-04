<script setup lang="ts">
import { nextTick, onUpdated, ref } from "vue";

import type {
  ChatUiMessage,
  ConversationDefinition,
  ConversationStatus,
  DocumentSearchHit,
} from "@/types";

const props = defineProps<{
  definition: ConversationDefinition;
  messages: ChatUiMessage[];
  selectedFiles: DocumentSearchHit[];
  pending: boolean;
  conversationStatus: ConversationStatus;
}>();

const emit = defineEmits<{
  send: [message: string];
  toggleFile: [file: DocumentSearchHit];
  previewFile: [file: DocumentSearchHit];
  inspectVersions: [file: DocumentSearchHit];
}>();

const message = ref("");
const stream = ref<HTMLElement | null>(null);

function fileKey(file: DocumentSearchHit): string {
  return `${file.doc_id}:${file.revision_id}`;
}

function isSelected(file: DocumentSearchHit): boolean {
  return props.selectedFiles.some((item) => fileKey(item) === fileKey(file));
}

function submit(): void {
  const value = message.value.trim() || props.definition.placeholder;
  if (!props.pending && value) {
    emit("send", value);
    message.value = "";
  }
}

onUpdated(async () => {
  await nextTick();
  stream.value?.scrollTo({ top: stream.value.scrollHeight, behavior: "smooth" });
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
        {{ pending ? "답변 생성 중" : selectedFiles.length ? `선택 파일 ${selectedFiles.length}개` : "파일 선택 필요" }}
      </span>
    </header>

    <div v-if="selectedFiles.length" class="conversation-file-context" aria-label="현재 선택 문서">
      <strong>대화 범위</strong>
      <button v-for="file in selectedFiles" :key="fileKey(file)" type="button" @click="emit('previewFile', file)">
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
          <section v-if="item.response.citations.length" class="citation-panel">
            <header>
              <strong>답변 근거</strong>
              <span>{{ item.response.citations.length }}개 · 검색 {{ item.response.retrieval?.elapsed_ms.toFixed(1) }}ms</span>
            </header>
            <ol>
              <li v-for="citation in item.response.citations" :key="citation.chunk_id">
                <strong>[{{ citation.index }}] {{ citation.title }}</strong>
                <span>{{ citation.section_path.join(" › ") || "본문" }}</span>
                <p>{{ citation.excerpt }}</p>
              </li>
            </ol>
          </section>
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
          <small>{{ selectedFiles.length ? `${selectedFiles.length}개 문서 안에서 검색` : "파일을 먼저 선택하세요" }}</small>
        </div>
      </div>
      <div class="composer-input-row">
        <textarea
          v-model="message"
          rows="2"
          :placeholder="definition.placeholder"
          :disabled="pending"
          @keydown.ctrl.enter.prevent="submit"
        ></textarea>
        <button type="submit" :disabled="pending || !selectedFiles.length">{{ pending ? "생성 중" : "전송" }}</button>
      </div>
      <p class="composer-note">Ctrl + Enter로 전송 · 선택한 DB 문서 밖의 내용은 답변 근거로 사용하지 않습니다.</p>
    </form>
  </section>
</template>
