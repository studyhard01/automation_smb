<script setup lang="ts">
import { computed } from "vue";

import PlaygroundApp from "@/App.vue";
import LoginScreen from "@/components/LoginScreen.vue";
import RegisterScreen from "@/components/RegisterScreen.vue";
import UserManagementScreen from "@/components/UserManagementScreen.vue";

const normalizedPath = window.location.pathname.replace(/\/+$/, "") || "/";

const screen = computed(() => {
  if (normalizedPath === "/login") return LoginScreen;
  if (normalizedPath === "/register") return RegisterScreen;
  if (normalizedPath === "/user") return UserManagementScreen;
  return PlaygroundApp;
});
</script>

<template>
  <component :is="screen" />
</template>
