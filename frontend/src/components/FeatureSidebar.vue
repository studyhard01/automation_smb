<script setup lang="ts">
import { computed } from "vue";

import type { ConversationStatus, FunctionDefinition, FunctionId, StoresStatusResponse } from "@/types";

const props = defineProps<{
  functions: FunctionDefinition[];
  selectedFunction: FunctionId;
  selectedFileCount: number;
  functionPending: boolean;
  conversationStatus: ConversationStatus;
  functionFeedback: string;
  stores: StoresStatusResponse | null;
}>();

const emit = defineEmits<{
  runFunction: [functionId: FunctionId];
}>();

function stateLabel(connected: boolean, degraded: boolean): string {
  if (!connected) return "연결 안 됨";
  return degraded ? "제한됨" : "연결됨";
}

const activeFunction = computed(
  () => props.functions.find((item) => item.id === props.selectedFunction) ?? props.functions[0],
);
const activeFunctionReady = computed(() => Boolean(
  activeFunction.value && (!activeFunction.value.requiresFiles || props.selectedFileCount),
));

const resultTitle = computed(() => {
  if (props.functionFeedback) return props.functionFeedback;
  if (props.functionPending || props.conversationStatus === "loading") return "기능을 실행하고 있어요";
  if (props.conversationStatus === "success") return "기능 실행을 완료했어요";
  if (props.conversationStatus === "error") return "실행 상태를 확인해 주세요";
  return "아직 실행된 기능이 없어요";
});

const resultStatusLabel = computed(() => {
  if (props.conversationStatus === "loading") return "진행 중";
  if (props.conversationStatus === "success") return "완료";
  if (props.conversationStatus === "error") return "확인 필요";
  return "대기";
});
</script>

<template>
  <aside class="functions-panel" aria-label="문서 기능">
    <section class="functions-section">
      <div class="section-heading">
        <strong>기능</strong>
        <small>{{ selectedFileCount ? `${selectedFileCount}개 파일 준비됨` : "왼쪽에서 참고 파일을 선택해 주세요" }}</small>
      </div>
      <div class="function-list">
        <button
          v-for="item in functions"
          :key="item.id"
          class="function-card"
          :class="{ active: selectedFunction === item.id }"
          type="button"
          :disabled="functionPending || (item.requiresFiles && !selectedFileCount)"
          @click="emit('runFunction', item.id)"
        >
          <span class="function-icon" aria-hidden="true">{{ item.icon }}</span>
          <span>
            <strong>{{ item.label }}</strong>
            <small>{{ item.description }}</small>
          </span>
          <span class="function-arrow" aria-hidden="true">›</span>
        </button>
      </div>
    </section>

    <section class="function-guide" :class="{ ready: activeFunctionReady }">
      <strong>{{ activeFunction?.label }}</strong>
      <p>{{ activeFunction?.description }}</p>
      <span v-if="activeFunction?.requiresFiles">
        {{ selectedFileCount ? `● 선택 파일 ${selectedFileCount}개에 적용` : "왼쪽 검색 결과에서 참고 파일을 먼저 선택해 주세요" }}
      </span>
    </section>

    <section class="conversation-output-section">
      <div class="section-heading">
        <strong>실행 결과</strong>
        <small>{{ resultStatusLabel }}</small>
      </div>
      <div class="conversation-output-card" :class="conversationStatus">
        <strong>{{ resultTitle }}</strong>
        <p>{{ activeFunction?.resultDescription }}</p>
      </div>
    </section>

    <details class="store-status-section">
      <summary>
        <span>저장소 연결</span>
        <small>{{ stores?.overall === "ok" ? "정상" : "상태 확인" }}</small>
      </summary>
      <div class="section-heading">
        <div>
          <small>실제 조회 경로 상태</small>
        </div>
      </div>
      <ul v-if="stores" class="store-status-list">
        <li>
          <span>PostgreSQL</span>
          <strong>{{ stateLabel(stores.postgresql.connected, stores.postgresql.degraded) }}</strong>
        </li>
        <li>
          <span>MinIO</span>
          <strong>{{ stateLabel(stores.minio.connected, stores.minio.degraded) }}</strong>
        </li>
        <li>
          <span>Neo4j</span>
          <strong>{{ stateLabel(stores.neo4j.connected, stores.neo4j.degraded) }}</strong>
        </li>
      </ul>
      <p v-else class="tool-list-status">연결 상태를 확인하는 중입니다.</p>
    </details>
  </aside>
</template>
