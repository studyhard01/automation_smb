<script setup lang="ts">
import { computed, nextTick, ref } from "vue";

import { ApiError } from "@/api/client";
import { authApi } from "@/api/auth";
import AuthShell from "@/components/AuthShell.vue";

const props = defineProps<{
  redirectToPlayground?: () => void;
}>();

type LoginMode = "local" | "seelis";

const mode = ref<LoginMode>("local");
const identifier = ref("");
const password = ref("");
const pending = ref(false);
const error = ref("");
const localTab = ref<HTMLButtonElement | null>(null);
const seelisTab = ref<HTMLButtonElement | null>(null);

const identifierLabel = computed(() => mode.value === "local" ? "아이디" : "SeeLIS 아이디");
const identifierId = computed(() => mode.value === "local" ? "login-username" : "login-seelis-user-id");
const submitLabel = computed(() => mode.value === "local" ? "로컬 계정으로 로그인" : "SeeLIS 계정으로 로그인");
const pendingLabel = computed(() => mode.value === "local" ? "로컬 계정 로그인 중" : "SeeLIS 계정 로그인 중");

function selectMode(nextMode: LoginMode): void {
  if (pending.value || mode.value === nextMode) return;
  mode.value = nextMode;
  identifier.value = "";
  password.value = "";
  error.value = "";
}

async function handleTabKeydown(event: KeyboardEvent): Promise<void> {
  let nextMode: LoginMode | null = null;
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    nextMode = mode.value === "local" ? "seelis" : "local";
  } else if (event.key === "Home") {
    nextMode = "local";
  } else if (event.key === "End") {
    nextMode = "seelis";
  }
  if (!nextMode) return;
  event.preventDefault();
  selectMode(nextMode);
  await nextTick();
  (nextMode === "local" ? localTab.value : seelisTab.value)?.focus();
}

function loginErrorMessage(cause: unknown): string {
  if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
    return "아이디 또는 비밀번호를 확인해 주세요.";
  }
  if (cause instanceof ApiError && cause.status === 409) {
    return "계정 연동에 문제가 있습니다. 관리자에게 문의해 주세요.";
  }
  if (cause instanceof ApiError && cause.status === 429) {
    return "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.";
  }
  if (cause instanceof ApiError && (cause.status === 502 || cause.status === 503)) {
    return "로그인 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.";
  }
  return "로그인 서비스에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

async function submit(): Promise<void> {
  if (pending.value) return;
  error.value = "";
  pending.value = true;
  try {
    if (mode.value === "local") {
      await authApi.login({ username: identifier.value.trim(), password: password.value });
    } else {
      await authApi.seelisLogin({ userId: identifier.value.trim(), pswd: password.value });
    }
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
  <AuthShell
    :eyebrow="mode === 'local' ? 'LOCAL ACCOUNT' : 'SEELIS ACCOUNT'"
    title="로그인"
    :description="mode === 'local'
      ? '승인된 로컬 계정으로 업무 공간에 접속하세요.'
      : '사용 중인 SeeLIS 계정으로 업무 공간에 접속하세요.'"
  >
    <div class="auth-mode-tabs" role="tablist" aria-label="로그인 방식" @keydown="handleTabKeydown">
      <button
        id="local-login-tab"
        ref="localTab"
        type="button"
        role="tab"
        :aria-selected="mode === 'local'"
        aria-controls="local-login-panel"
        :tabindex="mode === 'local' ? 0 : -1"
        :disabled="pending"
        @click="selectMode('local')"
      >
        로컬 계정
      </button>
      <button
        id="seelis-login-tab"
        ref="seelisTab"
        type="button"
        role="tab"
        :aria-selected="mode === 'seelis'"
        aria-controls="seelis-login-panel"
        :tabindex="mode === 'seelis' ? 0 : -1"
        :disabled="pending"
        @click="selectMode('seelis')"
      >
        SeeLIS 계정
      </button>
    </div>

    <form
      :id="`${mode}-login-panel`"
      class="auth-form"
      role="tabpanel"
      :aria-labelledby="`${mode}-login-tab`"
      @submit.prevent="submit"
    >
      <div class="form-field">
        <label :for="identifierId">{{ identifierLabel }}</label>
        <input
          :id="identifierId"
          v-model="identifier"
          :name="mode === 'local' ? 'username' : 'userId'"
          type="text"
          :autocomplete="mode === 'local' ? 'section-local username' : 'section-seelis username'"
          autocapitalize="none"
          spellcheck="false"
          :disabled="pending"
          required
          autofocus
          :placeholder="`${identifierLabel}를 입력하세요`"
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
          :name="mode === 'local' ? 'local-password' : 'seelis-password'"
          type="password"
          :autocomplete="mode === 'local'
            ? 'section-local current-password'
            : 'section-seelis current-password'"
          :disabled="pending"
          required
          placeholder="비밀번호를 입력하세요"
        >
      </div>

      <p v-if="error" class="form-feedback error" role="alert">{{ error }}</p>

      <button class="primary-button auth-submit" type="submit" :disabled="pending">
        <span v-if="pending" class="button-spinner" aria-hidden="true"></span>
        {{ pending ? pendingLabel : submitLabel }}
      </button>
    </form>

    <p v-if="mode === 'local'" class="auth-switch">
      계정이 없으신가요? <a href="/register">회원 가입</a>
    </p>
    <p v-else class="auth-switch auth-mode-note">SeeLIS 계정은 별도 회원 가입 없이 사용할 수 있습니다.</p>
    <a class="auth-guest-link" href="/playground/">개발용 Playground 바로가기</a>
  </AuthShell>
</template>
