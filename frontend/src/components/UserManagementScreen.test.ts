import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authApi } from "@/api/auth";
import type { AuthUser } from "@/authTypes";
import UserManagementScreen from "@/components/UserManagementScreen.vue";

vi.mock("@/api/auth", () => ({
  authApi: {
    getMe: vi.fn(),
    getUsers: vi.fn(),
    updateUser: vi.fn(),
    logout: vi.fn(),
  },
}));

const admin: AuthUser = {
  id: "admin-1",
  username: "admin-user",
  email: "admin@example.com",
  display_name: "관리자",
  system_role: "admin",
  is_superuser: true,
  is_active: true,
  all_services_access: true,
  service_keys: [],
  created_at: "2026-08-01T00:00:00Z",
  last_login_at: "2026-08-06T00:00:00Z",
};

const user: AuthUser = {
  id: "user-2",
  username: "synthetic-user",
  email: "synthetic@example.com",
  display_name: "합성 사용자",
  system_role: "user",
  is_superuser: false,
  is_active: true,
  all_services_access: false,
  service_keys: ["playground"],
  created_at: "2026-08-02T00:00:00Z",
  last_login_at: null,
};

describe("UserManagementScreen", () => {
  beforeEach(() => {
    vi.mocked(authApi.getMe).mockReset().mockResolvedValue({ user: admin });
    vi.mocked(authApi.getUsers).mockReset().mockResolvedValue({
      users: [admin, user],
      services: [
        { key: "playground", name: "Playground", description: "문서 대화", is_active: true },
        { key: "reports", name: "보고서", description: "보고서 작성", is_active: true },
      ],
    });
    vi.mocked(authApi.updateUser).mockReset();
    vi.mocked(authApi.logout).mockReset();
  });

  it("관리자가 역할을 변경하고 명시적 권한 payload로 저장한다", async () => {
    vi.mocked(authApi.updateUser).mockResolvedValue({ user: { ...user, system_role: "admin" } });
    const wrapper = mount(UserManagementScreen);
    await flushPromises();

    expect(wrapper.text()).toContain("합성 사용자");
    await wrapper.get("#role-user-2").setValue("admin");
    const userCards = wrapper.findAll(".user-card");
    await userCards[1].get(".user-save-area button").trigger("click");
    await flushPromises();

    expect(authApi.updateUser).toHaveBeenCalledWith("user-2", {
      system_role: "admin",
      is_superuser: false,
      is_active: true,
      all_services_access: false,
      service_keys: ["playground"],
    });
    expect(wrapper.text()).toContain("변경사항을 저장했습니다");
  });

  it("검색 결과가 없을 때 명확한 빈 상태를 표시한다", async () => {
    const wrapper = mount(UserManagementScreen);
    await flushPromises();
    await wrapper.get("#user-search-input").setValue("없는 사용자");
    expect(wrapper.text()).toContain("검색 결과가 없습니다");
  });
});
