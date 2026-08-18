import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/App.vue", () => ({ default: { template: '<div data-screen="playground">Playground</div>' } }));
vi.mock("@/components/LoginScreen.vue", () => ({ default: { template: '<div data-screen="login">Login</div>' } }));
vi.mock("@/components/RegisterScreen.vue", () => ({ default: { template: '<div data-screen="register">Register</div>' } }));
vi.mock("@/components/UserManagementScreen.vue", () => ({ default: { template: '<div data-screen="user">User</div>' } }));

describe("AppRouter", () => {
  beforeEach(() => vi.resetModules());

  it.each([
    ["/login", "login"],
    ["/register/", "register"],
    ["/user", "user"],
    ["/playground/", "playground"],
  ])("%s 경로에서 %s 화면만 선택한다", async (path, expected) => {
    window.history.replaceState({}, "", path);
    const { default: AppRouter } = await import("@/AppRouter.vue");
    const wrapper = mount(AppRouter);
    expect(wrapper.get("[data-screen]").attributes("data-screen")).toBe(expected);
  });
});
