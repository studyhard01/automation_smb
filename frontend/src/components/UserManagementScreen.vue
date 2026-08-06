<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import { authApi } from "@/api/auth";
import { ApiError } from "@/api/client";
import type { AuthUser, ServiceSummary, UserAccessUpdate } from "@/authTypes";

const props = defineProps<{
  redirectToLogin?: () => void;
}>();

type SaveState = "idle" | "saving" | "success" | "error";

const currentUser = ref<AuthUser | null>(null);
const users = ref<AuthUser[]>([]);
const services = ref<ServiceSummary[]>([]);
const query = ref("");
const loading = ref(true);
const loadError = ref("");
const forbidden = ref(false);
const logoutPending = ref(false);
const saveStates = ref<Record<string, SaveState>>({});
const saveMessages = ref<Record<string, string>>({});

const filteredUsers = computed(() => {
  const normalized = query.value.trim().toLocaleLowerCase();
  if (!normalized) return users.value;
  return users.value.filter((user) => (
    [user.username, user.display_name, user.email]
      .join(" ")
      .toLocaleLowerCase()
      .includes(normalized)
  ));
});

const activeCount = computed(() => users.value.filter((user) => user.is_active).length);
const adminCount = computed(() => users.value.filter((user) => user.system_role === "admin").length);

function cloneUser(user: AuthUser): AuthUser {
  return { ...user, service_keys: [...user.service_keys] };
}

