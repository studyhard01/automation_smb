<script setup lang="ts">
import { ref } from "vue";

import { ApiError } from "@/api/client";
import { authApi } from "@/api/auth";
import AuthShell from "@/components/AuthShell.vue";

const props = defineProps<{
  redirectToPlayground?: () => void;
}>();

const username = ref("");
const password = ref("");
const pending = ref(false);
const error = ref("");

function loginErrorMessage(cause: unknown): string {
  if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
    return "아이디 또는 비밀번호를 확인해 주세요.";
  }
  if (cause instanceof ApiError && cause.status === 429) {
    return "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.";
  }
  return "로그인 서비스에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

async function submit(): Promise<void> {
  if (pending.value) return;
  error.value = "";
  pending.value = true;
  try {
    await authApi.login({ username: username.value.trim(), password: password.value });
    if (props.redirectToPlayground) props.redirectToPlayground();
    else window.location.assign("/playground/");
  } catch (cause) {
    error.value = loginErrorMessage(cause);
  } finally {
    pending.value = false;
  }
}
</script>

<template>
  <AuthShell eyebrow="WELCOME BACK" title="로그인" description="승인된 계정으로 업무 공간에 접속하세요.">
    <form class="auth-form" @submit.prevent="submit">
      <div class="form-field">
        <label for="login-username">아이디</label>
        <input
          id="login-username"
          v-model="username"
          name="username"
          type="text"
          autocomplete="username"
          autocapitalize="none"
          spellcheck="false"
          required
          autofocus
          placeholder="아이디를 입력하세요"
        >
      </div>
      <div class="form-field">
        <div class="form-label-row">
          <label for="login-password">비밀번호</label>
          <span>대소문자를 구분합니다</span>
        </div>
        <input
          id="login-password"
          v-model="password"
          name="password"
          type="password"
          autocomplete="current-password"
          required
          placeholder="비밀번호를 입력하세요"
        >
      </div>

      <p v-if="error" class="form-feedback error" role="alert">{{ error }}</p>

      <button class="primary-button auth-submit" type="submit" :disabled="pending">
        <span v-if="pending" class="button-spinner" aria-hidden="true"></span>
        {{ pending ? "로그인 중" : "로그인" }}
      </button>
    </form>

    <p class="auth-switch">계정이 없으신가요? <a href="/register">회원 가입</a></p>
    <a class="auth-guest-link" href="/playground/">개발용 Playground 바로가기</a>
  </AuthShell>
</template>
