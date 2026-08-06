import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authApi } from "@/api/auth";
import LoginScreen from "@/components/LoginScreen.vue";

vi.mock("@/api/auth", () => ({
  authApi: { login: vi.fn() },
}));

describe("LoginScreen", () => {
  beforeEach(() => vi.mocked(authApi.login).mockReset());

  it("로그인 성공 시 Playground로 이동한다", async () => {
    vi.mocked(authApi.login).mockResolvedValue({ user: {} as never });
    const redirect = vi.fn();
    const wrapper = mount(LoginScreen, { props: { redirectToPlayground: redirect } });

    await wrapper.get("#login-username").setValue(" synthetic-user ");
    await wrapper.get("#login-password").setValue("safe-password");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(authApi.login).toHaveBeenCalledWith({ username: "synthetic-user", password: "safe-password" });
    expect(redirect).toHaveBeenCalledOnce();
  });
});
