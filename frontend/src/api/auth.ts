import { ApiError } from "@/api/client";
import type {
  AuthUserResponse,
  LoginPayload,
  RegisterPayload,
  SeelisLoginPayload,
  UserAccessUpdate,
  UsersResponse,
} from "@/authTypes";
import type { ApiErrorBody } from "@/types";

function normalizeDetail(value: unknown): string {
  if (value == null || value === "") return "";
  if (["string", "number", "boolean"].includes(typeof value)) return String(value);
  if (Array.isArray(value)) return value.map(normalizeDetail).filter(Boolean).join("; ");
  if (typeof value === "object") {
    const detail = value as Record<string, unknown>;
    return normalizeDetail(detail.message ?? detail.detail ?? detail.code ?? detail.error_code);
  }
  return "";
}

async function parseResponse<T>(response: Response): Promise<T> {
  const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
  if (!response.ok) {
    throw new ApiError(
      normalizeDetail(body.detail) || body.message || body.error_code || `HTTP ${response.status}`,
      response.status,
      body.request_id,
    );
  }
  return body as T;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  return parseResponse<T>(
    await fetch(path, {
      ...options,
      credentials: "include",
    }),
  );
}

function jsonOptions(method: "POST" | "PATCH", body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}

export const authApi = {
  async login(payload: LoginPayload): Promise<AuthUserResponse> {
    return request<AuthUserResponse>("/api/auth/login", jsonOptions("POST", payload));
  },

  async seelisLogin(payload: SeelisLoginPayload): Promise<AuthUserResponse> {
    return request<AuthUserResponse>("/api/auth/seelis-login", jsonOptions("POST", payload));
  },

  async register(payload: RegisterPayload): Promise<AuthUserResponse> {
    return request<AuthUserResponse>("/api/auth/register", jsonOptions("POST", payload));
  },

  async getMe(): Promise<AuthUserResponse> {
    return request<AuthUserResponse>("/api/auth/me");
  },

  async logout(): Promise<void> {
    await request<Record<string, unknown>>("/api/auth/logout", jsonOptions("POST"));
  },

  async getUsers(): Promise<UsersResponse> {
    return request<UsersResponse>("/api/users");
  },

  async updateUser(userId: string, payload: UserAccessUpdate): Promise<AuthUserResponse> {
    return request<AuthUserResponse>(
      `/api/users/${encodeURIComponent(userId)}`,
      jsonOptions("PATCH", payload),
    );
  },
};
