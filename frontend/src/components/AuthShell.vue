<script setup lang="ts">
defineProps<{
  eyebrow: string;
  title: string;
  description: string;
}>();
</script>

<template>
  <main class="auth-page">
    <section class="auth-hero" aria-labelledby="auth-product-title">
      <a class="auth-brand" href="/playground/" aria-label="SMB 자동화 Playground로 이동">
        <span class="brand-mark" aria-hidden="true">S</span>
        <span class="brand-copy">
          <strong id="auth-product-title">SMB Automation</strong>
          <small>공유 문서 업무 Playground</small>
        </span>
      </a>
      <div class="auth-hero-copy">
        <span class="auth-eyebrow">ON-PREMISE WORKSPACE</span>
        <h1>필요한 문서를 빠르게 찾고,<br>안전하게 업무를 이어가세요.</h1>
        <p>계정과 서비스 접근 권한은 조직 내부 정책에 따라 관리됩니다.</p>
      </div>
      <div class="auth-safety-note">
        <span class="status-dot" aria-hidden="true"></span>
        <span>사내 계정 전용 · 로그인 정보는 브라우저에 저장하지 않습니다.</span>
      </div>
    </section>

    <section class="auth-form-panel">
      <div class="auth-card">
        <header class="auth-card-header">
          <span>{{ eyebrow }}</span>
          <h2>{{ title }}</h2>
          <p>{{ description }}</p>
        </header>
        <slot />
      </div>
    </section>
  </main>
</template>
