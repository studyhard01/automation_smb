import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authApi } from "@/api/auth";
import { ApiError } from "@/api/client";
import type { AuthUserResponse } from "@/authTypes";
import LoginScreen from "@/components/LoginScreen.vue";

vi.mock("@/api/auth", () => ({
  authApi: { login: vi.fn(), seelisLogin: vi.fn() },
}));

const authResponse = { user: {} as never } satisfies AuthUserResponse;

describe("LoginScreen", () => {
  beforeEach(() => {
    vi.mocked(authApi.login).mockReset();
    vi.mocked(authApi.seelisLogin).mockReset();
  });

  it("로컬 계정을 기본 탭으로 제공하고 탭 접근성 및 전환 상태를 유지한다", async () => {
    const wrapper = mount(LoginScreen);
    const localTab = wrapper.get("#local-login-tab");
    const seelisTab = wrapper.get("#seelis-login-tab");

    expect(wrapper.get('[role="tablist"]').attributes("aria-label")).toBe("로그인 방식");
    expect(localTab.attributes("role")).toBe("tab");
    expect(localTab.attributes("aria-selected")).toBe("true");
    expect(localTab.attributes("aria-controls")).toBe("local-login-panel");
    expect(seelisTab.attributes("aria-selected")).toBe("false");
    expect(wrapper.get('[role="tabpanel"]').attributes("aria-labelledby")).toBe("local-login-tab");
    expect(wrapper.get("#login-username").attributes("name")).toBe("username");
    expect(wrapper.find('a[href="/register"]').exists()).toBe(true);

    await wrapper.get("#login-username").setValue("local-user");
    await wrapper.get("#login-password").setValue("must-be-cleared");
    await seelisTab.trigger("click");

    expect(wrapper.get("#seelis-login-tab").attributes("aria-selected")).toBe("true");
    expect(wrapper.get('[role="tabpanel"]').attributes("aria-labelledby")).toBe("seelis-login-tab");
    expect(wrapper.get("#login-seelis-user-id").attributes("name")).toBe("userId");
    expect(wrapper.get("#login-seelis-user-id").attributes("autocomplete")).toBe("section-seelis username");
    expect((wrapper.get("#login-seelis-user-id").element as HTMLInputElement).value).toBe("");
    expect(wrapper.get("#login-password").attributes("name")).toBe("seelis-password");
    expect(wrapper.get("#login-password").attributes("autocomplete")).toBe("section-seelis current-password");
    expect((wrapper.get("#login-password").element as HTMLInputElement).value).toBe("");
    expect(wrapper.find('a[href="/register"]').exists()).toBe(false);
    expect(wrapper.text()).toContain("SeeLIS 계정은 별도 회원 가입 없이 사용할 수 있습니다.");

    await wrapper.get("#seelis-login-tab").trigger("keydown", { key: "Home" });
    expect(wrapper.get("#local-login-tab").attributes("aria-selected")).toBe("true");
  });

  it("로그인 성공 시 Playground로 이동한다", async () => {
    vi.mocked(authApi.login).mockResolvedValue(authResponse);
    const redirect = vi.fn();
    const wrapper = mount(LoginScreen, { props: { redirectToPlayground: redirect } });

    await wrapper.get("#login-username").setValue(" synthetic-user ");
    await wrapper.get("#login-password").setValue("safe-password");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(authApi.login).toHaveBeenCalledWith({ username: "synthetic-user", password: "safe-password" });
    expect(authApi.seelisLogin).not.toHaveBeenCalled();
    expect(redirect).toHaveBeenCalledOnce();
  });

  it("SeeLIS 로그인은 식별자만 정리하고 비밀번호 원문을 보존해 전용 API를 호출한다", async () => {
    vi.mocked(authApi.seelisLogin).mockResolvedValue(authResponse);
    const redirect = vi.fn();
    const wrapper = mount(LoginScreen, { props: { redirectToPlayground: redirect } });

    await wrapper.get("#seelis-login-tab").trigger("click");
    await wrapper.get("#login-seelis-user-id").setValue(" seelis-user ");
    await wrapper.get("#login-password").setValue(" password-with-spaces ");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(authApi.seelisLogin).toHaveBeenCalledWith({
      userId: "seelis-user",
      pswd: " password-with-spaces ",
    });
    expect(authApi.login).not.toHaveBeenCalled();
    expect(redirect).toHaveBeenCalledOnce();
  });

  it("처리 중인 로그인 요청은 중복 제출하지 않는다", async () => {
    let resolveRequest!: (value: AuthUserResponse) => void;
    const request = new Promise<AuthUserResponse>((resolve) => {
      resolveRequest = resolve;
    });
    vi.mocked(authApi.login).mockReturnValue(request);
    const wrapper = mount(LoginScreen, { props: { redirectToPlayground: vi.fn() } });

    await wrapper.get("#login-username").setValue("synthetic-user");
    await wrapper.get("#login-password").setValue("safe-password");
    await wrapper.get("form").trigger("submit");
    await wrapper.get("form").trigger("submit");

    expect(authApi.login).toHaveBeenCalledOnce();
    expect(wrapper.get(".auth-submit").attributes()).toHaveProperty("disabled");
    expect(wrapper.get(".auth-submit").text()).toContain("로컬 계정 로그인 중");

    resolveRequest(authResponse);
    await flushPromises();
  });

  it.each([
    [401, "아이디 또는 비밀번호를 확인해 주세요."],
    [403, "아이디 또는 비밀번호를 확인해 주세요."],
    [409, "계정 연동에 문제가 있습니다. 관리자에게 문의해 주세요."],
    [429, "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요."],
    [502, "로그인 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."],
    [503, "로그인 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."],
  ])("HTTP %i 오류를 안전한 사용자 메시지로 표시한다", async (status, message) => {
    vi.mocked(authApi.login).mockRejectedValue(new ApiError("노출하면 안 되는 상세 오류", status, "request-id"));
    const wrapper = mount(LoginScreen);

    await wrapper.get("#login-username").setValue("synthetic-user");
    await wrapper.get("#login-password").setValue("safe-password");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    const alert = wrapper.get('[role="alert"]');
    expect(alert.text()).toBe(message);
    expect(alert.text()).not.toContain("상세 오류");
    expect(alert.text()).not.toContain("request-id");
  });
});
