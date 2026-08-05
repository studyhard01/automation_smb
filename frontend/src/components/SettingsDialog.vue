<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type {
  PlaygroundSettingsResponse,
  StoreConnectionState,
  StoresStatusResponse,
  UploadStatus,
} from "@/types";

const props = defineProps<{
  settings: PlaygroundSettingsResponse | null;
  stores: StoresStatusResponse | null;
  loading: boolean;
  savePending: boolean;
  feedback: string;
  feedbackStatus: UploadStatus;
}>();

const emit = defineEmits<{
  saveUploadDirectory: [relativeDirectory: string];
  reload: [];
}>();

const dialog = ref<HTMLDialogElement | null>(null);
const relativeDirectory = ref("");

watch(
  () => props.settings?.upload.relative_directory,
  (value) => { relativeDirectory.value = value || ""; },
  { immediate: true },
);

const relativeDirectoryError = computed(() => {
  const value = relativeDirectory.value.trim();
  if (!value) return "공유폴더 안의 상대 경로를 입력해 주세요.";
  if (/^(?:[a-zA-Z]:[\\/]|[\\/])/.test(value) || value.split(/[\\/]+/).includes("..")) {
    return "드라이브·루트·상위 폴더가 아닌 상대 경로만 사용할 수 있습니다.";
  }
  return "";
});

const canSave = computed(() => Boolean(
  props.settings
  && !props.savePending
  && !relativeDirectoryError.value
  && relativeDirectory.value.trim() !== props.settings.upload.relative_directory,
));

function open(): void {
  relativeDirectory.value = props.settings?.upload.relative_directory || "";
  dialog.value?.showModal();
}

function close(): void {
  dialog.value?.close();
}

function save(): void {
  if (canSave.value) emit("saveUploadDirectory", relativeDirectory.value.trim());
}

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(value % (1024 * 1024) ? 1 : 0)} MB`;
}

function storeLabel(store: StoreConnectionState | undefined): string {
  if (!store) return "확인 중";
  if (!store.connected) return "연결 안 됨";
  return store.degraded ? "제한됨" : "연결됨";
}

function storeClass(store: StoreConnectionState | undefined): string {
  if (!store) return "pending";
  if (!store.connected) return "error";
  return store.degraded ? "warning" : "success";
}

defineExpose({ open, close });
</script>

<template>
  <dialog ref="dialog" class="settings-dialog" @cancel="close">
    <div class="settings-shell">
      <header class="settings-header">
        <div>
          <p class="eyebrow">Playground 설정</p>
          <h2>파일 첨부와 연결 상태</h2>
          <p>비밀번호나 서버 주소는 이 화면에서 표시하거나 변경하지 않습니다.</p>
        </div>
        <button class="icon-button" type="button" aria-label="설정 닫기" @click="close">×</button>
      </header>

      <div v-if="loading && !settings" class="settings-state" role="status">
        <strong>설정을 확인하고 있습니다.</strong>
      </div>

      <div v-else-if="!settings" class="settings-state error" role="alert">
        <strong>설정을 불러오지 못했습니다.</strong>
        <span>{{ feedback || "잠시 후 다시 시도해 주세요." }}</span>
        <button type="button" @click="emit('reload')">다시 확인</button>
      </div>

      <template v-else>
        <div class="settings-body">
          <section class="settings-section" aria-labelledby="uploadSettingsTitle">
            <header>
              <div>
                <h3 id="uploadSettingsTitle">파일 첨부</h3>
                <p>공유폴더 기준 상대 경로만 변경할 수 있습니다.</p>
              </div>
              <span class="settings-status-pill" :class="settings.upload.enabled && settings.upload.configured ? 'success' : 'error'">
                {{ settings.upload.enabled && settings.upload.configured ? "사용 가능" : "설정 필요" }}
              </span>
            </header>

            <form class="upload-settings-form" @submit.prevent="save">
              <label for="uploadRelativeDirectory">업로드 상대 경로</label>
              <div class="settings-input-row">
                <input
                  id="uploadRelativeDirectory"
                  v-model="relativeDirectory"
                  type="text"
                  maxlength="240"
                  autocomplete="off"
                  spellcheck="false"
                  placeholder="예: playground/uploads"
                  :aria-invalid="Boolean(relativeDirectoryError)"
                  aria-describedby="uploadRelativeDirectoryHelp"
                />
                <button type="submit" :disabled="!canSave">{{ savePending ? "저장 중" : "저장" }}</button>
              </div>
              <p id="uploadRelativeDirectoryHelp" :class="{ error: relativeDirectoryError }">
                {{ relativeDirectoryError || `저장 위치: ${settings.upload.destination_label}` }}
              </p>
            </form>

            <dl class="upload-policy-list">
              <div>
                <dt>허용 확장자</dt>
                <dd>
                  <span v-for="extension in settings.upload.allowed_extensions" :key="extension">{{ extension }}</span>
                </dd>
              </div>
              <div>
                <dt>파일당 최대 크기</dt>
                <dd>{{ formatBytes(settings.upload.max_size_bytes) }}</dd>
              </div>
            </dl>

            <p v-if="feedback" class="settings-feedback" :class="feedbackStatus" :role="feedbackStatus === 'error' ? 'alert' : 'status'">
              {{ feedback }}
            </p>
          </section>

          <section class="settings-section" aria-labelledby="connectionSettingsTitle">
            <header>
              <div>
                <h3 id="connectionSettingsTitle">연결 상태</h3>
                <p>접속 정보 없이 현재 사용 가능 여부만 표시합니다.</p>
              </div>
              <button class="settings-reload-button" type="button" :disabled="loading" @click="emit('reload')">
                {{ loading ? "확인 중" : "새로고침" }}
              </button>
            </header>
            <ul class="settings-connection-list">
              <li>
                <span>PostgreSQL</span>
                <strong :class="storeClass(stores?.postgresql)">{{ storeLabel(stores?.postgresql) }}</strong>
              </li>
              <li>
                <span>MinIO</span>
                <strong :class="storeClass(stores?.minio)">{{ storeLabel(stores?.minio) }}</strong>
              </li>
              <li>
                <span>Neo4j</span>
                <strong :class="storeClass(stores?.neo4j)">{{ storeLabel(stores?.neo4j) }}</strong>
              </li>
              <li>
                <span>로컬 LLM</span>
                <strong :class="settings.local_llm_configured ? 'success' : 'error'">
                  {{ settings.local_llm_configured ? "설정됨" : "설정 필요" }}
                </strong>
              </li>
            </ul>
          </section>
        </div>

        <footer class="settings-footer">
          <span>변경한 상대 경로는 다음 파일 첨부부터 적용됩니다.</span>
          <button type="button" @click="close">닫기</button>
        </footer>
      </template>
    </div>
  </dialog>
</template>
