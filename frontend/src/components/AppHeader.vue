<script setup lang="ts">
import type { WorkspaceStatus } from "@/types";

defineProps<{
  workspaceStatus: WorkspaceStatus;
  workspaceTitle: string;
}>();
</script>

<template>
  <header class="app-header">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">S</div>
      <div class="brand-copy">
        <strong>공유폴더 도우미</strong>
        <small>파일과 문서를 빠르게 찾아드려요</small>
      </div>
    </div>

    <nav class="app-mode-nav" aria-label="현재 작업 영역">
      <span class="active">검색</span>
      <span>문서 대화</span>
    </nav>

    <div class="header-spacer"></div>

    <div class="header-controls">
      <span class="environment-pill">테스트 화면</span>
      <div class="conversation-state" :class="workspaceStatus" role="status">
        <span class="status-dot" aria-hidden="true"></span>
        {{ workspaceTitle }}
      </div>
    </div>
  </header>
</template>
