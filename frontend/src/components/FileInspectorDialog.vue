<script setup lang="ts">
import { ref, watch } from "vue";

import type { DocumentPreviewResponse, DocumentSearchHit, DocumentVersionGraphResponse } from "@/types";

const props = defineProps<{
  file: DocumentSearchHit | null;
  preview: DocumentPreviewResponse | null;
  graph: DocumentVersionGraphResponse | null;
  previewPending: boolean;
  graphPending: boolean;
  error: string;
}>();

const emit = defineEmits<{ loadGraph: [] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const activeTab = ref<"preview" | "versions">("preview");

function open(): void {
  activeTab.value = "preview";
  dialog.value?.showModal();
}

function openVersions(): void {
  activeTab.value = "versions";
  dialog.value?.showModal();
}

function close(): void {
  dialog.value?.close();
}

function selectTab(tab: "preview" | "versions"): void {
  activeTab.value = tab;
  if (tab === "versions" && !props.graph && !props.graphPending) emit("loadGraph");
}

watch(() => props.file, () => { activeTab.value = "preview"; });
defineExpose({ open, openVersions, close });
</script>

<template>
  <dialog ref="dialog" class="file-inspector-dialog" @cancel="close">
    <div class="file-inspector-shell">
      <header class="file-inspector-header">
        <div>
          <p class="eyebrow">문서 확인</p>
          <h2>{{ file?.title || file?.file_name || "선택 문서" }}</h2>
          <p>{{ file?.file_name }}</p>
        </div>
        <button class="icon-button" type="button" aria-label="문서 확인 닫기" @click="close">×</button>
      </header>

      <div class="file-inspector-tabs" role="tablist" aria-label="문서 보기 방식">
        <button
          id="previewTab"
          type="button"
          role="tab"
          :aria-selected="activeTab === 'preview'"
          :class="{ active: activeTab === 'preview' }"
          @click="selectTab('preview')"
        >내용 미리보기</button>
        <button
          id="versionsTab"
          type="button"
          role="tab"
          :aria-selected="activeTab === 'versions'"
          :class="{ active: activeTab === 'versions' }"
          @click="selectTab('versions')"
        >버전 관계</button>
      </div>

      <section v-if="activeTab === 'preview'" class="file-inspector-body" role="tabpanel">
        <div v-if="previewPending" class="inspector-state loading" role="status">
          <strong>미리보기를 불러오는 중입니다.</strong>
          <span>브라우저에는 원본 Object URI를 전달하지 않습니다.</span>
        </div>
        <div v-else-if="error" class="inspector-state error" role="alert">
          <strong>문서를 표시하지 못했습니다.</strong><span>{{ error }}</span>
        </div>
        <template v-else-if="preview">
          <div class="preview-meta">
            <span>{{ preview.media_type || "문서" }}</span>
            <span>{{ preview.elapsed_ms.toFixed(1) }}ms</span>
            <span>{{ preview.size_bytes.toLocaleString("ko-KR") }} bytes</span>
          </div>
          <pre class="preview-content">{{ preview.content || "미리보기 내용이 없습니다." }}</pre>
        </template>
        <div v-else class="inspector-state"><strong>미리보기가 없습니다.</strong></div>
      </section>

      <section v-else class="file-inspector-body" role="tabpanel">
        <div v-if="graphPending" class="inspector-state loading" role="status"><strong>버전 관계 확인 중입니다.</strong></div>
        <div v-else-if="error" class="inspector-state error" role="alert">
          <strong>버전 관계를 표시하지 못했습니다.</strong><span>{{ error }}</span>
        </div>
        <template v-else-if="graph">
          <div class="preview-meta">
            <span>노드 {{ graph.nodes.length }}개</span>
            <span>관계 {{ graph.edges.length }}개</span>
            <span>{{ graph.elapsed_ms.toFixed(1) }}ms</span>
          </div>
          <div v-if="graph.degraded" class="grounding-callout insufficient" role="status">
            <strong>일부 버전 정보가 제한됐습니다.</strong>
            <span>{{ graph.warnings.join(" · ") }}</span>
          </div>
          <ol class="version-timeline" aria-label="문서 버전 목록">
            <li v-for="version in graph.nodes" :key="version.id" :class="{ active: version.status === 'active' }">
              <span class="version-dot" aria-hidden="true"></span>
              <div>
                <strong>{{ version.label || "문서 버전" }}</strong>
                <span>{{ version.status === "active" ? "현재 활성 버전" : version.status || "이전 버전" }}</span>
                <small>{{ version.type }}</small>
              </div>
            </li>
          </ol>
        </template>
        <div v-else class="inspector-state"><strong>버전 관계가 없습니다.</strong></div>
      </section>
    </div>
  </dialog>
</template>