function loadErrorMessage(cause: unknown): string {
  if (cause instanceof ApiError && cause.status === 401) return "로그인이 필요합니다.";
  if (cause instanceof ApiError && cause.status === 403) return "사용자 관리 권한이 없습니다.";
  return "사용자 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

async function loadUsers(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  forbidden.value = false;
  try {
    const meResponse = await authApi.getMe();
    currentUser.value = cloneUser(meResponse.user);
    if (meResponse.user.system_role !== "admin" && !meResponse.user.is_superuser) {
      forbidden.value = true;
      return;
    }
    const response = await authApi.getUsers();
    users.value = response.users.map(cloneUser);
    services.value = [...response.services].sort((left, right) => left.name.localeCompare(right.name));
  } catch (cause) {
    loadError.value = loadErrorMessage(cause);
    forbidden.value = cause instanceof ApiError && cause.status === 403;
  } finally {
    loading.value = false;
  }
}

function toggleService(user: AuthUser, serviceKey: string): void {
  if (user.service_keys.includes(serviceKey)) {
    user.service_keys = user.service_keys.filter((key) => key !== serviceKey);
  } else {
    user.service_keys = [...user.service_keys, serviceKey];
  }
  saveStates.value[user.id] = "idle";
  saveMessages.value[user.id] = "";
}

function accessPayload(user: AuthUser): UserAccessUpdate {
  return {
    system_role: user.system_role,
    is_superuser: user.is_superuser,
    is_active: user.is_active,
    all_services_access: user.all_services_access,
    service_keys: user.all_services_access ? [] : [...user.service_keys],
  };
}

async function saveUser(user: AuthUser): Promise<void> {
  if (saveStates.value[user.id] === "saving") return;
  saveStates.value[user.id] = "saving";
  saveMessages.value[user.id] = "저장 중입니다.";
  try {
    const response = await authApi.updateUser(user.id, accessPayload(user));
    const index = users.value.findIndex((item) => item.id === user.id);
    if (index >= 0) users.value.splice(index, 1, cloneUser(response.user));
    saveStates.value[user.id] = "success";
    saveMessages.value[user.id] = "변경사항을 저장했습니다.";
  } catch (cause) {
    saveStates.value[user.id] = "error";
    saveMessages.value[user.id] = cause instanceof ApiError && cause.status === 409
      ? "다른 변경과 충돌했습니다. 새로고침 후 다시 시도해 주세요."
      : "저장하지 못했습니다. 권한과 입력값을 확인해 주세요.";
  }
}

async function logout(): Promise<void> {
  if (logoutPending.value) return;
  logoutPending.value = true;
  try {
    await authApi.logout();
  } finally {
    if (props.redirectToLogin) props.redirectToLogin();
    else window.location.assign("/login");
  }
}

function formatDate(value: string | null): string {
  if (!value) return "기록 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "기록 없음";
  return new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

onMounted(loadUsers);
</script>

<template>
  <main class="user-admin-page">
    <header class="user-admin-header">
      <a class="auth-brand" href="/playground/" aria-label="SMB 자동화 Playground로 이동">
        <span class="brand-mark" aria-hidden="true">S</span>
        <span class="brand-copy">
          <strong>SMB Automation</strong>
          <small>사용자 및 서비스 권한 관리</small>
        </span>
      </a>
      <div class="user-header-actions">
        <span v-if="currentUser" class="current-user-pill">{{ currentUser.display_name || currentUser.username }}</span>
        <a class="secondary-button" href="/playground/">Playground</a>
        <button class="secondary-button" type="button" :disabled="logoutPending" @click="logout">
          {{ logoutPending ? "로그아웃 중" : "로그아웃" }}
        </button>
      </div>
    </header>

    <div class="user-admin-content">
      <section class="user-admin-intro">
        <div>
          <span class="auth-eyebrow">ACCESS CONTROL</span>
          <h1>사용자 관리</h1>
          <p>계정 상태, 시스템 역할, 서비스별 접근 권한을 관리합니다.</p>
        </div>
        <dl class="user-summary" aria-label="사용자 현황">
          <div><dt>전체 사용자</dt><dd>{{ users.length }}</dd></div>
          <div><dt>활성 계정</dt><dd>{{ activeCount }}</dd></div>
          <div><dt>관리자</dt><dd>{{ adminCount }}</dd></div>
        </dl>
      </section>

      <section v-if="loading" class="user-page-state" aria-live="polite">
        <span class="large-spinner" aria-hidden="true"></span>
        <strong>사용자 정보를 불러오고 있습니다.</strong>
        <p>잠시만 기다려 주세요.</p>
      </section>

      <section v-else-if="forbidden" class="user-page-state error" role="alert">
        <span class="state-icon" aria-hidden="true">!</span>
        <strong>사용자 관리 권한이 없습니다.</strong>
        <p>관리자 계정으로 로그인하거나 최고 관리자에게 권한을 요청해 주세요.</p>
        <div><a class="primary-button" href="/login">로그인 화면으로 이동</a></div>
      </section>

      <section v-else-if="loadError" class="user-page-state error" role="alert">
        <span class="state-icon" aria-hidden="true">!</span>
        <strong>{{ loadError }}</strong>
        <p>연결 상태를 확인한 뒤 다시 시도해 주세요.</p>
        <div><button class="primary-button" type="button" @click="loadUsers">다시 불러오기</button></div>
      </section>

      <section v-else class="user-directory" aria-labelledby="user-directory-title">
        <div class="user-toolbar">
          <div>
            <h2 id="user-directory-title">계정 및 권한</h2>
            <p>최고 관리자 여부와 서비스 권한은 별도로 관리됩니다.</p>
          </div>
          <div class="user-search">
            <label class="visually-hidden" for="user-search-input">사용자 검색</label>
            <span aria-hidden="true">⌕</span>
            <input
              id="user-search-input"
              v-model="query"
              type="search"
              placeholder="이름, 아이디, 이메일 검색"
            >
          </div>
        </div>

        <div v-if="users.length === 0" class="user-page-state compact">
          <span class="state-icon" aria-hidden="true">＋</span>
          <strong>등록된 사용자가 없습니다.</strong>
          <p>회원 가입 화면에서 첫 사용자를 등록해 주세요.</p>
          <div><a class="primary-button" href="/register">회원 가입</a></div>
        </div>

        <div v-else-if="filteredUsers.length === 0" class="user-page-state compact">
          <span class="state-icon" aria-hidden="true">⌕</span>
          <strong>검색 결과가 없습니다.</strong>
          <p>다른 이름, 아이디 또는 이메일로 검색해 보세요.</p>
          <div><button class="secondary-button" type="button" @click="query = ''">검색 지우기</button></div>
        </div>

        <div v-else class="user-list">
          <article v-for="user in filteredUsers" :key="user.id" class="user-card">
            <div class="user-identity">
              <span class="user-avatar" aria-hidden="true">{{ (user.display_name || user.username).slice(0, 1).toUpperCase() }}</span>
              <div>
                <div class="user-name-row">
                  <h3>{{ user.display_name || user.username }}</h3>
                  <span v-if="user.id === currentUser?.id" class="self-badge">내 계정</span>
                  <span v-if="user.is_superuser" class="superuser-badge">최고 관리자</span>
                </div>
                <p>@{{ user.username }} · {{ user.email }}</p>
                <small>가입 {{ formatDate(user.created_at) }} · 최근 로그인 {{ formatDate(user.last_login_at) }}</small>
              </div>
            </div>

            <div class="user-control-group">
              <label :for="`role-${user.id}`">시스템 역할</label>
              <select
                :id="`role-${user.id}`"
                v-model="user.system_role"
                :disabled="user.id === currentUser?.id"
                @change="saveStates[user.id] = 'idle'"
              >
                <option value="user">일반 사용자</option>
                <option value="admin">관리자</option>
              </select>
              <label class="check-control">
                <input
                  v-model="user.is_superuser"
                  type="checkbox"
                  :disabled="!currentUser?.is_superuser || user.id === currentUser?.id"
                  @change="saveStates[user.id] = 'idle'"
                >
                <span>최고 관리자</span>
              </label>
              <label class="check-control">
                <input
                  v-model="user.is_active"
                  type="checkbox"
                  :disabled="user.id === currentUser?.id"
                  @change="saveStates[user.id] = 'idle'"
                >
                <span>계정 활성화</span>
              </label>
            </div>

            <div class="service-access-editor">
              <div class="service-access-heading">
                <div>
                  <strong>서비스 접근 권한</strong>
                  <small>{{ user.all_services_access ? "모든 현재·향후 서비스" : `${user.service_keys.length}개 서비스 허용` }}</small>
                </div>
                <label class="switch-control">
                  <input
                    v-model="user.all_services_access"
                    type="checkbox"
                    @change="saveStates[user.id] = 'idle'"
                  >
                  <span>전체 허용</span>
                </label>
              </div>
              <fieldset :disabled="user.all_services_access" class="service-options">
                <legend class="visually-hidden">{{ user.username }} 서비스별 접근 권한</legend>
                <label
                  v-for="service in services"
                  :key="service.key"
                  class="service-chip"
                  :class="{ inactive: !service.is_active }"
                  :title="service.description || service.name"
                >
                  <input
                    type="checkbox"
                    :checked="user.service_keys.includes(service.key)"
                    :disabled="!service.is_active"
                    @change="toggleService(user, service.key)"
                  >
                  <span>{{ service.name }} <small>{{ service.key }}</small></span>
                </label>
                <p v-if="services.length === 0">등록된 개별 서비스가 없습니다.</p>
              </fieldset>
            </div>

            <div class="user-save-area">
              <button
                class="primary-button"
                type="button"
                :disabled="saveStates[user.id] === 'saving'"
                @click="saveUser(user)"
              >
                {{ saveStates[user.id] === "saving" ? "저장 중" : "변경사항 저장" }}
              </button>
              <p
                v-if="saveMessages[user.id]"
                class="save-feedback"
                :class="saveStates[user.id]"
                :role="saveStates[user.id] === 'error' ? 'alert' : 'status'"
              >{{ saveMessages[user.id] }}</p>
            </div>
          </article>
        </div>
      </section>
    </div>
  </main>
</template>
