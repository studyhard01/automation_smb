<script setup lang="ts">
import { ref } from "vue";

import { ApiError } from "@/api/client";
import { authApi } from "@/api/auth";
import AuthShell from "@/components/AuthShell.vue";

const props = defineProps<{
  redirectToLogin?: () => void;
}>();

const username = ref("");
const displayName = ref("");
const email = ref("");
const password = ref("");
const passwordConfirm = ref("");
const pending = ref(false);
const error = ref("");
const registeredUsername = ref("");

function registerErrorMessage(cause: unknown): string {
  if (cause instanceof ApiError && cause.status === 409) {
    return "이미 사용 중인 아이디 또는 이메일입니다.";
  }
  if (cause instanceof ApiError && cause.status === 422) {
    return cause.message || "입력 내용을 확인해 주세요.";
  }
  return "회원 가입을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

async function submit(): Promise<void> {
  if (pending.value) return;
  error.value = "";
  if (password.value !== passwordConfirm.value) {
    error.value = "비밀번호가 서로 일치하지 않습니다.";
    return;
  }

  pending.value = true;
  try {
    const response = await authApi.register({
      username: username.value.trim(),
      display_name: displayName.value.trim(),
      email: email.value.trim(),
      password: password.value,
    });
    registeredUsername.value = response.user.username;
  } catch (cause) {
    error.value = registerErrorMessage(cause);
  } finally {
    pending.value = false;
  }
}

function goToLogin(): void {
  if (props.redirectToLogin) props.redirectToLogin();
  else window.location.assign("/login");
}
</script>

<template>
  <AuthShell eyebrow="CREATE ACCOUNT" title="회원 가입" description="기본 정보를 입력해 새 계정을 신청하세요.">
    <div v-if="registeredUsername" class="register-success" role="status">
      <span class="success-check" aria-hidden="true">✓</span>
      <h3>회원 가입이 완료되었습니다.</h3>
      <p><strong>{{ registeredUsername }}</strong> 계정으로 로그인할 수 있습니다.</p>
      <button class="primary-button auth-submit" type="button" @click="goToLogin">로그인으로 이동</button>
    </div>

    <form v-else class="auth-form register-form" @submit.prevent="submit">
      <div class="form-field">
        <label for="register-username">아이디</label>
        <input
          id="register-username"
          v-model="username"
          name="username"
          type="text"
          autocomplete="username"
          autocapitalize="none"
          spellcheck="false"
          minlength="3"
          maxlength="50"
          pattern="[A-Za-z0-9_.-]+"
          required
          autofocus
          placeholder="영문, 숫자, ., _, - 사용"
        >
      </div>
      <div class="form-field">
        <label for="register-display-name">이름</label>
        <input
          id="register-display-name"
          v-model="displayName"
          name="display_name"
          type="text"
          autocomplete="name"
          maxlength="100"
          required
          placeholder="화면에 표시할 이름"
        >
      </div>
      <div class="form-field form-field-wide">
        <label for="register-email">이메일</label>
        <input
          id="register-email"
          v-model="email"
          name="email"
          type="email"
          autocomplete="email"
          maxlength="254"
          required
          placeholder="name@example.com"
        >
      </div>
      <div class="form-field">
        <label for="register-password">비밀번호</label>
        <input
          id="register-password"
          v-model="password"
          name="new-password"
          type="password"
          autocomplete="new-password"
          minlength="10"
          required
          placeholder="10자 이상 입력"
        >
      </div>
      <div class="form-field">
        <label for="register-password-confirm">비밀번호 확인</label>
        <input
          id="register-password-confirm"
          v-model="passwordConfirm"
          name="password_confirm"
          type="password"
          autocomplete="new-password"
          minlength="10"
          required
          placeholder="비밀번호를 다시 입력"
        >
      </div>

      <p v-if="error" class="form-feedback error form-field-wide" role="alert">{{ error }}</p>
      <p class="password-hint form-field-wide">비밀번호는 10자 이상으로 구성하고 다른 서비스와 다르게 설정하세요.</p>

      <button class="primary-button auth-submit form-field-wide" type="submit" :disabled="pending">
        <span v-if="pending" class="button-spinner" aria-hidden="true"></span>
        {{ pending ? "가입 처리 중" : "회원 가입" }}
      </button>
    </form>

    <p v-if="!registeredUsername" class="auth-switch">이미 계정이 있으신가요? <a href="/login">로그인</a></p>
  </AuthShell>
</template>
