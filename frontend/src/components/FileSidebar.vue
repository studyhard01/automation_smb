<script setup lang="ts">
import { computed, ref } from "vue";

import type { DocumentSearchHit, WorkspaceStatus } from "@/types";

const props = defineProps<{
  workspaceStatus: WorkspaceStatus;
  workspaceTitle: string;
  workspaceDetail: string;
  results: DocumentSearchHit[];
  selectedFiles: DocumentSearchHit[];
  searchPending: boolean;
  searchFeedback: string;
}>();

const emit = defineEmits<{
  newConversation: [];
  search: [query: string];
  toggleFile: [file: DocumentSearchHit];
  removeFile: [file: DocumentSearchHit];
  previewFile: [file: DocumentSearchHit];
  inspectVersions: [file: DocumentSearchHit];
}>();

const query = ref("");
const selectedCount = computed(() => props.selectedFiles.length);

function key(file: DocumentSearchHit): string {
  return `${file.doc_id}:${file.revision_id}`;
}

function isSelected(file: DocumentSearchHit): boolean {
  return props.selectedFiles.some((item) => key(item) === key(file));
}

function submit(): void {
  const value = query.value.trim();
  if (value && !props.searchPending) emit("search", value);
}

function formatSize(value: number | null): string {
  if (!value) return "크기 미상";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
</script>

<template>
  <aside class="source-panel" aria-label="파일 찾기">
    <header class="source-panel-header">
      <h2>파일 찾기</h2>
      <button class="new-chat-button" type="button" @click="emit('newConversation')">+ 새 대화</button>
    </header>

    <div class="source-tabs" aria-label="파일 탐색 영역">
      <span class="active">파일</span>
    </div>

    <section class="source-section">
      <form class="source-search" @submit.prevent="submit">
        <span class="source-search-icon" aria-hidden="true">⌕</span>
        <input
          id="sourceSearchInput"
          v-model="query"
          type="search"
          placeholder="예: 진검파트 WBS 찾아줘"
          autocomplete="off"
        />
        <button type="submit" :disabled="searchPending || !query.trim()">
          {{ searchPending ? "검색 중" : "검색" }}
        </button>
      </form>
      <p class="search-feedback" aria-live="polite">{{ searchFeedback }}</p>

      <div class="file-search-results-section" :hidden="!results.length">
        <div class="file-search-results" role="listbox" aria-label="파일 검색 결과" aria-multiselectable="true">
          <article
            v-for="file in results"
            :key="key(file)"
            class="file-search-result"
            :class="{ selected: isSelected(file) }"
          >
            <button
              class="file-search-result-select"
              type="button"
              role="option"
              :aria-selected="isSelected(file)"
              @click="emit('toggleFile', file)"
            >
              <span class="file-search-result-heading">
                <strong>{{ file.file_name }}</strong>
                <span class="file-search-result-check" aria-hidden="true">{{ isSelected(file) ? "✓" : "+" }}</span>
              </span>
              <span class="file-search-result-title">{{ file.title || file.file_name }}</span>
              <span class="file-search-result-meta">
                {{ file.match_source === "content" ? "내용 일치" : "메타데이터 일치" }} · {{ formatSize(file.size_bytes) }}
              </span>
            </button>
            <div class="file-search-result-actions">
              <button
                class="file-version-button"
                type="button"
                :aria-label="`${file.file_name} 버전 확인`"
                @click="emit('inspectVersions', file)"
              >버전 확인</button>
            </div>
          </article>
        </div>
      </div>
    </section>

    <section class="source-section selected-source-section">
      <div class="section-heading">
        <div>
          <strong>대화 참고 파일</strong>
          <small>{{ selectedCount }}개 · 대화 범위로 사용</small>
        </div>
        <span v-if="selectedFiles.length" class="section-action-hint">−로 제외</span>
      </div>
      <div class="active-attachment-chips" aria-live="polite">
        <div v-if="!selectedFiles.length" class="source-empty-state">
          검색 결과에서 파일을 선택해 주세요.
        </div>
        <article v-for="file in selectedFiles" :key="key(file)" class="active-attachment-chip selected-document">
          <button type="button" :aria-label="`${file.file_name} 미리보기`" @click="emit('previewFile', file)">
            <strong>{{ file.file_name }}</strong>
            <small>{{ file.title || "선택 문서" }}</small>
          </button>
          <button type="button" :aria-label="`${file.file_name} 선택 해제`" @click="emit('removeFile', file)">×</button>
        </article>
      </div>
    </section>

    <section class="workspace-card" :class="workspaceStatus" aria-live="polite">
      <strong><span class="status-dot" aria-hidden="true"></span>{{ workspaceTitle }}</strong>
      <span>{{ workspaceDetail }}</span>
    </section>

    <p class="source-panel-note">검색 결과는 현재 연결된 문서 DB에서 조회합니다.</p>
  </aside>
</template>
