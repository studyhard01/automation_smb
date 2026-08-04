<script setup lang="ts">
import { computed } from "vue";

import type { ConversationStatus, FunctionDefinition, FunctionId, StoresStatusResponse } from "@/types";

const props = defineProps<{
  functions: FunctionDefinition[];
  selectedFunction: FunctionId;
  selectedFileCount: number;
  functionPending: boolean;
  conversationStatus: ConversationStatus;
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

const resultTitle = computed(() => {
  if (props.functionPending || props.conversationStatus === "loading") return "선택 문서에서 근거를 찾고 있어요";
  if (props.conversationStatus === "success") return "실행 결과가 대화에 표시됐어요";
  if (props.conversationStatus === "error") return "실행 상태를 확인해 주세요";
  return "아직 실행된 기능이 없어요";
});
</script>

<template>
  <aside class="functions-panel" aria-label="문서 기능">
    <section class="functions-section">
      <div class="section-heading">
        <strong>기능</strong>
        <small>{{ selectedFileCount ? `${selectedFileCount}개 파일 준비됨` : "파일 선택 필요" }}</small>
      </div>
      <div class="function-list">
        <button
          v-for="item in functions"
          :key="item.id"
          class="function-card"
          :class="{ active: selectedFunction === item.id }"
          type="button"
          :disabled="functionPending || !selectedFileCount"
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

    <section class="function-guide" :class="{ ready: selectedFileCount }">
      <strong>{{ activeFunction?.label }}</strong>
      <p>{{ activeFunction?.description }}</p>
      <span>{{ selectedFileCount ? `● 선택 파일 ${selectedFileCount}개에 적용` : "파일을 선택하면 바로 실행할 수 있어요" }}</span>
    </section>

    <section class="conversation-output-section">
      <div class="section-heading">
        <strong>실행 결과</strong>
        <small>{{ conversationStatus === "success" ? "완료" : "대기" }}</small>
      </div>
      <div class="conversation-output-card" :class="conversationStatus">
        <strong>{{ resultTitle }}</strong>
        <p>답변과 근거 문서는 중앙 대화 영역에서 확인할 수 있습니다.</p>
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
