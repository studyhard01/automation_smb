<script setup lang="ts">
import { computed, ref } from "vue";

import type { DocumentSearchHit, UploadStatus } from "@/types";

const props = defineProps<{
  results: DocumentSearchHit[];
  selectedFiles: DocumentSearchHit[];
  searchPending: boolean;
  searchFeedback: string;
  uploadEnabled: boolean;
  uploadPending: boolean;
  uploadStatus: UploadStatus;
  uploadFeedback: string;
  allowedExtensions: string[];
}>();

const emit = defineEmits<{
  newConversation: [];
  search: [query: string];
  toggleFile: [file: DocumentSearchHit];
  removeFile: [file: DocumentSearchHit];
  previewFile: [file: DocumentSearchHit];
  inspectVersions: [file: DocumentSearchHit];
  uploadFile: [file: File];
  openSettings: [];
}>();

const query = ref("");
const fileInput = ref<HTMLInputElement | null>(null);
const selectedCount = computed(() => props.selectedFiles.length);
const uploadAccept = computed(() => props.allowedExtensions.map((extension) => (
  extension.startsWith(".") ? extension : `.${extension}`
)).join(","));

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

function matchedStoreLabel(file: DocumentSearchHit): string {
  const labels = { postgresql: "PostgreSQL", minio: "MinIO", neo4j: "Neo4j" } as const;
  return (file.matched_stores || ["postgresql"]).map((store) => labels[store]).join(" + ");
}

function openFilePicker(): void {
  if (!props.uploadEnabled || props.uploadPending) return;
  fileInput.value?.click();
}

function selectUpload(event: Event): void {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (file) emit("uploadFile", file);
  input.value = "";
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
      <div class="source-upload">
        <button
          class="file-upload-button"
          type="button"
          :disabled="!uploadEnabled || uploadPending"
          :aria-describedby="uploadFeedback ? 'uploadFeedback' : undefined"
          @click="openFilePicker"
        >
          <span class="file-upload-icon" aria-hidden="true">↑</span>
          <span>
            <strong>{{ uploadPending ? "업로드 중" : "파일 첨부" }}</strong>
            <small>{{ uploadEnabled ? "설정된 공유폴더에 한 파일 저장" : "설정에서 업로드 경로를 확인하세요" }}</small>
          </span>
        </button>
        <input
          ref="fileInput"
          class="visually-hidden"
          type="file"
          :accept="uploadAccept || undefined"
          :disabled="!uploadEnabled || uploadPending"
          @change="selectUpload"
        />
        <p
          v-if="uploadFeedback"
          id="uploadFeedback"
          class="upload-feedback"
          :class="uploadStatus"
          :role="uploadStatus === 'error' ? 'alert' : 'status'"
        >{{ uploadFeedback }}</p>
      </div>
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
                {{ file.match_source === "content" ? "내용 일치" : "메타데이터 일치" }} ·
                {{ matchedStoreLabel(file) }} · {{ formatSize(file.size_bytes) }}
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
          <button
            type="button"
            :disabled="file.source === 'upload'"
            :aria-label="file.source === 'upload' ? `${file.file_name} 첨부 파일` : `${file.file_name} 미리보기`"
            @click="emit('previewFile', file)"
          >
            <strong>{{ file.file_name }}</strong>
            <small>{{ file.source === "upload" ? "방금 첨부 · 대화에 반영" : file.title || "선택 문서" }}</small>
          </button>
          <button type="button" :aria-label="`${file.file_name} 선택 해제`" @click="emit('removeFile', file)">×</button>
        </article>
      </div>
    </section>

    <button class="settings-button" type="button" @click="emit('openSettings')">
      <span aria-hidden="true">⚙</span>
      <span>설정</span>
    </button>
  </aside>
</template>
