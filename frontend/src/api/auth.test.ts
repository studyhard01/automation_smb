import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authApi } from "@/api/auth";
import type { AuthUser, UsersResponse } from "@/authTypes";

const user: AuthUser = {
  id: "user-1",
  username: "synthetic-user",
  email: "synthetic@example.com",
  display_name: "합성 사용자",
  system_role: "user",
  is_superuser: false,
  is_active: true,
  all_services_access: false,
  service_keys: ["playground"],
  created_at: "2026-08-01T00:00:00Z",
  last_login_at: null,
};

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("authApi", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("로그인과 회원 가입 요청에 쿠키 포함 정책과 정확한 payload를 사용한다", async () => {
    fetchMock.mockResolvedValueOnce(response({ user }));
    await authApi.login({ username: "synthetic-user", password: "safe-password" });
    expect(fetchMock).toHaveBeenLastCalledWith("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "synthetic-user", password: "safe-password" }),
      credentials: "include",
    });

    fetchMock.mockResolvedValueOnce(response({ user }));
    await authApi.register({
      username: "synthetic-user",
      display_name: "합성 사용자",
      email: "synthetic@example.com",
      password: "safe-password",
    });
    expect(fetchMock).toHaveBeenLastCalledWith("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: "synthetic-user",
        display_name: "합성 사용자",
        email: "synthetic@example.com",
        password: "safe-password",
      }),
      credentials: "include",
    });
  });

  it("관리자 사용자 목록과 서비스 권한 PATCH 계약을 보존한다", async () => {
    const usersResponse: UsersResponse = {
      users: [user],
      services: [
        { key: "playground", name: "Playground", description: "문서 대화", is_active: true },
        { key: "reports", name: "보고서", description: "보고서 작성", is_active: true },
      ],
    };
    fetchMock.mockResolvedValueOnce(response(usersResponse));
    await expect(authApi.getUsers()).resolves.toEqual(usersResponse);
    expect(fetchMock).toHaveBeenLastCalledWith("/api/users", { credentials: "include" });

    fetchMock.mockResolvedValueOnce(response({ user: { ...user, system_role: "admin" } }));
    await authApi.updateUser(user.id, {
      system_role: "admin",
      is_superuser: false,
      is_active: true,
      all_services_access: false,
      service_keys: ["playground"],
    });
    expect(fetchMock).toHaveBeenLastCalledWith("/api/users/user-1", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system_role: "admin",
        is_superuser: false,
        is_active: true,
        all_services_access: false,
        service_keys: ["playground"],
      }),
      credentials: "include",
    });
  });
});
