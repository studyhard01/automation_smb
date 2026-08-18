import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authApi } from "@/api/auth";
import RegisterScreen from "@/components/RegisterScreen.vue";
import type { AuthUser } from "@/authTypes";

vi.mock("@/api/auth", () => ({
  authApi: { register: vi.fn() },
}));

const registeredUser = {
  username: "synthetic-user",
} as AuthUser;

describe("RegisterScreen", () => {
  beforeEach(() => vi.mocked(authApi.register).mockReset());

  it("서로 다른 비밀번호는 API 호출 전에 안내한다", async () => {
    const wrapper = mount(RegisterScreen);
    await wrapper.get("#register-password").setValue("safe-password");
    await wrapper.get("#register-password-confirm").setValue("different-password");
    await wrapper.get("form").trigger("submit");

    expect(authApi.register).not.toHaveBeenCalled();
    expect(wrapper.get("[role='alert']").text()).toContain("일치하지 않습니다");
  });

  it("회원 가입 성공 후 로그인 이동을 안내한다", async () => {
    vi.mocked(authApi.register).mockResolvedValue({ user: registeredUser });
    const redirect = vi.fn();
    const wrapper = mount(RegisterScreen, { props: { redirectToLogin: redirect } });

    await wrapper.get("#register-username").setValue("synthetic-user");
    await wrapper.get("#register-display-name").setValue("합성 사용자");
    await wrapper.get("#register-email").setValue("synthetic@example.com");
    await wrapper.get("#register-password").setValue("safe-password");
    await wrapper.get("#register-password-confirm").setValue("safe-password");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(authApi.register).toHaveBeenCalledWith({
      username: "synthetic-user",
      display_name: "합성 사용자",
      email: "synthetic@example.com",
      password: "safe-password",
    });
    expect(wrapper.text()).toContain("회원 가입이 완료되었습니다");
    await wrapper.get(".register-success button").trigger("click");
    expect(redirect).toHaveBeenCalledOnce();
  });
});
